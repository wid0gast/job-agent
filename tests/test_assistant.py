"""The assistant: its tools on a real prepared packet, its guards, and a chat turn end to end
(scripted model, no network). Submitting only ever happens from your click on a card."""

import http.client
import json
import re
import threading
import time
from dataclasses import replace
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from job_agent import chat, config, dashboard, llm, packets, prepare
from job_agent.assistant import Assistant, Chat
from test_pipeline import ScriptedModel, add_job, ctx, env, site  # noqa: F401 - fixtures


class Cards:
    """Stands in for the chat: what the user said, and the cards posted."""

    def __init__(self, *said):
        self.said, self.cards, self.workers = list(said), [], 0

    def chat(self):
        return Chat(said=lambda: self.said, card=lambda kind, data: self.cards.append((kind, data)) or len(self.cards),
                    start_worker=self.count_worker)

    def count_worker(self):
        self.workers += 1


def test_packet_tools_and_their_guards(ctx, tmp_path):  # noqa: F811 - pytest fixture
    ctx.settings = replace(ctx.settings, research="off")
    job_id = add_job(ctx, "4001")
    assert prepare.prepare(job_id, ctx, log=lambda *_: None) == "prepared"
    cards = Cards("Use https://linkedin.com/in/jane-doe for my LinkedIn.")
    tools = Assistant(ctx.tracker, ctx.settings, cards.chat())

    packet = tools.get_packet(job_id)
    assert packet["link"] == f"/job/{job_id}" and isinstance(packet["needs_you"], list)
    assert "## Education" in packet["resume"]["text"] and packet["cover_letter"] is None
    assert tools.find_jobs("prepared")["jobs"][0]["id"] == job_id

    # A fact the user never gave is refused; their own words go in, marked as theirs.
    with pytest.raises(ValueError, match="isn't something the user said"):
        tools.edit_answer(job_id, "question_9004", "https://linkedin.com/in/someone-else")
    tools.edit_answer(job_id, "question_9004", "https://linkedin.com/in/jane-doe")
    essay = "I want to build evaluation tools for real teams. " * 5
    tools.edit_answer(job_id, "question_9003", essay)  # Essays may be written for them.
    answers = {a["key"]: a for a in packets.load(ctx.tracker.get(job_id))["answers"]}
    assert answers["question_9004"]["source"] == "you" and answers["question_9003"]["source"] == "model-essay"

    # Document edits: exact quotes, no invented numbers, then typeset again by the worker.
    resume = tools.get_packet(job_id)["resume"]["text"]
    line = next(l[2:] for l in resume.splitlines() if l.startswith("- ") and len(l) > 30)
    with pytest.raises(ValueError, match="isn.t on one line"):
        tools.edit_document(job_id, "resume", [{"find": "not in the resume", "replace": "x"}])
    with pytest.raises(ValueError, match="isn't in your master resume"):
        tools.edit_document(job_id, "resume", [{"find": line, "replace": line + ", cutting costs by 73%"}])
    changed = tools.edit_document(job_id, "resume", [{"find": line, "replace": line + " (team of researchers)"}])
    assert "+" in changed["diff"] and ctx.tracker.task(changed["task_id"])["kind"] == "render"
    assert prepare.render(job_id, ctx, log=lambda *_: None) == "Typeset again: resume."
    pdf_text = (packets.load(ctx.tracker.get(job_id))["documents"]["resume"]["markdown"])
    assert "(team of researchers)" in open(pdf_text).read()

    # Submitting is a card for the user; no task is queued by the assistant.
    asked = tools.request_submission([job_id])
    assert cards.cards[-1][0] == "confirm" and asked["waiting_for"] == [job_id]
    assert not [t for t in ctx.tracker.tasks() if t["kind"] == "submit"]

    # The user's click approves and queues the submit (here through the dashboard's handler).
    store = chat.ChatStore(tmp_path / "chats.db")
    chat_id = store.create("claude-sonnet-5-5")
    item = store.add(chat_id, "confirm", {"jobs": cards.cards[-1][1]["jobs"], "state": "pending"})
    app = SimpleNamespace(chats=store, tracker=ctx.tracker, settings=ctx.settings, ensure_worker=lambda: None)
    dashboard.chat_card(app, item, "yes")
    assert ctx.tracker.get(job_id)["status"] == "approved"
    assert [t["job_id"] for t in ctx.tracker.tasks() if t["kind"] == "submit"] == [job_id]
    assert "The user confirmed" in store.items(chat_id, kind="note")[0]["data"]["text"]
    with pytest.raises(ValueError, match="already been handled"):
        dashboard.chat_card(app, item, "yes")
    # Approved packets aren't prepared again.
    assert "approved" in tools.prepare_applications([job_id])["problems"][0]


def test_profile_edits_can_be_undone(env, tmp_path):  # noqa: F811 - pytest fixture
    settings, tracker = env
    cards = Cards()
    tools = Assistant(tracker, settings, cards.chat())
    before = tools.read_file("story-bank.md")["text"]
    with pytest.raises(ValueError, match="isn't empty"):
        tools.edit_file("story-bank.md", content="replaced")
    if before.strip():
        tools.edit_file("story-bank.md", edits=[{"find": before.strip().splitlines()[0],
                                                 "replace": "## Shipping under pressure"}])
    else:
        tools.edit_file("story-bank.md", content="## Shipping under pressure\n")
    assert "Shipping under pressure" in tools.read_file("story-bank.md")["text"]
    kind, data = cards.cards[-1]
    assert kind == "undo" and data["before"] == before
    with pytest.raises(ValueError, match="must be one of"):
        tools.read_file("../.env")

    store = chat.ChatStore(tmp_path / "chats.db")
    chat_id = store.create("claude-sonnet-5-5")
    item = store.add(chat_id, "undo", data)
    app = SimpleNamespace(chats=store, tracker=tracker, settings=settings, ensure_worker=lambda: None)
    dashboard.chat_card(app, item, "undo")
    assert tools.read_file("story-bank.md")["text"].strip() == before.strip()
    assert "before" not in dashboard.public(store.item(item))  # The old text never goes to the page.

    tools.update_search({"roles": ["Data Analyst"], "include_senior": True})
    assert config.load_search()["roles"] == ["Data Analyst"]
    with pytest.raises(ValueError, match="Unknown setting"):
        tools.update_search({"salary": 1})


# ----- a whole chat turn, through the Messages API path -------------------------------

class Block(SimpleNamespace):
    def model_dump(self, exclude_none=True):
        return {k: v for k, v in vars(self).items()}


class Stream:
    def __init__(self, response):
        self.response = response

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def __iter__(self):
        for block in self.response.content:
            if block.type == "text":
                yield SimpleNamespace(type="content_block_start", content_block=SimpleNamespace(type="text"))
                yield SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=block.text))

    def get_final_message(self):
        return self.response


class FakeAPI:
    """A scripted Messages API: each call returns the next response."""

    def __init__(self, responses):
        self.responses, self.sent = list(responses), []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self.stream))

    def stream(self, **kwargs):
        self.sent.append(json.loads(json.dumps(kwargs, default=str)))
        return Stream(self.responses.pop(0))


def reply(*blocks, stop="end_turn"):
    return Block(content=list(blocks), stop_reason=stop, usage=Block(input_tokens=100, output_tokens=20))


@pytest.fixture()
def app(env, tmp_path, monkeypatch):  # noqa: F811 - pytest fixture
    settings, _ = env
    monkeypatch.setattr(dashboard, "DB_PATH", tmp_path / "data" / "t.db")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    api = llm.AnthropicAPI(spend_cap_usd=1.0)
    monkeypatch.setattr(chat.models, "client", lambda settings: api)
    app = dashboard.Dashboard(replace(settings, submit_mode="review"), 0)
    app.llm_missing = False
    app.ensure_worker = lambda: None
    app.api = api
    server = ThreadingHTTPServer(("127.0.0.1", 0), None)
    app.port = server.server_port
    server.RequestHandlerClass = dashboard.handler_for(app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield app
    server.shutdown()


def call(app, method, path, fields=None, token=True):
    conn = http.client.HTTPConnection("127.0.0.1", app.port, timeout=10)
    body = urlencode({**({"token": app.token} if token else {}), **(fields or {})}) if method == "POST" else ""
    conn.request(method, path, body, {"Host": f"127.0.0.1:{app.port}", "Origin": f"http://127.0.0.1:{app.port}",
                                      "Content-Type": "application/x-www-form-urlencoded"})
    response = conn.getresponse()
    return response.status, response.read().decode()


def settle(app, chat_id):
    deadline = time.time() + 10
    while chat_id in app.turns.running and time.time() < deadline:
        time.sleep(0.05)
    assert chat_id not in app.turns.running


def test_a_chat_turn_uses_tools_and_remembers(app):
    job_id = add_job(app.tracker, "4001")
    app.api.client = FakeAPI([
        reply(Block(type="thinking", thinking="", signature="sig"), Block(type="text", text="Let me look."),
              Block(type="tool_use", id="toolu_1", name="find_jobs", input={"status": "found"}), stop="tool_use"),
        reply(Block(type="text", text=f"You have one: [the internship](/job/{job_id}).")),
        reply(Block(type="text", text="Still one.")),
    ])
    page = call(app, "GET", "/assistant")[1]
    assert 'src="/assistant/chat.js"' in page and "What can I do for you?" in page
    assert call(app, "GET", "/assistant/chat.js")[0] == 200
    assert call(app, "POST", "/assistant/api/send", {"text": "Hi"}, token=False)[0] == 403
    assert "Pick a model" in call(app, "POST", "/assistant/api/send", {"text": "Hi", "model": "gpt-9"})[1]

    status, body = call(app, "POST", "/assistant/api/send", {"text": "What jobs do I have?", "model": "claude-opus-5-5"})
    chat_id = json.loads(body)["chat"]
    settle(app, chat_id)
    state = json.loads(call(app, "GET", f"/assistant/api/poll?chat={chat_id}&since=0")[1])
    kinds = [(i["kind"], i["data"].get("name") or i["data"].get("text")) for i in state["items"]]
    assert kinds == [("user", "What jobs do I have?"), ("text", "Let me look."), ("tool", "find_jobs"),
                     ("text", f"You have one: [the internship](/job/{job_id})."), ("stats", None)]
    footer = state["items"][-1]["data"]  # What the reply took: two requests to Opus.
    assert footer["requests"] == 2 and footer["model"] == "claude-opus-5-5"
    tool = state["items"][2]["data"]
    assert tool["status"] == "ok" and tool["summary"] == "1 job(s)"
    first, second = app.api.client.sent[:2]
    assert first["model"] == "claude-opus-5-5" and "[App now: jobs: 1 found" in first["messages"][0]["content"][0]["text"]
    assert {"web_search", "web_fetch"} <= {t["name"] for t in first["tools"]}
    assert second["messages"][1]["content"][0] == {"type": "thinking", "thinking": "", "signature": "sig"}
    result = second["messages"][2]["content"][0]
    assert result["tool_use_id"] == "toolu_1" and json.loads(result["content"])["total"] == 1
    assert app.chats.chat(chat_id)["title"] == "What jobs do I have?"

    # The next message carries the whole conversation, and what the user did in between.
    app.chats.add(chat_id, "note", {"text": "The user undid your edit to profile.yaml.", "delivered": False})
    call(app, "POST", "/assistant/api/send", {"chat": chat_id, "text": "And now?"})
    settle(app, chat_id)
    third = app.api.client.sent[2]
    assert len(third["messages"]) == 5
    assert "[Since your last reply: The user undid your edit" in third["messages"][4]["content"][0]["text"]
    assert not json.loads(call(app, "GET", f"/assistant/api/poll?chat={chat_id}&since={state['rev']}")[1])["running"]
    assert re.search(r"/assistant\?chat=\d+", call(app, "GET", "/assistant?new=1")[1])  # Listed in the sidebar.
    # The Assistant tab goes back to the latest chat rather than an empty new one.
    conn = http.client.HTTPConnection("127.0.0.1", app.port, timeout=10)
    conn.request("GET", "/assistant", headers={"Host": f"127.0.0.1:{app.port}"})
    answer = conn.getresponse()
    assert answer.status == 303 and answer.getheader("Location") == f"/assistant?chat={chat_id}"


def test_tool_errors_reach_claude_and_stop_works(app):
    app.api.client = FakeAPI([
        reply(Block(type="tool_use", id="toolu_1", name="get_job", input={"job_id": 999}), stop="tool_use"),
        reply(Block(type="text", text="That job doesn't exist.")),
    ])
    chat_id = app.turns.send(None, "Open job 999")
    settle(app, chat_id)
    error = app.api.client.sent[1]["messages"][2]["content"][0]
    assert error["is_error"] and "No job 999" in error["content"]
    tool = app.chats.items(chat_id, kind="tool")[0]["data"]
    assert tool["status"] == "error"

    # Stopping: the turn ends, and Claude hears about it next time.
    app.api.client = FakeAPI([reply(Block(type="tool_use", id="toolu_2", name="wait_for_tasks",
                                          input={"task_ids": [app.tracker.add_task("refresh")], "max_minutes": 1}),
                                    stop="tool_use")])
    app.turns.send(chat_id, "Refresh and wait")
    time.sleep(0.3)
    app.turns.stop(chat_id)
    settle(app, chat_id)
    assert app.chats.items(chat_id, kind="note")[-1]["data"]["shown"] == "You stopped Claude."


def test_each_chat_picks_its_effort(app):
    """Effort is chosen beside the model, kept per chat, sent with every request and shown in the footer.
    Extra high is an Opus level: on other models it runs as high."""
    app.api.client = FakeAPI([reply(Block(type="text", text="Done.")), reply(Block(type="text", text="Again."))])
    assert "Pick an effort" in call(app, "POST", "/assistant/api/send", {"text": "Hi", "effort": "huge"})[1]
    page = call(app, "GET", "/assistant")[1]
    assert 'id=effort' in page and 'value="xhigh"' in page

    chat_id = json.loads(call(app, "POST", "/assistant/api/send",
                              {"text": "Hi", "model": "claude-opus-5-5", "effort": "max"})[1])["chat"]
    settle(app, chat_id)
    assert app.api.client.sent[0]["output_config"]["effort"] == "max"
    state = json.loads(call(app, "GET", f"/assistant/api/poll?chat={chat_id}&since=0")[1])
    assert state["items"][-1]["data"]["effort"] == "max"

    call(app, "POST", "/assistant/api/model", {"chat": chat_id, "model": "claude-sonnet-5-5", "effort": "xhigh"})
    call(app, "POST", "/assistant/api/send", {"chat": chat_id, "text": "And now?"})
    settle(app, chat_id)
    assert app.api.client.sent[1]["output_config"]["effort"] == "high"  # Extra high is Opus-only.
