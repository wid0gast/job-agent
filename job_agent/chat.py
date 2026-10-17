"""The assistant chat: conversations, one running turn per chat, and the two ways to Claude.

A turn runs in a thread of the dashboard process. Everything it does becomes an item
in the chat (your message, Claude's text, each tool call, cards for you to click),
saved as it happens; the page polls for new and changed items, plus the text being
written right now. Slow work still goes to the worker as tasks.

- Your Claude plan: the Claude Agent SDK runs Claude Code with the app's tools as an
  in-process MCP server, plus Claude Code's own web search and fetch. The SDK keeps
  the conversation (resumed by session id) and summarises it when it gets long.
- An API key: the Messages API with the same tools, web search and fetch as server
  tools, Claude's thinking kept between turns, and old tool results cleared by the API.
"""

import asyncio
import json
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

from . import config, llm as models
from .assistant import Assistant, Chat
from .prompts import ASSISTANT_PROMPT

MAX_STEPS = 60  # Model calls per turn.


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ----- conversations ---------------------------------------------------------------

class ChatStore:
    """Chats and their items, in the app's database. Each change gets a new `rev`, so the
    page can ask for everything that changed since the last revision it saw."""

    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS chats (
              id INTEGER PRIMARY KEY,
              title TEXT NOT NULL,
              model TEXT NOT NULL,
              session_id TEXT,               -- Claude Code's conversation (your plan)
              api_messages TEXT,             -- the Messages API conversation (API key)
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chat_items (
              id INTEGER PRIMARY KEY,
              chat_id INTEGER NOT NULL,
              kind TEXT NOT NULL,            -- user, text, tool, confirm, undo, note, error
              data TEXT NOT NULL,
              rev INTEGER NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS chat_items_rev ON chat_items (chat_id, rev);
            """)
        self.db.commit()

    def _rev(self) -> int:
        return (self.db.execute("SELECT MAX(rev) FROM chat_items").fetchone()[0] or 0) + 1

    def create(self, model: str, title: str = "New chat") -> int:
        with self.lock:
            cursor = self.db.execute("INSERT INTO chats (title, model, created_at, updated_at) VALUES (?, ?, ?, ?)",
                                     (title, model, now(), now()))
            self.db.commit()
            return cursor.lastrowid

    def chats(self) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute(
                "SELECT id, title, model, updated_at FROM chats ORDER BY updated_at DESC, id DESC")]

    def chat(self, chat_id: int) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM chats WHERE id = ?", (chat_id,)).fetchone()
            return dict(row) if row else None

    def update(self, chat_id: int, **fields) -> None:
        fields["updated_at"] = now()
        with self.lock:
            self.db.execute(f"UPDATE chats SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                            (*fields.values(), chat_id))
            self.db.commit()

    def delete(self, chat_id: int) -> None:
        with self.lock:
            self.db.execute("DELETE FROM chat_items WHERE chat_id = ?", (chat_id,))
            self.db.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
            self.db.commit()

    def add(self, chat_id: int, kind: str, data: dict) -> int:
        with self.lock:
            cursor = self.db.execute(
                "INSERT INTO chat_items (chat_id, kind, data, rev, created_at) VALUES (?, ?, ?, ?, ?)",
                (chat_id, kind, json.dumps(data, ensure_ascii=False, default=str), self._rev(), now()))
            self.db.execute("UPDATE chats SET updated_at = ? WHERE id = ?", (now(), chat_id))
            self.db.commit()
            return cursor.lastrowid

    def change(self, item_id: int, **data) -> dict:
        """Merge `data` into an item; returns the item."""
        with self.lock:
            row = self.db.execute("SELECT * FROM chat_items WHERE id = ?", (item_id,)).fetchone()
            if row is None:
                raise ValueError("No such item.")
            merged = {**json.loads(row["data"]), **data}
            self.db.execute("UPDATE chat_items SET data = ?, rev = ? WHERE id = ?",
                            (json.dumps(merged, ensure_ascii=False, default=str), self._rev(), item_id))
            self.db.commit()
            return {**dict(row), "data": merged}

    def item(self, item_id: int) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM chat_items WHERE id = ?", (item_id,)).fetchone()
            return {**dict(row), "data": json.loads(row["data"])} if row else None

    def items(self, chat_id: int, since_rev: int = 0, kind: str | None = None) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM chat_items WHERE chat_id = ? AND rev > ?" + (" AND kind = ?" if kind else "")
                + " ORDER BY id", (chat_id, since_rev, *([kind] if kind else [])))
            return [{**dict(r), "data": json.loads(r["data"])} for r in rows]

    def recover(self) -> None:
        """After a restart, nothing is running: close tool calls the old process left open."""
        with self.lock:
            rows = self.db.execute("SELECT id FROM chat_items WHERE kind = 'tool' AND data LIKE '%\"running\"%'").fetchall()
        for row in rows:
            self.change(row["id"], status="error", summary="Interrupted: the dashboard restarted.")


# ----- turns -----------------------------------------------------------------------

class Turn:
    """One message from you and everything Claude does about it."""

    def __init__(self, turns: "Turns", chat: dict, prompt: str):
        self.turns, self.store, self.chat_id = turns, turns.store, chat["id"]
        self.chat, self.prompt = chat, prompt
        self.partial = ""                       # Text being written right now.
        self.stopped = threading.Event()
        self.interrupt: Callable[[], None] = lambda: None
        self.tool_items: dict[str, int] = {}    # Built-in web tool call id -> item.

    # Events from the backends.

    def new_text(self) -> None:
        self.partial = ""

    def text(self, delta: str) -> None:
        self.partial += delta

    def commit_text(self, text: str) -> None:
        self.partial = ""
        if text.strip():
            self.store.add(self.chat_id, "text", {"text": text.strip()})

    def tool_start(self, name: str, args: dict, call_id: str | None = None) -> int:
        item = self.store.add(self.chat_id, "tool", {"name": name, "args": args, "status": "running"})
        if call_id:
            self.tool_items[call_id] = item
        return item

    def tool_end(self, item: int | str, ok: bool, result) -> None:
        if isinstance(item, str):
            item = self.tool_items.pop(item, None)
            if item is None:
                return
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
        self.store.change(item, status="ok" if ok else "error", summary=summarize(result, ok),
                          result=text[:20000])

    def run_tool(self, tool, args: dict) -> tuple[bool, str]:
        """Run one of the app's tools; returns (ok, result as JSON text for Claude)."""
        item = self.tool_start(tool.name, args)
        try:
            result, ok = tool.fn(**args), True
        except TypeError as error:
            result, ok = f"Bad arguments for {tool.name}: {error}", False
        except (Exception, SystemExit) as error:  # noqa: BLE001 - Claude reads it and recovers.
            result, ok = f"{error}", False
        self.tool_end(item, ok, result)
        return ok, result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)

    def card(self, kind: str, data: dict) -> int:
        return self.store.add(self.chat_id, kind, data)

    def said(self) -> list[str]:
        return [i["data"]["text"] for i in self.store.items(self.chat_id, kind="user")]


class Turns:
    """Runs turns in threads, one per chat at a time."""

    def __init__(self, store: ChatStore, tracker, settings, start_worker: Callable[[], None],
                 backend: Callable[[object], object] | None = None):
        self.store, self.tracker, self.settings, self.start_worker = store, tracker, settings, start_worker
        self.backend = backend or backend_for  # (llm client) -> object with run(turn, system, tools)
        self.running: dict[int, Turn] = {}
        self.lock = threading.Lock()

    def default_model(self) -> str:
        return models.Model(self.settings.model, self.settings.tier_models).choose("assistant")[0]

    def send(self, chat_id: int | None, text: str, model: str = "") -> int:
        text = text.strip()
        if not text:
            raise ValueError("Type a message first.")
        if chat_id is None:
            chat_id = self.store.create(model or self.default_model(), title=title_for(text))
        chat = self.store.chat(chat_id)
        if chat is None:
            raise ValueError("No such chat.")
        with self.lock:
            if chat_id in self.running:
                raise ValueError("Claude is still answering; stop it or wait.")
            if chat["title"] == "New chat":
                self.store.update(chat_id, title=title_for(text))
            self.store.add(chat_id, "user", {"text": text})
            turn = Turn(self, chat, self.prompt_for(chat_id, text))
            self.running[chat_id] = turn
        threading.Thread(target=self.run, args=(turn,), daemon=True, name=f"chat-{chat_id}").start()
        return chat_id

    def prompt_for(self, chat_id: int, text: str) -> str:
        """Your message, after what changed since Claude last spoke (your clicks, the app's state)."""
        notes = [i for i in self.store.items(chat_id, kind="note") if not i["data"].get("delivered")]
        for note in notes:
            self.store.change(note["id"], delivered=True)
        lines = [f"[Since your last reply: {n['data']['text']}]" for n in notes]
        lines.append(f"[App now: {status_line(self.tracker, self.settings)}]")
        return "\n".join(lines) + "\n\n" + text

    def run(self, turn: Turn) -> None:
        try:
            client = models.client(self.settings)
            if client is None:
                raise RuntimeError(models.NOT_SET_UP)
            assistant = Assistant(self.tracker, self.settings, Chat(
                said=turn.said, card=turn.card, start_worker=self.start_worker, stopped=turn.stopped))
            self.backend(client).run(turn, ASSISTANT_PROMPT, assistant.tools())
        except Exception as error:  # noqa: BLE001 - shown in the chat.
            turn.partial = ""
            self.store.add(turn.chat_id, "error", {"text": f"{error}"[:1500]})
        finally:
            if turn.partial.strip():  # Stopped mid-sentence: keep what was written.
                turn.commit_text(turn.partial + " …")
            if turn.stopped.is_set():
                self.store.add(turn.chat_id, "note", {"text": "The user stopped your last reply.",
                                                      "shown": "You stopped Claude.", "delivered": False})
            with self.lock:
                self.running.pop(turn.chat_id, None)

    def stop(self, chat_id: int) -> None:
        turn = self.running.get(chat_id)
        if turn:
            turn.stopped.set()
            turn.interrupt()

    def partial(self, chat_id: int) -> str | None:
        turn = self.running.get(chat_id)
        return turn.partial if turn else None


def backend_for(client):
    if isinstance(client, models.AnthropicAPI):
        return APIChat(client)
    if isinstance(client, models.ClaudeCode):
        return ClaudeCodeChat(client)
    raise RuntimeError(f"The assistant can't use {type(client).__name__}.")


# ----- your Claude plan: the Agent SDK ------------------------------------------------

class ClaudeCodeChat:
    SERVER = "jobagent"
    WEB_TOOLS = ["WebSearch", "WebFetch"]

    def __init__(self, client: models.ClaudeCode):
        self.client = client

    def run(self, turn: Turn, system: str, tools: list) -> None:
        asyncio.run(self.converse(turn, system, tools))

    async def converse(self, turn: Turn, system: str, tools: list) -> None:
        from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ResultMessage,
                                      SdkMcpTool, StreamEvent, TextBlock, ToolAnnotations, ToolResultBlock,
                                      ToolUseBlock, UserMessage, create_sdk_mcp_server)

        def handler(tool):
            async def call(args: dict) -> dict:
                ok, text = await asyncio.to_thread(turn.run_tool, tool, args)
                return {"content": [{"type": "text", "text": text}], "is_error": not ok}
            return call

        server = create_sdk_mcp_server(self.SERVER, tools=[
            SdkMcpTool(t.name, t.description, t.parameters, handler(t),
                       annotations=ToolAnnotations(readOnlyHint=t.read_only)) for t in tools])
        effort = models.TASKS["assistant"][1]
        options = ClaudeAgentOptions(
            system_prompt=system, model=turn.chat["model"], effort=effort,
            tools=self.WEB_TOOLS, allowed_tools=[f"mcp__{self.SERVER}__{t.name}" for t in tools] + self.WEB_TOOLS,
            mcp_servers={self.SERVER: server},
            # Only this app's tools: none of your Claude Code settings, connectors, skills or commands.
            strict_mcp_config=True, setting_sources=[], skills=[], extra_args={"disable-slash-commands": None},
            include_partial_messages=True, cwd=str(self.client.workdir), max_turns=MAX_STEPS,
            resume=turn.chat.get("session_id") or None,
            # An API key would take precedence over your sign-in; ~20 tools fit without tool search.
            env={"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": "", "ENABLE_TOOL_SEARCH": "false"})
        loop = asyncio.get_running_loop()
        async with ClaudeSDKClient(options) as sdk:
            turn.interrupt = lambda: asyncio.run_coroutine_threadsafe(sdk.interrupt(), loop)
            if turn.stopped.is_set():
                return
            await sdk.query(turn.prompt)
            async for message in sdk.receive_response():
                if isinstance(message, StreamEvent):
                    event = message.event
                    if event.get("type") == "content_block_start" and event.get("content_block", {}).get("type") == "text":
                        turn.new_text()
                    elif event.get("type") == "content_block_delta" and event["delta"].get("type") == "text_delta":
                        turn.text(event["delta"]["text"])
                elif isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            turn.commit_text(block.text)
                        elif isinstance(block, ToolUseBlock) and block.name in self.WEB_TOOLS:
                            turn.tool_start(block.name, block.input, block.id)
                elif isinstance(message, UserMessage) and isinstance(message.content, list):
                    for block in message.content:
                        if isinstance(block, ToolResultBlock):
                            turn.tool_end(block.tool_use_id, not block.is_error, web_result(block.content))
                elif isinstance(message, ResultMessage):
                    turn.store.update(turn.chat_id, session_id=message.session_id)
                    if message.is_error and not turn.stopped.is_set():
                        detail = str(message.result or message.subtype or "")[:400]
                        raise RuntimeError(f"Claude Code: {detail}" + (
                            " (Sign in again on the Connect page.)" if "auth" in detail.lower() or "log" in detail.lower() else ""))


def web_result(content) -> str:
    if isinstance(content, list):
        return "\n".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    return str(content or "")


# ----- an API key: the Messages API ---------------------------------------------------

class APIChat:
    def __init__(self, client: models.AnthropicAPI):
        self.client = client

    def web_tools(self, model: str) -> list[dict]:
        if "haiku" in model:  # The basic versions; dynamic filtering is on Sonnet and Opus.
            return [{"type": "web_search_20250305", "name": "web_search", "max_uses": 8},
                    {"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 8}]
        return [{"type": "web_search_20260209", "name": "web_search", "max_uses": 8},
                {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 8}]

    def run(self, turn: Turn, system: str, tools: list) -> None:
        api, model = self.client, turn.chat["model"]
        by_name = {t.name: t for t in tools}
        declared = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools]
        messages = json.loads(turn.chat.get("api_messages") or "[]")
        prompt = {"type": "text", "text": turn.prompt}
        if messages and messages[-1]["role"] == "user":  # The last turn was stopped before Claude answered.
            messages[-1]["content"].append(prompt)
        else:
            messages.append({"role": "user", "content": [prompt]})
        try:
            for _ in range(MAX_STEPS):
                if turn.stopped.is_set():
                    break
                api.check_budget()
                with api.client.beta.messages.stream(
                        model=model, max_tokens=16000, system=system, messages=messages,
                        tools=declared + self.web_tools(model), thinking=api.THINKING, betas=api.AGENT_BETAS,
                        context_management={"edits": [{"type": "clear_tool_uses_20250919"}]},
                        output_config={"effort": models.TASKS["assistant"][1]}) as stream:
                    for event in stream:
                        if turn.stopped.is_set():
                            break
                        if event.type == "content_block_start" and event.content_block.type == "text":
                            turn.new_text()
                        elif event.type == "content_block_delta" and event.delta.type == "text_delta":
                            turn.text(event.delta.text)
                    if turn.stopped.is_set():
                        break  # Claude's unfinished reply isn't kept.
                    response = stream.get_final_message()
                api.finished(response, model)
                messages.append({"role": "assistant",
                                 "content": [b.model_dump(exclude_none=True) for b in response.content]})
                results = []
                for block in response.content:
                    if block.type == "text":
                        turn.commit_text(block.text)
                    elif block.type == "server_tool_use":
                        turn.tool_start(block.name, block.input, block.id)
                    elif block.type.endswith("_tool_result") and hasattr(block, "tool_use_id"):
                        turn.tool_end(block.tool_use_id, "error" not in str(getattr(block.content, "type", "")),
                                      "results received")
                    elif block.type == "tool_use":
                        if turn.stopped.is_set():
                            ok, text = False, "Stopped by the user."
                        elif block.name not in by_name:
                            ok, text = False, f"Unknown tool {block.name!r}."
                        else:
                            ok, text = turn.run_tool(by_name[block.name], block.input)
                        results.append({"type": "tool_result", "tool_use_id": block.id, "content": text,
                                        **({"is_error": True} if not ok else {})})
                if results:
                    messages.append({"role": "user", "content": results})
                elif response.stop_reason != "pause_turn":  # pause_turn: a long server-tool run continues.
                    break
        finally:
            turn.store.update(turn.chat_id, api_messages=json.dumps(messages, ensure_ascii=False))


# ----- helpers ------------------------------------------------------------------------

def title_for(text: str) -> str:
    words = " ".join(text.split())
    return words[:60] + ("…" if len(words) > 60 else "")


def status_line(tracker, settings) -> str:
    counts: dict[str, int] = {}
    for job in tracker.all():
        counts[job["status"]] = counts.get(job["status"], 0) + 1
    jobs = ", ".join(f"{counts[s]} {s.replace('_', ' ')}" for s in
                     ("found", "prepared", "needs_you", "approved", "submitted") if counts.get(s))
    active = [t for t in tracker.tasks(20) if t["status"] in ("queued", "running", "waiting")]
    tasks = "; ".join(f"task {t['id']} {t['kind']}" + (f" job {t['job_id']}" if t["job_id"] else "")
                      + f" {t['status']}" for t in active[:6])
    return (f"jobs: {jobs or 'none yet'}; submitted today {tracker.submitted_today()} of "
            f"{settings.daily_submit_limit}; {'background: ' + tasks if tasks else 'no background tasks'}; "
            f"today is {datetime.now():%Y-%m-%d}")


def summarize(result, ok: bool) -> str:
    """One line for the chat: what a tool call came back with."""
    if not ok:
        return str(result)[:200]
    if isinstance(result, dict):
        if "card" in result:
            return "Waiting for your click below"
        if "diff" in result:
            return "Saved" + (", typesetting again" if "task_id" in result else "")
        if "total" in result and "jobs" in result:
            return f"{result['total']} job(s)"
        if "task_id" in result:
            return f"Task {result['task_id']} started"
        if "queued" in result:
            return f"{len(result['queued'])} queued" + (f", {len(result['problems'])} not" if result.get("problems") else "")
        if "tasks" in result and isinstance(result["tasks"], list):
            return ", ".join(f"#{t['id']} {t['status']}" for t in result["tasks"][:5]) or "No tasks"
        for key in ("message", "note", "next"):
            if isinstance(result.get(key), str):
                return result[key][:160]
        if "title" in result and "company" in result:
            return f"{result['title']} at {result['company']}"
        if "file" in result:
            return f"{len(result.get('text') or '')} characters"
    return str(result)[:160]
