"""Model clients: Claude through your Claude Code login, or through an Anthropic API key.

Both give the app the same two calls: `json` (an answer matching a JSON Schema, for
pipeline steps) and `complete` (one agent turn with tools, for harness.AgentRun, in the
OpenAI-style message format the harness keeps).

- ClaudeCode runs the Claude Code CLI in headless mode (`claude -p`), signed in as you,
  so it uses your Claude plan (Pro or Max) with no API key. Each request is a fresh,
  unsaved session with Claude Code's own tools turned off.
- AnthropicAPI uses the Anthropic SDK with an API key (ANTHROPIC_API_KEY) or an
  `ant auth login` profile, billed per token, with the spending cap enforced here.

Each task gets a model and effort that fit it (TASKS): Haiku for bulk title ranking,
Sonnet for reading and extraction, Opus for what employers see and what decides whether
you apply. CLAUDE_MODEL forces one model everywhere; CLAUDE_MODEL_LIGHT, _STANDARD and
_BEST swap a tier.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager

from . import trace

DEFAULT_API_MODEL = "claude-opus-5-5"
# Full IDs: Claude Code's short names (haiku, sonnet) still point at older models.
TIERS = {"light": "claude-haiku-5-5", "standard": "claude-sonnet-5-5", "best": "claude-opus-5-5"}
# Task (the JSON schema name, or the agent) -> (tier, effort).
TASKS = {
    "match_scores": ("light", "low"),
    "posting_facts": ("light", "low"),         # a pasted link's title, company and place          # ~60 job titles per call; the fit check reads postings later
    "resume_markdown": ("standard", "low"),    # a PDF's text to Markdown, wording kept
    "resume_merge": ("standard", "medium"),    # which lines of several resume versions say the same thing
    "resume_facts": ("standard", "low"),       # contact, education, work authorization: used in every form
    "research": ("standard", "medium"),        # agent: reads company pages, keeps quoted facts
    "assistant": ("standard", "medium"),       # the chat (pick per chat); the writing is done by Opus steps
    "fit": ("best", "medium"),                 # decides whether you apply at all
    "tailored_resume": ("best", "medium"),     # what recruiters read; must not invent (high wrote the same page, slower)
    "cover_letter": ("best", "high"),
    "form_answers": ("best", "medium"),        # answers and essays sent to employers
    "explore": ("best", "medium"),             # agent: fills unfamiliar forms on live sites
    "discover": ("standard", "low"),           # web searches on job sites; code reads what's found
    "people": ("standard", "medium"),          # agent: finds people to contact; code checks each one
    "web_search": ("standard", "low"),         # one search through Claude's web search, results as given
    "outreach": ("best", "medium"),            # emails and LinkedIn notes to people at the company
    "first_read": ("standard", "medium"),      # a hiring manager's read: a different model from the writer's
}
DEFAULT_TASK = ("standard", "medium")
# Per million tokens (input, output), for the spending cap. Cache reads cost a tenth of input.
PRICES = {"claude-fable-5-1": (10, 50), "claude-opus-5-5": (4, 20), "claude-opus-5": (5, 25),
          "claude-sonnet-5-5": (2, 10), "claude-haiku-5-5": (0.1, 0.5)}


TRANSIENT = re.compile(r"overloaded|\b5\d\d\b|timed? ?out|took more than|connection|network|"
                       r"temporar|ECONNRESET|socket|try again", re.I)


class SpendCapReached(RuntimeError):
    pass


class Model:
    """What every client shares: model choice per task, call counting and the JSON contract."""

    model = ""  # Set: this one model for every task (CLAUDE_MODEL).
    spend_cap_usd = float("inf")

    def __init__(self, model: str = "", tiers: dict | None = None):
        self.model = model
        self.tiers = {**TIERS, **{k: v for k, v in (tiers or {}).items() if v}}
        self.spent_usd = 0.0
        self.calls = 0
        self._counting = threading.Lock()  # Match scoring calls the model from two threads.
        self._local = threading.local()

    @property
    def last_usage(self) -> dict:
        """Tokens and cost of this thread's latest request (match scoring runs on two threads)."""
        return getattr(self._local, "usage", {})

    @last_usage.setter
    def last_usage(self, value: dict) -> None:
        self._local.usage = value

    def choose(self, task: str) -> tuple[str, str]:
        """(model, effort) for a task."""
        tier, effort = TASKS.get(task, DEFAULT_TASK)
        return self.model or self.tiers[tier], effort

    def plan(self) -> dict[str, tuple[str, str]]:
        """Every task's (model, effort), for the dashboard."""
        return {task: self.choose(task) for task in TASKS}

    def count(self, cost_usd: float) -> None:
        with self._counting:
            self.calls += 1
            self.spent_usd += cost_usd

    def check_budget(self) -> None:
        if self.spent_usd >= self.spend_cap_usd:
            raise SpendCapReached(
                f"Spending cap of ${self.spend_cap_usd:.2f} reached (spent ${self.spent_usd:.4f}).")

    def json(self, system: str, user: str, schema: dict, name: str = "answer") -> dict:
        """A JSON object matching `schema`, asked again once if the reply doesn't fit."""
        error = ""
        for attempt in range(2):
            prompt = user if not attempt else (
                f"{user}\n\nYour previous reply was not valid ({error}). Reply with the corrected JSON object.")
            try:
                value = self._json(system, prompt, schema, name)
                check(value, schema)
                return value
            except ValueError as problem:
                error = str(problem)
                trace.check("Reply has the expected shape", False,
                            f"{error}; " + ("asked again." if not attempt else "gave up."))
        raise ValueError(f"Model did not return valid JSON: {error}")

    def _json(self, system: str, user: str, schema: dict, name: str) -> dict:
        raise NotImplementedError

    def complete(self, messages: list[dict], tools: list[dict], task: str = "agent") -> dict:
        raise NotImplementedError

    def web_json(self, system: str, user: str, schema: dict, task: str, domains: list[str],
                 max_searches: int) -> dict:
        """Like json(), but Claude may run web searches first (limited to `domains`, if any)."""
        raise NotImplementedError


# ----- Claude Code (your Claude plan) -------------------------------------------

class ClaudeCode(Model):
    # Agent turns: the reply is a short thought plus the tool calls to make.
    TURN_PROMPT = """

You are working through tools. Reply with a JSON object: "thought" (a sentence or two on
what you're doing, shown to the user) and "tool_calls" (the calls to make now, each a tool
"name" and its "arguments" object, in order). Call only the tools listed below, with
arguments that match their schemas. Your own built-in tools are switched off: a tool
runs only when you list it in tool_calls, and its result arrives in the next message.
When the work is done, call the finishing tool.

Tools:
"""

    def __init__(self, model: str = "", tiers: dict | None = None):
        super().__init__(model, tiers)
        self.executable = shutil.which("claude")
        if not self.executable:
            raise RuntimeError("Claude Code isn't installed: see https://code.claude.com/docs/en/setup")
        # A quiet folder, so no project's CLAUDE.md or settings shape the answers.
        from . import config

        self.workdir = config.DATA_DIR / "claude-code"
        self.workdir.mkdir(parents=True, exist_ok=True)

    def run(self, system: str, prompt: str, schema: dict, task: str, tools: tuple[str, ...] = (),
            turn: bool = False) -> dict:
        """One headless request; recorded in the job's trace (trace.py) when one is open."""
        model, effort = self.choose(task)
        for attempt in range(2):
            try:
                with recorded(self, task, model, effort, system, prompt, turn) as done:
                    value = self._run(system, prompt, schema, model, effort, tools)
                    done.update(text=json.dumps(value, ensure_ascii=False))
                return value
            except RuntimeError as error:
                # A momentary failure (overloaded, network, timeout) gets one more try; a usage
                # limit or a sign-in problem won't fix itself in seconds, so those go straight back.
                if attempt or not TRANSIENT.search(str(error)):
                    raise
                time.sleep(8)
        raise AssertionError("unreachable")

    def _run(self, system: str, prompt: str, schema: dict, model: str, effort: str, tools: tuple[str, ...]) -> dict:
        allowed = ",".join(tools)  # Claude Code's own tools: none, except web search for discovery.
        command = [self.executable, "-p", "--output-format", "json", "--tools", allowed,
                   *(["--allowedTools", allowed] if tools else []),
                   "--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands",
                   "--system-prompt", system, "--json-schema", json.dumps(schema),
                   "--model", model, "--effort", effort]
        # An API key in the environment would take precedence over your Claude login.
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        try:
            done = subprocess.run(command, input=prompt, capture_output=True, text=True,
                                  timeout=600, cwd=self.workdir, env=env)
        except subprocess.TimeoutExpired:
            raise RuntimeError("Claude Code took more than 10 minutes to answer.") from None
        try:
            reply = json.loads(done.stdout)
        except json.JSONDecodeError:
            detail = (done.stderr or done.stdout).strip()[-400:]
            raise RuntimeError(f"Claude Code failed (exit {done.returncode}): {detail}") from None
        # Your plan pays, so this is a notional cost: shown, never capped.
        self.count(float(reply.get("total_cost_usd") or 0))
        usage = reply.get("usage") or {}
        self.last_usage = {"input": (usage.get("input_tokens") or 0) + (usage.get("cache_read_input_tokens") or 0)
                           + (usage.get("cache_creation_input_tokens") or 0),
                           "output": usage.get("output_tokens") or 0,
                           "cost_usd": float(reply.get("total_cost_usd") or 0), "plan": True}
        if reply.get("is_error") or reply.get("subtype") != "success":
            message = str(reply.get("result") or reply.get("subtype"))[:400]
            if re.search(r"log ?in|auth|credential", message, re.I):
                message += " (Run `claude` in a terminal and sign in with your Claude account.)"
            raise RuntimeError(f"Claude Code: {message}")
        if "structured_output" not in reply:
            return parse_json(str(reply.get("result") or ""))
        return reply["structured_output"]

    def _json(self, system: str, user: str, schema: dict, name: str) -> dict:
        return self.run(system, user, schema, name)

    def web_json(self, system: str, user: str, schema: dict, task: str, domains: list[str],
                 max_searches: int) -> dict:
        prompt = f"{user}\n\nUse the WebSearch tool, at most {max_searches} times" + (
            f", always with allowed_domains set to {json.dumps(domains)} (site: in the query is ignored)."
            if domains else ".")
        value = self.run(system, prompt, schema, task, tools=("WebSearch",))
        check(value, schema)
        return value

    def complete(self, messages: list[dict], tools: list[dict], task: str = "agent") -> dict:
        names = [t["function"]["name"] for t in tools]
        schema = {"type": "object", "additionalProperties": False, "required": ["thought", "tool_calls"],
                  "properties": {
                      "thought": {"type": "string"},
                      "tool_calls": {"type": "array", "items": {
                          "type": "object", "required": ["name", "arguments"], "additionalProperties": False,
                          "properties": {"name": {"type": "string", "enum": names},
                                         "arguments": {"type": "object"}}}}}}
        system = messages[0]["content"] + self.TURN_PROMPT + "\n".join(
            json.dumps({"name": t["function"]["name"], "description": t["function"]["description"],
                        "arguments": t["function"]["parameters"]}, ensure_ascii=False) for t in tools)
        reply = self.run(system, transcript(messages[1:]), schema, task, turn=True)
        return {"content": reply.get("thought") or None,
                "tool_calls": [{"id": f"call_{uuid.uuid4().hex[:12]}", "type": "function",
                                "function": {"name": c["name"],
                                             "arguments": json.dumps(c.get("arguments") or {})}}
                               for c in reply.get("tool_calls") or []]}


def transcript(messages: list[dict]) -> str:
    """The conversation so far, written out for a fresh headless session."""
    parts = []
    for m in messages:
        if m["role"] == "user":
            parts.append(f"## User\n{m['content']}")
        elif m["role"] == "assistant":
            calls = "\n".join(f"- {c['function']['name']}({c['function']['arguments']})  [{c['id']}]"
                              for c in m.get("tool_calls") or [])
            parts.append(f"## You\n{m.get('content') or ''}\n{calls}".strip())
        elif m["role"] == "tool":
            parts.append(f"## Result of {m.get('name')} [{m['tool_call_id']}]\n{m['content']}")
    parts.append("## Next\nDecide your next tool calls.")
    return "\n\n".join(parts)


# ----- Anthropic API (API key) --------------------------------------------------

class AnthropicAPI(Model):
    # Agent runs keep Claude's thinking blocks and let the API clear old tool results
    # (server-side context editing) rather than edit the history, which would invalidate them.
    prunes_context = True
    AGENT_BETAS = ["context-management-2025-06-27", "thinking-binding-controls-2026-08-01"]
    THINKING = {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}

    def __init__(self, model: str = "", spend_cap_usd: float = 1.0, tiers: dict | None = None):
        super().__init__(model, tiers)
        import anthropic

        self.spend_cap_usd = spend_cap_usd
        self.client = anthropic.Anthropic(max_retries=4)

    def cost(self, usage, model: str) -> float:
        price_in, price_out = PRICES.get(model, PRICES[DEFAULT_API_MODEL])
        fresh = (usage.input_tokens or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0) * 1.25
        cached = (getattr(usage, "cache_read_input_tokens", 0) or 0) * 0.1
        searches = getattr(getattr(usage, "server_tool_use", None), "web_search_requests", 0) or 0
        return ((fresh + cached) * price_in + (usage.output_tokens or 0) * price_out) / 1e6 + searches * 0.01

    def finished(self, response, model: str) -> None:
        cost = self.cost(response.usage, model)
        self.count(cost)
        usage = response.usage
        self.last_usage = {"input": (usage.input_tokens or 0) + (getattr(usage, "cache_read_input_tokens", 0) or 0)
                           + (getattr(usage, "cache_creation_input_tokens", 0) or 0),
                           "output": usage.output_tokens or 0, "cost_usd": cost}
        if response.stop_reason == "refusal":
            raise RuntimeError("Claude declined this request.")
        if response.stop_reason == "max_tokens":
            raise RuntimeError("Claude's answer was cut off at the output limit.")

    def _json(self, system: str, user: str, schema: dict, name: str) -> dict:
        self.check_budget()
        model, effort = self.choose(name)
        with recorded(self, name, model, effort, system, user) as done:
            done.update(text=self._api_json(system, user, schema, model, effort))
        return parse_json(done["text"])

    def _api_json(self, system: str, user: str, schema: dict, model: str, effort: str) -> str:
        response = self.client.beta.messages.create(
            model=model, max_tokens=16000, system=cached(system),
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": schema}, "effort": effort},
            # If a safety check declines, the API retries on a suitable model itself.
            **({"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
               if model in FALLBACK_MODELS else {}))
        self.finished(response, model)
        return next((b.text for b in response.content if b.type == "text"), "")

    def web_json(self, system: str, user: str, schema: dict, task: str, domains: list[str],
                 max_searches: int) -> dict:
        self.check_budget()
        model, effort = self.choose(task)
        with recorded(self, task, model, effort, system, user) as done:
            done.update(text=self._api_web(system, user, schema, model, effort, domains, max_searches))
        value = parse_json(done["text"])
        check(value, schema)
        return value

    def _api_web(self, system, user, schema, model, effort, domains, max_searches) -> str:
        messages = [{"role": "user", "content": user}]
        for _ in range(4):  # A long run of searches can pause; sending it back continues it.
            response = self.client.messages.create(
                model=model, max_tokens=16000, system=system, messages=messages, cache_control=CACHE,
                output_config={"format": {"type": "json_schema", "schema": schema}, "effort": effort},
                tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches,
                        **({"allowed_domains": domains} if domains else {})}])
            if response.stop_reason != "pause_turn":
                break
            self.count(self.cost(response.usage, model))
            messages = [*messages, {"role": "assistant", "content": response.content}]
        self.finished(response, model)
        return next((b.text for b in reversed(response.content) if b.type == "text"), "")

    def complete(self, messages: list[dict], tools: list[dict], task: str = "agent") -> dict:
        self.check_budget()
        model, effort = self.choose(task)  # One task per agent run, so one model per conversation.
        with recorded(self, task, model, effort, turn=True) as done:
            response = self._api_turn(messages, tools, model, effort)
            done.update(text=json.dumps([b.model_dump(exclude_none=True) for b in response.content
                                         if b.type in ("text", "tool_use")], ensure_ascii=False))
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        return {"content": text or None,
                "tool_calls": [{"id": b.id, "type": "function",
                                "function": {"name": b.name, "arguments": json.dumps(b.input)}}
                               for b in response.content if b.type == "tool_use"],
                # Replayed exactly as received, thinking blocks included.
                "provider_content": [b.model_dump(exclude_none=True) for b in response.content]}

    def _api_turn(self, messages: list[dict], tools: list[dict], model: str, effort: str):
        response = self.client.beta.messages.create(
            model=model, max_tokens=16000, system=messages[0]["content"], output_config={"effort": effort},
            cache_control=CACHE,  # Each turn resends the conversation so far: read it from the cache.
            messages=to_anthropic(messages[1:]), thinking=self.THINKING, betas=self.AGENT_BETAS,
            context_management={"edits": [{"type": "clear_tool_uses_20250919"}]},
            tools=[{"name": t["function"]["name"], "description": t["function"]["description"],
                    "input_schema": t["function"]["parameters"]} for t in tools])
        self.finished(response, model)
        return response


# Prompt caching (API key only; Claude Code caches by itself). A cached prefix costs a tenth
# of fresh input to read back, and a quarter more to write once.
CACHE = {"type": "ephemeral"}


def cached(system: str) -> list[dict]:
    """Standing instructions marked for caching: the same text starts every request of a step."""
    return [{"type": "text", "text": system, "cache_control": CACHE}]


@contextmanager
def recorded(client, task: str, model: str, effort: str, system: str = "", prompt: str = "", turn: bool = False):
    """Time one request and write it to the open trace (with its reply, or the error)."""
    done, started = {}, time.time()
    try:
        yield done
    except Exception as error:
        trace.model_call(task, model, effort, time.time() - started, system, prompt, error=str(error), turn=turn)
        raise
    trace.model_call(task, model, effort, time.time() - started, system, prompt, done.get("text", ""),
                     getattr(client, "last_usage", {}), turn=turn)


# Server-side refusal fallbacks ("default" routing) are offered on these models.
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}


def to_anthropic(messages: list[dict]) -> list[dict]:
    """The harness's messages as Messages API turns; consecutive tool results share one user turn."""
    out: list[dict] = []
    for m in messages:
        if m["role"] == "assistant":
            out.append({"role": "assistant", "content": m["provider_content"]})
        elif m["role"] == "tool":
            block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
        else:
            out.append({"role": "user", "content": [{"type": "text", "text": m["content"]}]})
    return out


def has_api_credentials() -> bool:
    """An API key, or a profile from `ant auth login` (which the SDK reads by itself)."""
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                or os.environ.get("ANTHROPIC_PROFILE") or os.path.isdir(os.path.expanduser("~/.config/anthropic")))


def client(settings):
    """The model for this run, or None if none is set up.

    auto: Claude Code when it's installed (your plan, no key), else the Anthropic API
    when credentials are present."""
    backend = settings.backend
    if backend == "off":
        return None
    if backend == "claude-code" or (backend == "auto" and shutil.which("claude")):
        return ClaudeCode(settings.model, settings.tier_models)
    if backend == "anthropic" or (backend == "auto" and has_api_credentials()):
        return AnthropicAPI(settings.model, settings.spend_cap_usd, settings.tier_models)
    return None


NOT_SET_UP = ("No Claude connection: install Claude Code and sign in with your Claude account "
              "(https://code.claude.com/docs/en/setup), or set ANTHROPIC_API_KEY in .env.")


def parse_json(text: str):
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    start = text.find("{")
    if start > 0:
        text = text[start:]
    try:
        return json.JSONDecoder().raw_decode(text)[0]
    except json.JSONDecodeError as error:
        raise ValueError(f"not JSON ({error})") from None


def check(value, schema: dict, path: str = "$") -> None:
    """A small JSON Schema check: types, required keys, enums, nested objects and arrays."""
    kind = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int,
             "number": (int, float), "boolean": bool}
    if kind in types and not isinstance(value, types[kind]):
        raise ValueError(f"{path} should be {kind}")
    if kind in ("integer", "number") and isinstance(value, bool):
        raise ValueError(f"{path} should be {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} must be one of {schema['enum']}")
    if kind == "object":
        for key in schema.get("required", []):
            if key not in value:
                raise ValueError(f"{path}.{key} is missing")
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                check(value[key], sub, f"{path}.{key}")
    if kind == "array" and "items" in schema:
        for index, item in enumerate(value):
            check(item, schema["items"], f"{path}[{index}]")
