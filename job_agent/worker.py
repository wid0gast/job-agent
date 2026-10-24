"""The one process that owns the browser. The dashboard queues tasks; this runs them.

Playwright's sync API isn't thread-safe and Chrome locks its profile, so a
lock file guarantees a single worker. When a task needs you (a code, a
CAPTCHA), the question goes on the task row and the worker waits for your
reply from the dashboard.
"""

import fcntl
import os
import threading
import time
import traceback
from contextlib import contextmanager
from pathlib import Path

from . import config, stopping
from .llm import NOT_SET_UP
from .tracker import Tracker

DB_PATH = config.DATA_DIR / "applications.db"
BROWSER_PROFILE = config.DATA_DIR / "browser-profile"
LOCK_PATH = config.DATA_DIR / "worker.lock"
ANSWER_TIMEOUT = 15 * 60


class Busy(RuntimeError):
    pass


@contextmanager
def browser_lock():
    """Held by whoever drives Chrome: the worker, or a CLI command."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = LOCK_PATH.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise Busy("Another job-agent process (the dashboard's worker?) is using the browser.") from None
    try:
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


class Context:
    """Shared by pipeline steps: settings, tracker, model, HTTP and browsers."""

    def __init__(self, settings, tracker: Tracker, require_model: bool = False,
                 profile_dir=None, headless_window: bool = False):
        from playwright.sync_api import sync_playwright

        from .browser import Headless
        from .llm import client
        from .sources import http

        self.settings, self.tracker = settings, tracker
        self.llm = client(settings)
        self.ask = None  # Set per task: how a step asks you something.
        if require_model and self.llm is None:
            raise SystemExit(NOT_SET_UP)
        self.http = http()
        self.playwright = sync_playwright().start()
        self.headless = Headless(self.playwright, settings.browser_channel)
        self._browser = None
        self.profile_dir = profile_dir or BROWSER_PROFILE
        self.headless_window = headless_window  # Tests only; real submits stay visible.

    def browser(self):
        """The visible Chrome window, opened the first time a submit needs it."""
        from .browser import Browser

        if self._browser is None:
            self._browser = Browser(self.profile_dir, self.settings.browser_channel,
                                    headless=self.headless_window, playwright=self.playwright)
        return self._browser

    def close(self) -> None:
        try:
            if self._browser:
                self._browser.close()
            self.headless.close()
            self.http.close()
        finally:
            self.playwright.stop()


def run_task(task: dict, ctx: Context, log) -> str:
    tracker = ctx.tracker

    def ask(question: str) -> str:
        tracker.set_task(task["id"], status="waiting", question=question, answer=None)
        log(f"  waiting for you: {question}")
        deadline = time.time() + ANSWER_TIMEOUT
        while time.time() < deadline:
            row = tracker.task(task["id"])
            if row["answer"] is not None:
                tracker.set_task(task["id"], status="running", question=None)
                return row["answer"]
            time.sleep(1)
        tracker.set_task(task["id"], status="running", question=None)
        raise TimeoutError("No reply within 15 minutes.")

    ctx.ask = ask  # Agents inside a step (the explorer during prepare) ask through this task.
    handler = HANDLERS.get(task["kind"])
    if handler is None:
        raise ValueError(f"Unknown task {task['kind']!r}.")
    if task["kind"] in NEEDS_MODEL and ctx.llm is None:
        raise ValueError(NOT_SET_UP)
    return handler(task, ctx, log)


def _discover(task, ctx, log) -> str:
    from . import discover

    c = discover.run(ctx.llm, ctx.tracker, config.load_profile(), log)
    return (f"{c['postings']} postings found on job sites; {c['new_boards']} new company boards; "
            f"{c['new']} new jobs, {c['seen']} already known")


PREPARE_OUTCOMES = {"prepared": "Prepared", "filtered_out": "Not prepared", "closed": "Closed",
                    "failed": "Failed", "found": "Not prepared", "needs_you": "Needs you"}


def _prepare(task, ctx, log) -> str:
    """Prepare one job; the result says what happened and why (the job's reason)."""
    status = step("prepare", "prepare")(task["job_id"], ctx, log, note=task["arg"] or "")
    reason = (ctx.tracker.get(task["job_id"]) or {}).get("reason")
    return PREPARE_OUTCOMES.get(status, status) + (f": {reason}" if reason else
                                                   ": ready for your review." if status == "prepared" else "")


def _refresh(task, ctx, log) -> str:
    from . import matching, sources

    counts = sources.refresh(ctx.tracker, config.load_profile(), boards=True, log=log,
                             progress=lambda text: ctx.tracker.set_task(task["id"], result=text))
    matching.keyword_rank(ctx.tracker, config.load_profile())  # Free; the model ranks only the best of these.
    return (f"{counts['new']} new, {counts['closed']} closed, {counts['seen']} already known; "
            f"{counts['skipped']} outside your search not kept")


def _sync_profile(task, ctx, log) -> str:
    from . import profile_sync

    return "Profile updated from your resume. " + " ".join(profile_sync.sync(ctx.llm, log))


def _merge_resumes(task, ctx, log) -> str:
    from . import resumes

    proposal = resumes.propose(log=log, llm=ctx.llm)
    return f"Proposed master resume ready for review ({len(proposal['kept'])} line(s) kept from older versions)."


def _import_resume(task, ctx, log) -> str:
    from . import resumes

    details = resumes.parse(ctx.llm, Path(task["arg"]), log)
    return f"Read {details['source']} (content through {details['latest_date']})."


def _redraft(task, ctx, log) -> str:  # arg: {"people": [ids], "note": "what to change"}
    import json

    from . import people

    request = json.loads(task["arg"] or "{}")
    return people.redraft(task["job_id"], ctx, request.get("people") or [], request.get("note", ""), log)


def step(module: str, name: str):
    """A pipeline function, imported when first used (the dashboard imports this module too)."""
    import importlib

    return getattr(importlib.import_module(f".{module}", __package__), name)


# Every kind of task the dashboard and the assistant queue, and what runs it.
HANDLERS = {
    "discover": _discover, "refresh": _refresh, "sync_profile": _sync_profile,
    "merge_resumes": _merge_resumes, "import_resume": _import_resume, "redraft": _redraft,
    "score_matches": lambda task, ctx, log: score_matches(ctx, log),
    # arg: a note on what to change, for a redo.
    "prepare": lambda task, ctx, log: _prepare(task, ctx, log),
    "draft": lambda task, ctx, log: step("prepare", "draft_missing")(task["job_id"], ctx, log),
    "render": lambda task, ctx, log: step("prepare", "render")(task["job_id"], ctx, log),
    "letter": lambda task, ctx, log: step("prepare", "add_letter")(task["job_id"], ctx, log),
    "people": lambda task, ctx, log: step("people", "refresh")(task["job_id"], ctx, log),
    "first_read": lambda task, ctx, log: step("first_read", "again")(task["job_id"], ctx, log),
    "submit": lambda task, ctx, log: step("submit", "submit")(task["job_id"], ctx, ctx.ask, log),
}
NEEDS_MODEL = ("sync_profile", "import_resume", "first_read")


def score_matches(ctx: Context, log) -> str:
    from . import matching

    if ctx.llm is None:
        raise ValueError(NOT_SET_UP)
    result = matching.score(ctx.llm, ctx.tracker, log=log)
    return (f"Ranked {result['scored']} job(s) with the model; {result['remaining']} not yet "
            "(they keep their keyword score).")


# Tasks with lanes of their own: they never open the browser, so they run alongside the
# others, one at a time per lane. A prepare needn't wait for a long ranking, or for a
# refresh (on the class platform reading every company board took over 15 minutes, most
# of a session).
LANES = (("score_matches",), ("refresh", "discover"))
OWN_LANE = tuple(kind for kinds in LANES for kind in kinds)


def lane(ctx: Context, log, stop: threading.Event, kinds: tuple[str, ...] = LANES[0]) -> None:
    while not stop.is_set():
        task = ctx.tracker.claim_task(kinds=kinds)
        if task is None:
            stop.wait(1)
            continue
        log(f"Task {task['id']}: {task['kind']} (alongside other tasks)")
        stopping.watch(lambda task_id=task["id"]: ctx.tracker.stop_asked(task_id))
        try:
            if task["kind"] in NEEDS_MODEL and ctx.llm is None:
                raise ValueError(NOT_SET_UP)
            # The handler directly, not run_task: that sets ctx.ask, the way a prepare's agent
            # asks you things, and it must stay pointed at the prepare's task.
            result = HANDLERS[task["kind"]](task, ctx, log)
            ctx.tracker.set_task(task["id"], status="done", result=str(result)[:2000])
        except stopping.Stopped:
            ctx.tracker.set_task(task["id"], status="stopped", result="Stopped by you.")
        except Exception as error:  # noqa: BLE001 - reported on the task, like any other.
            log(traceback.format_exc())
            ctx.tracker.set_task(task["id"], status="failed", result=f"{type(error).__name__}: {error}"[:2000])


def serve(settings, log=print, idle_exit: float | None = None) -> None:
    """Run queued tasks until stopped (or until idle for `idle_exit` seconds)."""
    with browser_lock():
        tracker = Tracker(DB_PATH)
        for note in tracker.recover():
            log(f"Recovered: {note}")
        ctx = Context(settings, tracker)
        stop = threading.Event()
        sides = [threading.Thread(target=lane, args=(ctx, log, stop, kinds), daemon=True) for kinds in LANES]
        for side in sides:
            side.start()
        idle_since = time.time()
        parent = os.getppid()
        try:
            while True:
                if os.getppid() != parent:  # The dashboard that started this worker is gone.
                    log("The dashboard stopped; the worker stops too.")
                    return
                task = tracker.claim_task(exclude=OWN_LANE)
                if task is None:
                    busy = any(t["kind"] in OWN_LANE and t["status"] in ("queued", "running")
                               for t in tracker.tasks(10))
                    if busy:
                        idle_since = time.time()
                    if idle_exit is not None and time.time() - idle_since > idle_exit:
                        return
                    time.sleep(1)
                    continue
                label = f"{task['kind']}" + (f" job {task['job_id']}" if task["job_id"] else "")
                log(f"Task {task['id']}: {label}")
                stopping.watch(lambda task_id=task["id"]: tracker.stop_asked(task_id))
                try:
                    result = run_task(task, ctx, log)
                    tracker.set_task(task["id"], status="done", result=str(result)[:2000])
                except stopping.Stopped:
                    log(f"Task {task['id']} stopped at your request")
                    tracker.set_task(task["id"], status="stopped", result="Stopped by you.")
                except (Exception, SystemExit) as error:  # noqa: BLE001 - one task's failure doesn't stop the worker.
                    log(traceback.format_exc())
                    tracker.set_task(task["id"], status="failed",
                                     result=f"{type(error).__name__}: {error}"[:2000])
                idle_since = time.time()
        finally:
            stop.set()
            for side in sides:
                side.join(timeout=5)
            ctx.close()
