"""Click through the dashboard like a person: prepare → review → approve → submit → code.

The dashboard and a worker run in this process against the demo Greenhouse,
with the scripted model from test_pipeline. A real (headless) browser clicks
the dashboard; the worker's own browser fills the demo form.
"""

import re
import threading
import time
from dataclasses import replace
from http.server import ThreadingHTTPServer

from playwright.sync_api import expect, sync_playwright

from job_agent import dashboard, demo_greenhouse, worker
from job_agent.tracker import Tracker

from test_pipeline import ScriptedModel, add_job, env as env  # noqa: F401 - fixtures
from test_pipeline import site as site  # noqa: F401


def run_worker(db_path, settings, profile_dir, stop):
    """The worker loop, in a thread, with the scripted model."""
    context = worker.Context(settings, Tracker(db_path), profile_dir=profile_dir,
                             headless_window=True)
    context.llm = ScriptedModel()
    try:
        while not stop.is_set():
            task = context.tracker.claim_task()
            if task is None:
                time.sleep(0.2)
                continue
            try:
                result = worker.run_task(task, context, lambda *_: None)
                context.tracker.set_task(task["id"], status="done", result=str(result))
            except Exception as error:  # noqa: BLE001
                context.tracker.set_task(task["id"], status="failed", result=repr(error))
    finally:
        context.close()


def test_whole_flow_in_the_dashboard(env, tmp_path, monkeypatch):  # noqa: F811
    settings, tracker = env
    db_path = tmp_path / "data" / "t.db"
    job_id = add_job(tracker, "4001")
    monkeypatch.setattr(dashboard, "DB_PATH", db_path)
    monkeypatch.setattr(dashboard.Dashboard, "ensure_worker", lambda self: None)
    app = dashboard.Dashboard(replace(settings), 0)
    app.llm_missing = False  # The worker below runs a scripted model.
    server = ThreadingHTTPServer(("127.0.0.1", 0), None)
    app.port = server.server_port
    server.RequestHandlerClass = dashboard.handler_for(app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stop = threading.Event()
    thread = threading.Thread(target=run_worker, daemon=True,
                              args=(db_path, settings, tmp_path / "chrome2", stop))
    thread.start()
    base = f"http://127.0.0.1:{app.port}"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(f"{base}/jobs?status=found")
            page.get_by_role("button", name="Prepare").click()
            expect(page.locator(".flash")).to_contain_text("Queued")

            page.goto(f"{base}/queue")
            for _ in range(60):
                if page.get_by_text("Machine Learning Intern").count():
                    break
                time.sleep(0.5)
                page.reload()
            page.get_by_role("link", name="Machine Learning Intern").click()
            expect(page.locator("h2").filter(has_text="Fit 85")).to_be_visible()

            # Edit an answer, then approve from the job page.
            row = page.locator("tr").filter(has_text="Why do you want to work at Contoso AI?")
            row.locator("textarea").fill("I want to build evaluation tools for document models.")
            row.get_by_role("button", name="Save").click()
            expect(page.locator(".flash")).to_contain_text("Saved")
            page.get_by_role("button", name="Approve").click()
            expect(page.locator(".pill").first).to_have_text("approved")

            page.get_by_role("button", name="Submit now").click()
            # The demo Greenhouse emails a security code; the dashboard asks for it.
            for _ in range(80):
                page.goto(base + "/")
                if page.locator("input[name=answer]").count():
                    break
                time.sleep(0.5)
            expect(page.get_by_text("security code")).to_be_visible()
            page.locator("input[name=answer]").fill(demo_greenhouse.LAST_CODE["value"])
            page.get_by_role("button", name="Send").click()

            for _ in range(80):
                if Tracker(db_path).get(job_id)["status"] == "submitted":
                    break
                time.sleep(0.5)
            page.goto(f"{base}/job/{job_id}")
            expect(page.locator(".pill").first).to_have_text("submitted")
            expect(page.get_by_role("link", name="screenshot")).to_be_visible()
            browser.close()
        sent = demo_greenhouse.SUBMISSIONS[-1]
        assert sent["question_9003"] == "I want to build evaluation tools for document models."
        assert re.search(r"_Resume\.pdf$", sent["resume_filename"])
    finally:
        stop.set()
        thread.join(timeout=20)
        server.shutdown()
