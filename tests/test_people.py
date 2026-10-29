"""People to contact: code keeps only people, links and addresses the agent really saw,
drafts follow the advice (short, a question, no invented numbers), and nothing is sent
except by you, from your own email, within the daily cap."""

import json
from datetime import UTC, datetime, timedelta

import pytest
import yaml

from job_agent import config, packets, people, prepare, research
from test_pipeline import ScriptedModel, add_job, ctx, env, site  # noqa: F401 - fixtures

ALUM = "https://www.linkedin.com/in/maria-gomez-1"
RESULTS = [
    {"title": "Maria Gomez - Machine Learning Engineer - Contoso AI | LinkedIn", "url": ALUM,
     "snippet": "Machine Learning Engineer at Contoso AI. Education: New York University."},
    {"title": "Contoso AI careers", "url": "https://contoso.example/team",
     "snippet": "Questions? Write to Sam Patel, University Recruiter, at sam.patel@contoso.ai."},
    {"title": "Leo Chen | Contoso AI", "url": "https://contoso.example/leo",
     "snippet": "Leo Chen, Recruiting Lead at Contoso AI."},
]
GOOD = {"subject": "Fellow NYU alum, a quick question",
        "body": "Hi Maria,\n\nI'm a fellow NYU student applying to the Machine Learning Intern role at Contoso AI. "
                "Could I ask you a few questions about your experience on the team?\n\nJane Doe",
        "linkedin_note": "Hi Maria, fellow NYU student here, applying to Contoso's ML internship. "
                         "Could I ask you about your experience on the team?",
        "follow_up": "Hi Maria, a gentle nudge on my note last week. Would you have 15 minutes?"}


class PeopleModel(ScriptedModel):
    def __init__(self, steps):
        super().__init__()
        self.steps, self.results, self.drafts = steps, [], 0

    def complete(self, messages, tools, task="agent"):
        if messages[-1]["role"] == "tool" and task == "people":
            self.results.append(json.loads(messages[-1]["content"]))
        if task != "people":  # Company research: nothing to report.
            return {"tool_calls": [{"id": "r", "type": "function",
                                    "function": {"name": "finish", "arguments": '{"summary": "none"}'}}]}
        name, args = self.steps.pop(0)
        return {"tool_calls": [{"id": name + str(len(self.steps)), "type": "function",
                                "function": {"name": name, "arguments": json.dumps(args)}}]}

    def json(self, system, user, schema, name):
        if name != "outreach":
            return super().json(system, user, schema, name)
        self.seen.append((name, user))
        self.drafts += 1
        ids = [p["id"] for p in json.loads(user)["people"]]
        first = {**GOOD, "body": GOOD["body"] + " I grew revenue 40% at my last job."}
        return {"messages": [{"id": i, **(first if self.drafts == 1 else GOOD)} for i in ids]}


def scout_steps():
    return [
        ("web_search", {"query": 'site:linkedin.com/in "Contoso AI" "New York University"'}),
        ("fetch_page", {"url": ALUM}),                                              # LinkedIn: refused
        ("save_person", {"name": "Maria Gomez", "kind": "background", "title": "ML Engineer", "why": "NYU alum",
                         "url": ALUM, "quote": "Machine Learning Engineer at Contoso AI",
                         "shared_on_page": "New York University", "shared_in_resume": "New York University"}),
        ("save_person", {"name": "Maria Gomez", "kind": "background", "title": "ML Engineer", "why": "NYU alum",
                         "url": ALUM, "quote": "Maria Gomez - Machine Learning Engineer - Contoso AI",
                         "shared_on_page": "Contoso AI", "shared_in_resume": "Contoso AI"}),  # not shared
        ("save_person", {"name": "Maria Gomez", "kind": "peer", "title": "ML Engineer", "why": "On the team",
                         "url": ALUM, "quote": "Maria Gomez - Machine Learning Engineer - Contoso AI",
                         "email": "maria@contoso.ai"}),                                        # invented email
        ("save_person", {"name": "Maria Gomez", "kind": "background", "title": "ML Engineer", "why": "Also at NYU",
                         "url": ALUM, "quote": "Maria Gomez - Machine Learning Engineer - Contoso AI | LinkedIn "
                                               "Machine Learning Engineer at Contoso AI. Education: New York University.",
                         "linkedin_url": ALUM, "shared_on_page": "New York University",
                         "shared_in_resume": "New York University"}),
        ("save_person", {"name": "Dana Fox", "kind": "hiring_manager", "title": "Manager", "why": "x",
                         "url": ALUM, "quote": "Maria Gomez"}),                     # kind not wanted
        ("note_email_format", {"address_format": "{first}.{last}@contoso.ai", "example": "sam.patel@contoso.ai",
                               "example_name": "Sam Patel"}),
        ("save_person", {"name": "Leo Chen", "kind": "recruiter", "title": "Recruiting Lead", "why": "Recruits here",
                         "url": "https://contoso.example/leo", "quote": "Leo Chen, Recruiting Lead at Contoso AI"}),
        ("finish", {"summary": "An NYU alum and a recruiter."}),
    ]


def prepare_and_find(job_id, ctx, **kwargs):  # noqa: F811
    """Prepare, then find people, as the worker does (people are a task of their own, after)."""
    status = prepare.prepare(job_id, ctx, **kwargs)
    if status in ("prepared", "needs_you") and prepare.wants_people(ctx):
        people.refresh(job_id, ctx, log=lambda *_: None)
    return status


def test_people_are_checked_drafted_and_sent_only_by_you(ctx, monkeypatch):  # noqa: F811
    monkeypatch.setattr(research, "search", lambda http, query: RESULTS)
    ctx.llm = PeopleModel(scout_steps())
    job_id = add_job(ctx, "4001")
    assert prepare_and_find(job_id, ctx, log=lambda *_: None) == "prepared"

    results = ctx.llm.results
    assert "LinkedIn pages aren't opened" in results[1]["error"]
    assert "must contain the person's name" in results[2]["error"]
    assert "shared_in_resume must be the user's" in results[3]["error"]
    assert "Only an email address shown on a page" in results[4]["error"]
    assert results[5]["ok"] and "only:" in results[6]["error"]

    found = packets.load(ctx.tracker.get(job_id))["people"]
    maria, leo = found["people"]
    assert (maria["kind"], maria["shared"], maria["linkedin"]) == ("background", "New York University", ALUM)
    assert (maria["email"], maria["email_status"]) == ("maria.gomez@contoso.ai", "guessed")
    assert (leo["email"], leo["email_status"]) == ("leo.chen@contoso.ai", "guessed")
    # The first draft invented a number, so it was sent back once with the problem named.
    assert ctx.llm.drafts == 2 and "40%" not in maria["draft"]["body"] and not maria["checks"]
    second = json.loads([u for n, u in ctx.llm.seen if n == "outreach"][1])
    assert "40%" in json.dumps(second["fix_these"])

    # Sending: the draft opens in your email; the app only records it, once, within the cap.
    url = people.compose_url(maria, "gmail")
    assert url.startswith("https://mail.google.com/mail/?view=cm") and "maria.gomez%40contoso.ai" in url
    assert people.compose_url(maria, "mail").startswith("mailto:maria.gomez@contoso.ai?subject=")
    with pytest.raises(ValueError, match="before a follow-up"):
        people.mark_sent(ctx.tracker, job_id, maria["id"], follow_up=True)
    people.mark_sent(ctx.tracker, job_id, maria["id"])
    with pytest.raises(ValueError, match="Already sent"):
        people.mark_sent(ctx.tracker, job_id, maria["id"])
    assert ctx.tracker.contacted_today() == 1
    monkeypatch.setenv("DAILY_OUTREACH_LIMIT", "1")
    with pytest.raises(ValueError, match="emailed 1 people today"):
        people.mark_sent(ctx.tracker, job_id, leo["id"])

    # Reminders: a second person after 3 days, one follow-up after 7, none once they reply.
    def sent_days_ago(days):
        packet = packets.load(ctx.tracker.get(job_id))
        packet["people"]["people"][0]["sent_at"] = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        packets.store(packet)

    sent_days_ago(1)
    assert people.due(ctx.tracker) == []
    sent_days_ago(4)
    assert [(d["action"], d["person"]["name"]) for d in people.due(ctx.tracker)] == [("second_person", "Leo Chen")]
    sent_days_ago(8)
    assert ("follow_up", "Maria Gomez") in [(d["action"], d["person"]["name"]) for d in people.due(ctx.tracker)]
    people.set_state(ctx.tracker, job_id, maria["id"], "replied")
    assert people.due(ctx.tracker) == []

    # A redo of the packet keeps the people you've emailed.
    ctx.llm = PeopleModel([("finish", {"summary": "No one new."})])
    prepare_and_find(job_id, ctx, log=lambda *_: None, note="shorter")
    kept = packets.load(ctx.tracker.get(job_id))["people"]["people"]
    assert [p["name"] for p in kept] == ["Maria Gomez"] and kept[0]["replied_at"]


def test_search_page_choices_and_no_contact_postings(ctx, monkeypatch):  # noqa: F811
    assert people.wanted({"preferences": {}}) == (people.DEFAULT_KINDS, people.DEFAULT_PER_JOB)
    assert people.wanted({"preferences": {"people_kinds": [], "people_per_job": 9}}) == ([], people.MAX_PER_JOB)
    profile = config.load_profile()
    ctx.llm = PeopleModel([])
    job = ctx.tracker.get(add_job(ctx, "4001"))
    found = people.find(ctx, job, "Please do not contact our recruiters directly.", profile, "", lambda *_: None)
    assert found["people"] == [] and "asks applicants not to contact" in found["summary"]
    (config.PROFILE_DIR / "search.yaml").write_text(yaml.safe_dump({"people_kinds": []}))
    off = people.find(ctx, job, "A posting.", config.load_profile(), "", lambda *_: None)
    assert off["summary"].startswith("Turned off")


def test_your_edits_are_checked(ctx, monkeypatch):  # noqa: F811
    monkeypatch.setattr(research, "search", lambda http, query: RESULTS)
    ctx.llm = PeopleModel(scout_steps())
    job_id = add_job(ctx, "4001")
    prepare_and_find(job_id, ctx, log=lambda *_: None)
    pid = packets.load(ctx.tracker.get(job_id))["people"]["people"][0]["id"]
    person = people.edit(ctx.tracker, job_id, pid, {"body": "Hi Maria, " + "word " * 120, "email": "m@x.org"})
    assert (person["email"], person["email_status"]) == ("m@x.org", "yours")
    assert any("words" in c for c in person["checks"]) and any("question" in c for c in person["checks"])
    with pytest.raises(ValueError, match="isn't an email"):
        people.edit(ctx.tracker, job_id, pid, {"email": "not an address"})


def test_the_assistant_asks_and_you_send(ctx, tmp_path, monkeypatch):  # noqa: F811
    from types import SimpleNamespace

    from job_agent import chat, dashboard
    from job_agent.assistant import Assistant
    from test_assistant import Cards

    monkeypatch.setattr(research, "search", lambda http, query: RESULTS)
    ctx.llm = PeopleModel(scout_steps())
    job_id = add_job(ctx, "4001")
    prepare_and_find(job_id, ctx, log=lambda *_: None)
    cards = Cards("Her address is maria@contoso-mail.com")
    tools = Assistant(ctx.tracker, ctx.settings, cards.chat())
    listed = tools.get_packet(job_id)["people_to_contact"]["people"]
    pid = listed[0]["id"]
    assert listed[0]["email_status"] == "guessed" and listed[0]["draft"]["subject"]

    with pytest.raises(ValueError, match="as the user wrote it"):
        tools.edit_outreach(job_id, pid, email="someone@else.com")
    with pytest.raises(ValueError, match="Numbers not in the resume"):
        tools.edit_outreach(job_id, pid, body="Hi Maria, I led 12 people. Could we talk?")
    assert tools.edit_outreach(job_id, pid, email="maria@contoso-mail.com")["email"] == "maria@contoso-mail.com"

    asked = tools.request_outreach(job_id, pid)
    kind, data = cards.cards[-1]
    assert kind == "outreach" and asked["card"] and data["gmail"].startswith("https://mail.google.com/")
    assert ctx.tracker.contacted_today() == 0  # Nothing recorded until the user clicks.

    store = chat.ChatStore(tmp_path / "chats.db")
    chat_id = store.create("claude-sonnet-5-5")
    item = store.add(chat_id, "outreach", data)
    app = SimpleNamespace(chats=store, tracker=ctx.tracker, settings=ctx.settings, ensure_worker=lambda: None)
    dashboard.chat_card(app, item, "sent")
    assert ctx.tracker.contacted_today() == 1 and store.item(item)["data"]["state"] == "done"
    with pytest.raises(ValueError, match="Already sent"):
        tools.request_outreach(job_id, pid)


def test_dashboard_shows_people_and_opens_your_email(ctx, tmp_path, monkeypatch):  # noqa: F811
    import re
    import threading
    from http.server import ThreadingHTTPServer

    from job_agent import dashboard
    from test_dashboard import request

    monkeypatch.setattr(research, "search", lambda http, query: RESULTS)
    ctx.llm = PeopleModel(scout_steps())
    job_id = add_job(ctx, "4001")
    prepare_and_find(job_id, ctx, log=lambda *_: None)
    monkeypatch.setattr(dashboard, "DB_PATH", tmp_path / "data" / "t.db")
    app = dashboard.Dashboard(ctx.settings, 0)
    server = ThreadingHTTPServer(("127.0.0.1", 0), None)
    app.port = server.server_port
    server.RequestHandlerClass = dashboard.handler_for(app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = {"Origin": f"http://127.0.0.1:{app.port}"}
    try:
        page = request(app.port, "GET", f"/job/{job_id}")[1]
        for text in ("People to contact", "Maria Gomez", "Shares your background", "guessed from the company",
                     "Send email in Gmail", "LinkedIn connection note", "before you apply"):
            assert text in page, text
        token = re.search(r'name=token value="([^"]+)"', page).group(1)
        pid = packets.load(ctx.tracker.get(job_id))["people"]["people"][0]["id"]
        status, _, headers = request(app.port, "POST", "/people/send",
                                     f"token={token}&job_id={job_id}&person={pid}&via=mail", origin)
        assert status == 303 and headers["Location"].startswith("mailto:maria.gomez@contoso.ai?subject=")
        page = request(app.port, "GET", f"/job/{job_id}")[1]
        assert "Emailed " in page and "Undo: I didn" in page
        status, _, headers = request(app.port, "POST", "/people/send",
                                     f"token={token}&job_id={job_id}&person={pid}&via=mail", origin)
        assert headers["Location"].startswith(f"/job/{job_id}?msg=Already")  # Refused: back to the page.

        search = request(app.port, "GET", "/search")[1]
        assert "People to contact" in search and 'value="hiring_manager"' in search
        request(app.port, "POST", "/search", f"token={token}&description=x&fit_threshold=60&people_kinds="
                "recruiter&people_kinds=hiring_manager&people_per_job=3", origin)
        assert people.wanted(config.load_profile()) == (["recruiter", "hiring_manager"], 3)
        assert request(app.port, "GET", f"/runs/people-{job_id}")[0] == 200
    finally:
        server.shutdown()


def test_headline_only_people_are_flagged_and_removed_people_stay_removed(ctx, monkeypatch):  # noqa: F811
    """A search headline can be months old (someone who has left still shows the company), so it's
    marked; and someone you remove is never suggested again for that company."""
    monkeypatch.setattr(research, "search", lambda http, query: RESULTS)
    ctx.llm = PeopleModel(scout_steps())
    job_id = add_job(ctx, "4001")
    prepare_and_find(job_id, ctx, log=lambda *_: None)
    maria = packets.load(ctx.tracker.get(job_id))["people"]["people"][0]
    assert maria["evidence"] == "search result"

    people.set_state(ctx.tracker, job_id, maria["id"], "removed")
    assert people.removed_names("Contoso AI") == {"maria gomez"}
    again = PeopleModel([step for step in scout_steps() if step[0] != "save_person" or "Leo" in step[1]["name"]
                         or step[1].get("kind") == "background"][:])
    ctx.llm = again
    people.refresh(job_id, ctx, log=lambda *_: None)
    names = [p["name"] for p in packets.load(ctx.tracker.get(job_id))["people"]["people"] if not p.get("removed")]
    assert "Maria Gomez" not in names
    assert any("removed this person before" in (r.get("error") or "") for r in again.results)


def test_the_application_is_ready_before_people_are_found(ctx, monkeypatch):  # noqa: F811
    """Finding people takes minutes, so it runs after, alongside other work: the application is
    ready to review at once."""
    from job_agent import worker

    monkeypatch.setattr(research, "search", lambda http, query: RESULTS)
    ctx.llm = PeopleModel(scout_steps())
    job_id = add_job(ctx, "4001")
    task = ctx.tracker.task(ctx.tracker.add_task("prepare", job_id))
    worker._prepare(task, ctx, lambda *_: None)
    assert ctx.tracker.get(job_id)["status"] == "prepared" and "people" not in packets.load(ctx.tracker.get(job_id))
    assert [t["kind"] for t in ctx.tracker.tasks(5)].count("people") == 1 and ("people",) in worker.LANES


def test_someone_you_email_while_people_are_searched_stays_sent(ctx, monkeypatch):  # noqa: F811
    monkeypatch.setattr(research, "search", lambda http, query: RESULTS)
    ctx.llm = PeopleModel(scout_steps())
    job_id = add_job(ctx, "4001")
    prepare_and_find(job_id, ctx, log=lambda *_: None)
    maria = packets.load(ctx.tracker.get(job_id))["people"]["people"][0]

    def search_while_you_send(*args):
        people.mark_sent(ctx.tracker, job_id, maria["id"])
        return {"found_at": people.now(), "people": [], "email_format": None, "summary": "No one new."}

    monkeypatch.setattr(people, "find", search_while_you_send)
    people.refresh(job_id, ctx, log=lambda *_: None)
    kept = packets.load(ctx.tracker.get(job_id))["people"]["people"]
    assert [p["name"] for p in kept] == ["Maria Gomez"] and kept[0]["sent_at"]
