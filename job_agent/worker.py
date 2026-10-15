"""The one process that owns the browser. The dashboard queues tasks; this runs them.

Playwright's sync API isn't thread-safe and Chrome locks its profile, so a
lock file guarantees a single worker. When a task needs you (a code, a
CAPTCHA), the question goes on the task row and the worker waits for your
reply from the dashboard.
"""

import fcntl
import time
import traceback
from contextlib import contextmanager
from pathlib import Path

from . import config
from .tracker import Tracker

DB_PATH = config.DATA_DIR / "applications.db"
BROWSER_PROFILE = config.DATA_DIR / "browser-profile"
LOCK_PATH = config.DATA_DIR / "worker.lock"
ANSWER_TIMEOUT = 15 * 60
LIVE_SCREENSHOT = config.DATA_DIR / "applications" / "live-page.png"


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
        if require_model and self.llm is None:
            raise SystemExit("Set OPENROUTER_API_KEY and OPENROUTER_MODEL in .env.")
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
                                    headless=self.headless_window or config.HOSTED, playwright=self.playwright)
        return self._browser

    def release_browsers(self) -> None:
        try:
            if self._browser:
                self._browser.context.close()
            self.headless.close()
        except Exception:  # noqa: BLE001 - a crashed browser is already gone.
            pass
        from .browser import Headless

        for shared in Headless._shared.values():
            try:
                shared.close()
            except Exception:  # noqa: BLE001
                pass
        Headless._shared.clear()
        self._browser = None

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
        if config.HOSTED and ctx._browser is not None:  # No window to look at: show the page instead.
            try:
                ctx._browser.page.screenshot(path=str(LIVE_SCREENSHOT), full_page=True)
            except Exception:  # noqa: BLE001 - the question still goes through.
                pass
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

    if task["kind"] == "plan":
        from . import planner

        if ctx.llm is None:
            raise ValueError("The planner needs a model: add an OpenRouter key and model in .env.")
        return planner.run(task["arg"] or "", ctx, ask, log, task["id"])
    if task["kind"] == "warmup":
        from . import hosted

        return hosted.warm_up(ctx, log)
    if task["kind"] == "refresh":
        counts = sources.refresh(tracker, config.load_profile(), boards=True, log=log)
        return f"{counts['new']} new, {counts['closed']} closed, {counts['seen']} already known"
    if task["kind"] == "sync_profile":
        from . import profile_sync

        if ctx.llm is None:
            raise ValueError("Add an OpenRouter key and model in .env to read your resume.")
        notes = profile_sync.sync(ctx.llm, log)
        return "Profile updated from your resume. " + " ".join(notes)
    if task["kind"] == "merge_resumes":
        proposal = resumes.propose(log=log)
        return f"Proposed master resume ready for review ({len(proposal['kept'])} line(s) kept from older versions)."
    if task["kind"] == "import_resume":
        if ctx.llm is None:
            raise ValueError("Add an OpenRouter key and model in .env to read resumes.")
        details = resumes.parse(ctx.llm, Path(task["arg"]), log)
        return f"Read {details['source']} (content through {details['latest_date']})."
    if task["kind"] == "score_matches":
        from . import matching

        if ctx.llm is None:
            raise ValueError("Add an OpenRouter key and model in .env to rank jobs.")
        result = matching.score(ctx.llm, tracker, log=log)
        return f"Ranked {result['scored']} job(s); {result['remaining']} still unranked."
    if task["kind"] == "draft":
        return prepare.draft_missing(task["job_id"], ctx, log)
    if task["kind"] == "prepare":
        return prepare.prepare(task["job_id"], ctx, log)
    if task["kind"] == "submit":
        return submit.submit(task["job_id"], ctx, ask, log)
    raise ValueError(f"Unknown task {task['kind']!r}.")


def serve(settings, log=print, idle_exit: float | None = None) -> None:
    """Run queued tasks until stopped (or until idle for `idle_exit` seconds)."""
    with browser_lock():
        tracker = Tracker(DB_PATH)
        for note in tracker.recover():
            log(f"Recovered: {note}")
        ctx = Context(settings, tracker)
        idle_since = time.time()
        try:
            while True:
                task = tracker.claim_task()
                if task is None:
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
                if config.HOSTED:  # Memory is tight: don't keep a browser open between tasks.
                    ctx.release_browsers()
                idle_since = time.time()
        finally:
            ctx.close()
