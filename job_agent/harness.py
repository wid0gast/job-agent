"""The agent loop: request a response, run the tools it asks for, repeat.

Tool errors go back to the model so it can recover, superseded page snapshots
are pruned so long runs fit in context, and every step is written to a JSONL
trace that the dashboard shows. A run ends when a tool raises StopRun (the
agent's `finish`), or at the step or time limit.
"""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import stopping, trace


class StopRun(Exception):
    """Raised by a tool to end the run with a final message."""


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., object]
    # Results of ephemeral tools (page snapshots) are pruned once superseded.
    ephemeral: bool = False
    # Changes nothing (told to the chat's MCP host as readOnlyHint).
    read_only: bool = False
    # A guard in code (e.g. "Fact is quoted from a page it read"): each call is a check in the job's trace.
    guard: str = ""

    def declaration(self) -> dict:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description,
                             "parameters": self.parameters}}


TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None),
         "integer": int, "number": (int, float)}


def argument_problems(schema: dict, value, where: str = "arguments", strict: bool = True) -> list[str]:
    """What's wrong with a value against a JSON Schema (the parts the app uses: type, enum,
    required, items, minItems, and with `strict` no extra properties). Tool calls are checked
    strictly before anything runs; a model's JSON reply (llm.check) may carry extra keys."""
    kinds = schema.get("type")
    if kinds:
        kinds = [kinds] if isinstance(kinds, str) else kinds
        if not any(isinstance(value, TYPES[k]) and not (k in ("integer", "number") and isinstance(value, bool))
                   for k in kinds):
            return [f"{where} should be {' or '.join(kinds)}, not {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{where} should be one of {', '.join(map(str, schema['enum']))}"]
    problems = []
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        problems += [f"{where} is missing {name!r}" for name in schema.get("required", []) if name not in value]
        if strict and schema.get("additionalProperties") is False:
            problems += [f"{where} has an unknown field {name!r}" for name in value if name not in properties]
        for name, item in value.items():
            if name in properties:
                problems += argument_problems(properties[name], item, f"{name!r}", strict)
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            problems.append(f"{where} needs at least {schema['minItems']} item(s)")
        if "items" in schema:
            for i, item in enumerate(value):
                problems += argument_problems(schema["items"], item, f"{where}[{i}]", strict)
    return problems


def trace_path(name: int | str) -> Path:
    """Where an agent run's trace lives (data/runs/<name>.jsonl)."""
    from . import config

    return config.DATA_DIR / "runs" / f"{name}.jsonl"


def schema(properties: dict | None = None, required: list[str] | None = None) -> dict:
    properties = properties or {}
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required,
            "additionalProperties": False}


@dataclass
class AgentRun:
    llm: object  # Anything with complete(messages, tools, task) -> assistant message.
    system_prompt: str
    tools: list[Tool]
    trace_path: Path
    task: str = "agent"  # Picks the model and effort (llm.TASKS).
    max_steps: int = 40
    max_seconds: float = 20 * 60
    keep_snapshots: int = 2
    messages: list[dict] = field(default_factory=list)
    # Called with each trace entry as it's written (kind, data), e.g. for a log.
    observer: Callable[[str, object], None] = lambda kind, data: None

    def __post_init__(self):
        self.by_name = {tool.name: tool for tool in self.tools}
        self.messages = [{"role": "system", "content": self.system_prompt}]
        self.trace_path.parent.mkdir(parents=True, exist_ok=True)

    def trace(self, kind: str, data) -> None:
        with self.trace_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"t": time.time(), "kind": kind, "data": data},
                                    ensure_ascii=False, default=str) + "\n")
        self.observer(kind, data)

    def run(self, task: str) -> str:
        self.start_line = len(read_trace(self.trace_path))  # This run's part of the trace file.
        self.steps, self.used_tools, self.reminded = 0, False, False
        self.messages.append({"role": "user", "content": task})
        self.trace("task", task)
        declarations = [tool.declaration() for tool in self.tools]
        deadline, nudged = time.time() + self.max_seconds, False
        for _ in range(self.max_steps):
            if time.time() > deadline:
                return self.end(f"Stopped at the time limit ({self.max_seconds / 60:.0f} minutes).")
            stopping.check()  # Each turn is a step boundary.
            if not getattr(self.llm, "prunes_context", False):  # Else the API clears old results itself.
                self.prune_snapshots()
            self.steps += 1
            message = self.llm.complete(self.messages, declarations, task=self.task)
            # provider_content: the model's reply as sent, replayed unchanged (thinking blocks included).
            message = {k: message[k] for k in ("content", "tool_calls", "provider_content") if message.get(k)}
            message["role"] = "assistant"
            self.messages.append(message)
            if message.get("content"):
                self.trace("thought", message["content"].strip())
            calls = message.get("tool_calls") or []
            if not calls:
                if nudged:  # Answered twice without tools: take the text as its report.
                    return self.end(message.get("content") or "Stopped without a report.")
                nudged = True
                self.messages.append({"role": "user", "content":
                                      "Continue with tool calls, or call finish if you're done."})
                continue
            checked = {call["id"]: self.check(call) for call in calls}  # The whole batch, before any runs.
            for call in calls:
                if checked[call["id"]]:
                    self.tool_result(call, checked[call["id"]])
                    continue
                if call["function"]["name"] == "finish" and not self.used_tools and not self.reminded:
                    # Finishing before any tool ran usually means it tried a tool of its own
                    # (none are on) and gave up. Say how tools work, once.
                    self.reminded = True
                    self.tool_result(call, {"ok": False, "error": (
                        "No tool has run yet in this task. Tools run only when you list them in your reply's "
                        "tool calls (for example web_search), and their results come back to you. Do the work "
                        "with them, then finish.")})
                    continue
                try:
                    result = self.execute(call)
                except StopRun as stop:
                    self.tool_result(call, {"ok": True, "result": str(stop)})
                    return self.end(str(stop))
                self.used_tools = True
                self.tool_result(call, result)
        return self.end(f"Stopped after {self.max_steps} steps without finishing.")

    def end(self, report: str) -> str:
        self.trace("finish", report)
        trace.agent(self.trace_path.stem, getattr(self, "start_line", 0), getattr(self, "steps", 0), report)
        return report

    def check(self, call: dict) -> dict | None:
        """Why this call can't run (unknown tool, arguments not JSON or not matching the tool's
        schema), as the result the model gets back; None when it may run."""
        name, raw = call["function"]["name"], call["function"].get("arguments") or "{}"
        tool = self.by_name.get(name)
        try:
            args = json.loads(raw)
        except json.JSONDecodeError as error:
            args, problems = raw, [f"arguments were not valid JSON ({error})"]
        else:
            problems = [f"unknown tool {name!r}"] if tool is None else argument_problems(tool.parameters, args)
        if not problems:
            return None
        self.trace("call", {"name": name, "args": args, "refused": problems})
        return {"ok": False, "error": f"Not run: {'; '.join(problems)}."}

    def execute(self, call: dict) -> dict:
        name = call["function"]["name"]
        tool = self.by_name[name]
        args = json.loads(call["function"].get("arguments") or "{}")
        self.trace("call", {"name": name, "args": args})
        what = "; ".join(str(v)[:120] for v in list(args.values())[:2])
        try:
            result = {"ok": True, "result": tool.fn(**args)}
            if tool.guard:
                trace.check(tool.guard, True, f"Kept: {what}")
            return result
        except StopRun:
            raise
        except Exception as error:  # noqa: BLE001 - the model decides how to recover.
            if tool.guard and isinstance(error, ValueError):
                trace.check(tool.guard, False, f"Refused {what}: {error}")
            return {"ok": False, "error": f"{type(error).__name__}: {error}"[:1500]}

    def tool_result(self, call: dict, result: dict) -> None:
        name = call["function"]["name"]
        self.messages.append({"role": "tool", "tool_call_id": call["id"], "name": name,
                              "content": json.dumps(result, ensure_ascii=False, default=str)})
        self.trace("result", {"name": name, **result})

    def prune_snapshots(self) -> None:
        """Keep only the newest page snapshots verbatim; older ones are stale anyway."""
        ephemeral = {t.name for t in self.tools if t.ephemeral}
        indexes = [i for i, m in enumerate(self.messages)
                   if m["role"] == "tool" and m.get("name") in ephemeral]
        for i in indexes[: -self.keep_snapshots]:
            if not self.messages[i]["content"].startswith('{"pruned"'):
                self.messages[i]["content"] = '{"pruned": "Older page snapshot removed. Refs from it are invalid."}'


def read_trace(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
