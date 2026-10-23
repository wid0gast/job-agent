"""Traces: what each run on a job did, for the job's "How it was made" page.

A run (prepare, finding people, submit...) is a list of entries in a JSONL file:
steps (with a duration and a one-line outcome), model calls (model, effort, time,
tokens, and the full prompt and reply), checks (a guard in code that passed or
caught something, with the evidence), agent runs (linked to their turn-by-turn
trace) and notes. The same split observability tools use (Langfuse's spans,
generations, tools and guardrails; OpenTelemetry's GenAI conventions).

Code records into whatever run is current, so the pipeline's functions keep their
shape: `with trace.step("Tailoring the resume"):` and `trace.check(...)`, while
llm.py records every model call by itself. With no run open, all of it is a no-op.
Everything stays on your computer, under data/traces/<job id>/.
"""

import contextvars
import json
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

KEEP_RUNS = 8           # Per job: older traces are deleted.
MAX_TEXT = 60_000       # A prompt or reply longer than this is cut in the trace.
KINDS = {"prepare": "Prepare", "people": "Find people to contact", "redraft": "Rewrite messages",
         "render": "Typeset documents", "letter": "Write a cover letter", "draft": "Draft missing answers", "submit": "Submit",
         "first_read": "A hiring manager's first read"}

_current: contextvars.ContextVar = contextvars.ContextVar("trace", default=None)


def folder(job_id: int) -> Path:
    from . import config

    return config.DATA_DIR / "traces" / str(job_id)


class Run:
    def __init__(self, kind: str, job_id: int):
        self.kind, self.job_id = kind, job_id
        self.id = f"{datetime.now():%Y%m%d-%H%M%S}-{kind}"
        self.path = folder(job_id) / f"{self.id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stack: list[int] = []
        self.count = 0
        self.lock = threading.Lock()

    def write(self, type_: str, **data) -> int:
        with self.lock:
            self.count += 1
            entry = {"n": self.count, "t": round(time.time(), 3), "type": type_,
                     "parent": self.stack[-1] if self.stack else None, **data}
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            return self.count


class Step:
    """Yielded by step(): set .summary for the step's one-line outcome."""

    def __init__(self):
        self.summary = ""


@contextmanager
def run(kind: str, job_id: int | None):
    """Open a trace for one run on a job (nested runs join the outer one)."""
    if job_id is None or _current.get() is not None:
        yield None
        return
    current = Run(kind, job_id)
    token = _current.set(current)
    started = time.time()
    current.write("run", kind=kind, job_id=job_id)
    outcome = {"status": "done"}
    try:
        yield outcome
    except BaseException as error:
        outcome = {"status": "failed", "detail": f"{type(error).__name__}: {error}"[:500]}
        raise
    finally:
        current.write("end_run", seconds=round(time.time() - started, 1), **outcome)
        _current.reset(token)
        prune(job_id)


@contextmanager
def step(name: str):
    current = _current.get()
    handle = Step()
    if current is None:
        yield handle
        return
    number = current.write("step", name=name)
    current.stack.append(number)
    started, error = time.time(), None
    try:
        yield handle
    except BaseException as failure:
        error = f"{type(failure).__name__}: {failure}"[:500]
        raise
    finally:
        current.stack.pop()
        current.write("end", of=number, seconds=round(time.time() - started, 1),
                      summary=handle.summary, error=error)


def check(name: str, passed: bool, detail: str = "") -> None:
    """A guard in code: passed, or caught something (detail says what)."""
    current = _current.get()
    if current is not None:
        current.write("check", name=name, passed=bool(passed), detail=str(detail)[:1500])


def note(text: str) -> None:
    current = _current.get()
    if current is not None:
        current.write("note", text=str(text)[:1500])


def model_call(task: str, model: str, effort: str, seconds: float, system: str = "", prompt: str = "",
               reply: str = "", usage: dict | None = None, error: str = "", turn: bool = False) -> None:
    """One request to the model. Agent turns keep only the reply (their trace has the rest)."""
    current = _current.get()
    if current is None:
        return
    current.write("model", task=task, model=model, effort=effort, seconds=round(seconds, 1),
                  usage=usage or {}, error=error[:1000], turn=turn,
                  system="" if turn else system[:MAX_TEXT], prompt="" if turn else prompt[:MAX_TEXT],
                  reply=reply[:MAX_TEXT])


def agent(name: str, start_line: int, steps: int, report: str) -> None:
    """An agent's run, linked to its turn-by-turn trace (data/runs/<name>.jsonl from start_line)."""
    current = _current.get()
    if current is not None:
        current.write("agent", name=name, start=start_line, steps=steps, report=str(report)[:1500])


def active() -> bool:
    return _current.get() is not None


# ----- reading ---------------------------------------------------------------------

def runs(job_id: int) -> list[Path]:
    """This job's traces, newest first."""
    path = folder(job_id)
    return sorted(path.glob("*.jsonl"), reverse=True) if path.exists() else []


def recent(limit: int = 30) -> list[Path]:
    from . import config

    root = config.DATA_DIR / "traces"
    if not root.exists():
        return []
    return sorted(root.glob("*/*.jsonl"), key=lambda p: p.name, reverse=True)[:limit]


def prune(job_id: int) -> None:
    for old in runs(job_id)[KEEP_RUNS:]:
        old.unlink(missing_ok=True)


def read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def tree(entries: list[dict]) -> dict:
    """Entries as a tree of steps, each with its children in order, plus the run's totals."""
    nodes = {0: {"type": "root", "children": []}}
    totals = {"calls": 0, "turns": 0, "input": 0, "output": 0, "cost": 0.0, "passed": 0, "caught": 0}
    head, tail = {}, {}
    for entry in entries:
        kind = entry["type"]
        if kind == "run":
            head = entry
            continue
        if kind == "end_run":
            tail = entry
            continue
        if kind == "end":
            nodes.get(entry["of"], {}).update(seconds=entry["seconds"], summary=entry.get("summary"),
                                              error=entry.get("error"), done=True)
            continue
        node = {**entry, "children": []}
        nodes[entry["n"]] = node
        nodes.get(entry["parent"] or 0, nodes[0])["children"].append(node)
        if kind == "model":
            totals["turns" if entry.get("turn") else "calls"] += 1
            usage = entry.get("usage") or {}
            totals["input"] += usage.get("input", 0)
            totals["output"] += usage.get("output", 0)
            totals["cost"] += usage.get("cost_usd", 0) or 0
            totals["plan"] = totals.get("plan") or bool(usage.get("plan"))
        if kind == "check":
            totals["passed" if entry["passed"] else "caught"] += 1
    return {"run": head, "end": tail, "root": nodes[0], "totals": totals}
