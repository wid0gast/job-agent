"""The agent loop: request a response, run the tools it asks for, repeat.

Same shape as the Session 1 smallest harness, plus three things this task
needs: tool errors go back to the model so it can recover, old page snapshots
are pruned so long forms fit in context, and every step is logged to disk.
"""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .llm import OpenRouter  # noqa: F401 - re-exported for callers of the agent loop.


class StopRun(Exception):
    """Raised by a tool to end the current run with a final message."""


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., object]
    # Results of ephemeral tools (page snapshots) are pruned once superseded.
    ephemeral: bool = False

    def declaration(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def schema(properties: dict | None = None, required: list[str] | None = None) -> dict:
    properties = properties or {}
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties) if required is None else required,
        "additionalProperties": False,
    }


@dataclass
class AgentRun:
    llm: OpenRouter
    system_prompt: str
    tools: list[Tool]
    log_path: Path
    max_steps: int = 60
    keep_snapshots: int = 2
    messages: list[dict] = field(default_factory=list)
    # Receives ("text", str) and ("tool", name, args) as they happen, for a UI.
    observer: Callable[..., None] = lambda *event: None

    def __post_init__(self):
        self.by_name = {tool.name: tool for tool in self.tools}
        self.messages = [{"role": "system", "content": self.system_prompt}]
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, kind: str, data) -> None:
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"t": time.time(), "kind": kind, "data": data}) + "\n")

    def run(self, task: str) -> str:
        self.messages.append({"role": "user", "content": task})
        self.log("task", task)
        declarations = [tool.declaration() for tool in self.tools]
        for _ in range(self.max_steps):
            self.prune_snapshots()
            message = self.llm.complete(self.messages, declarations)
            message = {
                key: message[key]
                for key in ("role", "content", "tool_calls")
                if message.get(key) is not None
            }
            message["role"] = "assistant"
            self.messages.append(message)
            self.log("assistant", message)
            if message.get("content"):
                print(f"  agent: {message['content'].strip()[:400]}")
                self.observer("text", message["content"].strip())
            calls = message.get("tool_calls") or []
            if not calls:
                return message.get("content") or ""
            for call in calls:
                try:
                    result = self.execute(call)
                except StopRun as stop:
                    self.tool_result(call, {"ok": True, "result": str(stop)})
                    return str(stop)
                self.tool_result(call, result)
        return f"Stopped after {self.max_steps} steps without finishing."

    def execute(self, call: dict) -> dict:
        name = call["function"]["name"]
        tool = self.by_name.get(name)
        if tool is None:
            return {"ok": False, "error": f"Unknown tool {name!r}."}
        try:
            args = json.loads(call["function"].get("arguments") or "{}")
        except json.JSONDecodeError as error:
            return {"ok": False, "error": f"Arguments were not valid JSON: {error}"}
        print(f"  → {name}({_brief(args)})")
        self.observer("tool", name, args)
        try:
            return {"ok": True, "result": tool.fn(**args)}
        except StopRun:
            raise
        except TypeError as error:
            return {"ok": False, "error": f"Bad arguments for {name}: {error}"}
        except Exception as error:  # noqa: BLE001 - the model decides how to recover.
            return {"ok": False, "error": f"{type(error).__name__}: {error}"[:1500]}

    def tool_result(self, call: dict, result: dict) -> None:
        name = call["function"]["name"]
        self.messages.append(
            {
                "role": "tool",
                "tool_call_id": call["id"],
                "name": name,
                "content": json.dumps(result, ensure_ascii=False, default=str),
            }
        )
        self.log("tool", {"name": name, "result": result})

    def prune_snapshots(self) -> None:
        """Keep only the newest page snapshots verbatim; older ones are stale anyway."""
        ephemeral = {t.name for t in self.tools if t.ephemeral}
        indexes = [
            i
            for i, m in enumerate(self.messages)
            if m["role"] == "tool" and m.get("name") in ephemeral
        ]
        for i in indexes[: -self.keep_snapshots]:
            if not self.messages[i]["content"].startswith('{"pruned"'):
                self.messages[i]["content"] = (
                    '{"pruned": "Older page snapshot removed. Refs from it are invalid."}'
                )


def _brief(args: dict) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in args.items())
    return text if len(text) < 140 else text[:137] + "..."
