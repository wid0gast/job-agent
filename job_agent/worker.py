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

from . import config
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
    from . import prepare, resumes, sources, submit

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
    if task["kind"] == "plan":
        from . import planner

        if ctx.llm is None:
            raise ValueError(NOT_SET_UP)
        return planner.run(task["arg"] or "", ctx, ask, log, task["id"])
    if task["kind"] == "discover":
        from . import discover

        c = discover.run(ctx.llm, tracker, config.load_profile(), log)
        return (f"{c['postings']} postings found on job sites; {c['new_boards']} new company boards; "
                f"{c['new']} new jobs, {c['seen']} already known")
    if task["kind"] == "refresh":
        counts = sources.refresh(tracker, config.load_profile(), boards=True, log=log)
        from . import matching

        matching.keyword_rank(tracker, config.load_profile())  # Free; the model ranks only the best of these.
        return f"{counts['new']} new, {counts['closed']} closed, {counts['seen']} already known"
    if task["kind"] == "sync_profile":
        from . import profile_sync

        if ctx.llm is None:
            raise ValueError(NOT_SET_UP)
        notes = profile_sync.sync(ctx.llm, log)
        return "Profile updated from your resume. " + " ".join(notes)
    if task["kind"] == "merge_resumes":
        proposal = resumes.propose(log=log)
        return f"Proposed master resume ready for review ({len(proposal['kept'])} line(s) kept from older versions)."
    if task["kind"] == "import_resume":
        if ctx.llm is None:
            raise ValueError(NOT_SET_UP)
        details = resumes.parse(ctx.llm, Path(task["arg"]), log)
        return f"Read {details['source']} (content through {details['latest_date']})."
    if task["kind"] == "score_matches":
        return score_matches(ctx, log)
    if task["kind"] == "draft":
        return prepare.draft_missing(task["job_id"], ctx, log)
    if task["kind"] == "prepare":
        return prepare.prepare(task["job_id"], ctx, log)
    if task["kind"] == "submit":
        return submit.submit(task["job_id"], ctx, ask, log)
    raise ValueError(f"Unknown task {task['kind']!r}.")


def score_matches(ctx: Context, log) -> str:
    from . import matching

    if ctx.llm is None:
        raise ValueError(NOT_SET_UP)
    result = matching.score(ctx.llm, ctx.tracker, log=log)
    return (f"Ranked {result['scored']} job(s) with the model; {result['remaining']} not yet "
            "(they keep their keyword score).")


# Tasks with their own lane: they only call the model, so they run alongside the others
# (a prepare needn't wait for a long ranking), one at a time.
OWN_LANE = ("score_matches",)


def lane(ctx: Context, log, stop: threading.Event) -> None:
    while not stop.is_set():
        task = ctx.tracker.claim_task(kinds=OWN_LANE)
        if task is None:
            stop.wait(1)
            continue
        log(f"Task {task['id']}: {task['kind']} (alongside other tasks)")
        try:
            ctx.tracker.set_task(task["id"], status="done", result=score_matches(ctx, log)[:2000])
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
        side = threading.Thread(target=lane, args=(ctx, log, stop), daemon=True)
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
                try:
                    result = run_task(task, ctx, log)
                    tracker.set_task(task["id"], status="done", result=str(result)[:2000])
                except (Exception, SystemExit) as error:  # noqa: BLE001 - one task's failure doesn't stop the worker.
                    log(traceback.format_exc())
                    tracker.set_task(task["id"], status="failed",
                                     result=f"{type(error).__name__}: {error}"[:2000])
                idle_since = time.time()
        finally:
            stop.set()
            side.join(timeout=5)
            ctx.close()
