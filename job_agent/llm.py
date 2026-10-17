"""Model clients: Claude through your Claude Code login, or through an Anthropic API key.

Both give the app the same two calls: `json` (an answer matching a JSON Schema, for
pipeline steps) and `complete` (one agent turn with tools, for harness.AgentRun, in the
OpenAI-style message format the harness keeps).

- ClaudeCode runs the Claude Code CLI in headless mode (`claude -p`), signed in as you,
  so it uses your Claude plan (Pro or Max) with no API key. Each request is a fresh,
  unsaved session with Claude Code's own tools turned off.
- AnthropicAPI uses the Anthropic SDK with an API key (ANTHROPIC_API_KEY) or an
  `ant auth login` profile, billed per token, with the spending cap enforced here.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import uuid

DEFAULT_API_MODEL = "claude-opus-5-5"
# Per million tokens (input, output), for the spending cap. Cache reads cost a tenth of input.
PRICES = {"claude-fable-5-1": (10, 50), "claude-opus-5-5": (4, 20), "claude-opus-5": (5, 25),
          "claude-sonnet-5-5": (2, 10), "claude-haiku-5-5": (0.1, 0.5)}


class SpendCapReached(RuntimeError):
    pass


class Model:
    """What every client shares: call counting and the JSON-answer contract."""

    model = ""
    spend_cap_usd = float("inf")

    def __init__(self):
        self.spent_usd = 0.0
        self.calls = 0
        self._counting = threading.Lock()  # Match scoring calls the model from two threads.

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
        raise ValueError(f"Model did not return valid JSON: {error}")

    def _json(self, system: str, user: str, schema: dict, name: str) -> dict:
        raise NotImplementedError

    def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        raise NotImplementedError


# ----- Claude Code (your Claude plan) -------------------------------------------

class ClaudeCode(Model):
    # Agent turns: the reply is a short thought plus the tool calls to make.
    TURN_PROMPT = """

You are working through tools. Reply with a JSON object: "thought" (a sentence or two on
what you're doing, shown to the user) and "tool_calls" (the calls to make now, each a tool
"name" and its "arguments" object, in order). Call only the tools listed below, with
arguments that match their schemas. When the work is done, call the finishing tool.

Tools:
"""

    def __init__(self, model: str = ""):
        super().__init__()
        self.model = model  # Empty: Claude Code's default model for your plan.
        self.executable = shutil.which("claude")
        if not self.executable:
            raise RuntimeError("Claude Code isn't installed: see https://code.claude.com/docs/en/setup")
        # A quiet folder, so no project's CLAUDE.md or settings shape the answers.
        from . import config

        self.workdir = config.DATA_DIR / "claude-code"
        self.workdir.mkdir(parents=True, exist_ok=True)

    def run(self, system: str, prompt: str, schema: dict) -> dict:
        command = [self.executable, "-p", "--output-format", "json", "--tools", "",
                   "--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands",
                   "--system-prompt", system, "--json-schema", json.dumps(schema)]
        if self.model:
            command += ["--model", self.model]
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
        if reply.get("is_error") or reply.get("subtype") != "success":
            message = str(reply.get("result") or reply.get("subtype"))[:400]
            if re.search(r"log ?in|auth|credential", message, re.I):
                message += " (Run `claude` in a terminal and sign in with your Claude account.)"
            raise RuntimeError(f"Claude Code: {message}")
        if "structured_output" not in reply:
            return parse_json(str(reply.get("result") or ""))
        return reply["structured_output"]

    def _json(self, system: str, user: str, schema: dict, name: str) -> dict:
        return self.run(system, user, schema)

    def complete(self, messages: list[dict], tools: list[dict]) -> dict:
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
        reply = self.run(system, transcript(messages[1:]), schema)
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

    def __init__(self, model: str = "", spend_cap_usd: float = 1.0):
        super().__init__()
        import anthropic

        self.model = model or DEFAULT_API_MODEL
        self.spend_cap_usd = spend_cap_usd
        self.client = anthropic.Anthropic(max_retries=4)

    def cost(self, usage) -> float:
        price_in, price_out = PRICES.get(self.model, PRICES[DEFAULT_API_MODEL])
        fresh = (usage.input_tokens or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0) * 1.25
        cached = (getattr(usage, "cache_read_input_tokens", 0) or 0) * 0.1
        return ((fresh + cached) * price_in + (usage.output_tokens or 0) * price_out) / 1e6

    def finished(self, response) -> None:
        self.count(self.cost(response.usage))
        if response.stop_reason == "refusal":
            raise RuntimeError("Claude declined this request.")
        if response.stop_reason == "max_tokens":
            raise RuntimeError("Claude's answer was cut off at the output limit.")

    def _json(self, system: str, user: str, schema: dict, name: str) -> dict:
        self.check_budget()
        response = self.client.beta.messages.create(
            model=self.model, max_tokens=16000, system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": schema}},
            # If a safety check declines, the API retries on a suitable model itself.
            betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        self.finished(response)
        return parse_json(next((b.text for b in response.content if b.type == "text"), ""))

    def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        self.check_budget()
        response = self.client.beta.messages.create(
            model=self.model, max_tokens=16000, system=messages[0]["content"],
            messages=to_anthropic(messages[1:]), thinking=self.THINKING, betas=self.AGENT_BETAS,
            context_management={"edits": [{"type": "clear_tool_uses_20250919"}]},
            tools=[{"name": t["function"]["name"], "description": t["function"]["description"],
                    "input_schema": t["function"]["parameters"]} for t in tools])
        self.finished(response)
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        return {"content": text or None,
                "tool_calls": [{"id": b.id, "type": "function",
                                "function": {"name": b.name, "arguments": json.dumps(b.input)}}
                               for b in response.content if b.type == "tool_use"],
                # Replayed exactly as received, thinking blocks included.
                "provider_content": [b.model_dump(exclude_none=True) for b in response.content]}


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
        return ClaudeCode(settings.model)
    if backend == "anthropic" or (backend == "auto" and has_api_credentials()):
        return AnthropicAPI(settings.model, settings.spend_cap_usd)
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
