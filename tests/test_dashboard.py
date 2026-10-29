"""The dashboard renders and refuses requests that don't come from itself."""

import http.client
import re
import threading
import time
from http.server import ThreadingHTTPServer

from job_agent import config, dashboard
from job_agent.tracker import Tracker


def request(port, method, path, body="", headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body, {"Host": f"127.0.0.1:{port}",
                                      "Content-Type": "application/x-www-form-urlencoded",
                                      **(headers or {})})
    response = conn.getresponse()
    return response.status, response.read().decode(), dict(response.getheaders())


def past_setup():
    """Today, not Get started, at / (Get started shows there until a first application)."""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    (config.DATA_DIR / dashboard.SETUP_SKIPPED).touch()


def test_pages_and_request_checks(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "DB_PATH", tmp_path / "t.db")
    Tracker(tmp_path / "t.db").add_lead("https://job-boards.greenhouse.io/acme/jobs/1", "s",
                                        "Acme", "ML Intern", "NYC")
    settings = config.Settings.from_env(require_model=False)
    app = dashboard.Dashboard(settings, 0)
    server = ThreadingHTTPServer(("127.0.0.1", 0), None)
    app.port = server.server_port
    server.RequestHandlerClass = dashboard.handler_for(app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = app.port
    try:
        for path in ("/", "/queue", "/jobs?status=found", "/activity"):
            status, body, _ = request(port, "GET", path)
            assert status == 200, path
        for path in dashboard.ASSETS:  # The style and scripts, from static/.
            status, body, headers = request(port, "GET", path)
            assert status == 200 and body.strip() and ("css" if path.endswith(".css") else "javascript") in \
                headers["Content-Type"], path
        assert "ML Intern" in request(port, "GET", "/jobs?status=found")[1]
        # The class platform's no-referrer header left form posts without a Referer, refused as cross-origin.
        assert "<meta name=referrer content=same-origin>" in request(port, "GET", "/")[1]
        token = re.search(r'name=token value="([^"]+)"', request(port, "GET", "/")[1]).group(1)

        # DNS rebinding: wrong Host header.
        assert request(port, "GET", "/", headers={"Host": "evil.example"})[0] == 403
        # Another site posting to localhost: wrong Origin, or no token.
        evil = {"Origin": "https://evil.example"}
        assert request(port, "POST", "/requeue", f"token={token}&job_id=1", evil)[0] == 403
        assert request(port, "POST", "/requeue", "job_id=1")[0] == 403
        # Files outside data/applications are never served.
        assert request(port, "GET", "/file?path=/etc/passwd")[0] == 404

        status, _, headers = request(port, "POST", "/reject", f"token={token}&job_id=1",
                                     {"Origin": f"http://127.0.0.1:{port}"})
        assert status == 303 and "msg=" in headers["Location"]  # refused: found can't be rejected
    finally:
        server.shutdown()


def test_today_points_to_the_assistant_and_agent_traces_render(tmp_path, monkeypatch):
    import json

    from job_agent.harness import trace_path

    monkeypatch.setattr(dashboard, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    tracker = Tracker(tmp_path / "t.db")
    tracker.add_lead("https://job-boards.greenhouse.io/acme/jobs/1", "s", "Acme", "ML Intern", "NYC")
    past_setup()
    settings = config.Settings.from_env(require_model=False)
    app = dashboard.Dashboard(settings, 0)
    server = ThreadingHTTPServer(("127.0.0.1", 0), None)
    app.port = server.server_port
    server.RequestHandlerClass = dashboard.handler_for(app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        today = request(app.port, "GET", "/")[1]
        assert "Ask the assistant" in today and 'action="/assistant"' in today
        assert 'href="/assistant"' in today  # In the navigation, next to Today.
        prefilled = request(app.port, "GET", "/assistant?q=Find+3+ML+internships")[1]
        assert ">Find 3 ML internships</textarea>" in prefilled  # Filled in, never sent by a link.

        trace = trace_path("explore-1")
        trace.parent.mkdir(parents=True)
        trace.write_text("\n".join(json.dumps(e) for e in [
            {"t": 1, "kind": "thought", "data": "Opening the form."},
            {"t": 2, "kind": "call", "data": {"name": "snapshot", "args": {}}},
            {"t": 3, "kind": "result", "data": {"name": "click", "ok": False, "error": "ValueError: gone"}},
            {"t": 4, "kind": "finish", "data": "Stopped at submit."}]))
        page = request(app.port, "GET", "/runs/explore-1")[1]
        for text in ("Fill in the application for ML Intern at Acme", "Opening the form.", "snapshot",
                     "error: ValueError: gone", "Stopped at submit."):
            assert text in page, text
        assert request(app.port, "GET", "/runs/7")[0] == 404  # Planner runs are gone.
    finally:
        server.shutdown()


def test_ranked_jobs_open_before_a_resume_and_errors_show(tmp_path, monkeypatch):
    """Without a master resume, Ranked jobs still opens; a page that fails says so (never hangs)."""
    import shutil

    profile = tmp_path / "profile"
    shutil.copytree(config.ROOT / "profile.example", profile)
    (profile / "resume.md").unlink(missing_ok=True)
    (profile / "search.yaml").write_text("description: data science\n")
    monkeypatch.setattr(config, "PROFILE_DIR", profile)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(dashboard, "DB_PATH", tmp_path / "t.db")
    Tracker(tmp_path / "t.db").add_lead("https://job-boards.greenhouse.io/acme/jobs/1", "s", "Acme", "ML Intern", "NYC")
    settings = config.Settings.from_env(require_model=False)
    app = dashboard.Dashboard(settings, 0)
    server = ThreadingHTTPServer(("127.0.0.1", 0), None)
    app.port = server.server_port
    server.RequestHandlerClass = dashboard.handler_for(app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, body, _ = request(app.port, "GET", "/jobs")
        assert status == 200 and "ML Intern" in body
        monkeypatch.setattr(dashboard, "page_queue", lambda *_: (_ for _ in ()).throw(SystemExit("boom")))
        status, body, _ = request(app.port, "GET", "/queue")
        assert status == 500 and "Something went wrong" in body and "boom" in body
    finally:
        server.shutdown()


def test_a_prepared_job_can_be_marked_applied(tmp_path, monkeypatch):
    """Applied to a prepared job outside the app: one click records it (and it leaves the queue)."""
    from job_agent import packets

    monkeypatch.setattr(dashboard, "DB_PATH", tmp_path / "t.db")
    tracker = Tracker(tmp_path / "t.db")
    tracker.add_lead("https://job-boards.greenhouse.io/acme/jobs/1", "s", "Acme", "ML Intern", "NYC")
    job = tracker.all()[0]
    folder = packets.directory(job)
    packets.save({"job": {"company_id": "acme"}, "fit": {"score": 80, "summary": "", "requirements": [], "flags": []},
                  "answers": [], "documents": {}, "checks": {},
                  "tailoring": {"matched": [], "emphasized": [], "gaps": []}}, folder)
    tracker.move(job["id"], "found", "preparing")
    tracker.move(job["id"], "preparing", "prepared", packet_dir=str(folder))
    app = dashboard.Dashboard(config.Settings.from_env(require_model=False), 0)
    server = ThreadingHTTPServer(("127.0.0.1", 0), None)
    app.port = server.server_port
    server.RequestHandlerClass = dashboard.handler_for(app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        page = request(app.port, "GET", f"/job/{job['id']}")[1]
        assert 'action="/mark-applied"' in page
        token = re.search(r'name=token value="([^"]+)"', page).group(1)
        request(app.port, "POST", "/mark-applied", f"token={token}&job_id={job['id']}",
                {"Origin": f"http://127.0.0.1:{app.port}"})
        assert Tracker(tmp_path / "t.db").get(job["id"])["status"] == "submitted"
    finally:
        server.shutdown()


def test_pages_with_work_running_update_in_place_without_reloading(tmp_path, monkeypatch):
    """No full-page reloads (they flash and lose your place): the content is swapped in place."""
    from playwright.sync_api import sync_playwright

    monkeypatch.setattr(dashboard, "DB_PATH", tmp_path / "t.db")
    tracker = Tracker(tmp_path / "t.db")
    task = tracker.add_task("refresh")
    tracker.set_task(task, status="running", result="Job lists read: 10 new jobs kept so far")
    past_setup()
    app = dashboard.Dashboard(config.Settings.from_env(require_model=False), 0)
    app.ensure_worker = lambda: None
    server = ThreadingHTTPServer(("127.0.0.1", 0), None)
    app.port = server.server_port
    server.RequestHandlerClass = dashboard.handler_for(app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{app.port}/")
            assert "10 new jobs" in page.inner_text("main")
            reloads = []
            page.on("framenavigated", lambda frame: reloads.append(frame.url))
            tracker.set_task(task, result="Job lists read: 42 new jobs kept so far")
            deadline = time.time() + 8
            while "42 new jobs" not in page.inner_text("main") and time.time() < deadline:
                page.wait_for_timeout(250)
            assert "42 new jobs" in page.inner_text("main")
            assert reloads == []  # Same page: updated in place, never reloaded.
            assert 'http-equiv="refresh"' not in page.content()
            browser.close()
    finally:
        server.shutdown()


def test_failed_tasks_are_said_plainly_with_what_to_do():
    from job_agent.dashboard import plain_error

    happened, todo = plain_error("Error: Page.goto: net::ERR_SOCKET_NOT_CONNECTED at https://x.example/job")
    assert happened.startswith("A network problem") and "safe" in todo
    assert plain_error("RuntimeError: No Claude connection: install Claude Code")[1].startswith("Sign in")
    assert plain_error("ValueError: Something odd happened")[0] == "Something odd happened"


def test_top_matches_that_all_fall_short_are_offered_and_failures_show_in_the_tray(tmp_path, monkeypatch):
    from job_agent.worker import TOP_PICK

    monkeypatch.setattr(dashboard, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    tracker = Tracker(tmp_path / "t.db")
    for n, fit in ((1, 52), (2, 41)):
        tracker.add_lead(f"https://job-boards.greenhouse.io/acme{n}/jobs/{n}", "s", f"Acme {n}", f"ML Intern {n}", "NYC")
        tracker.move(n, "found", "filtered_out", fit_score=fit, reason=f"Fit {fit} is below your threshold of 60.")
        task = tracker.add_task("prepare", n, f"{TOP_PICK}2")
        tracker.set_task(task, status="done", result="filtered_out")
    failed = tracker.add_task("refresh")
    tracker.set_task(failed, status="failed", result="ConnectError: [Errno 8] connection failed")
    app = dashboard.Dashboard(config.Settings.from_env(require_model=False), 0)
    today = dashboard.page_today(app, "").decode()
    assert "None of your top matches passed your fit threshold" in today and "Prepare these anyway" in today
    assert today.index("ML Intern 1") < today.index("ML Intern 2")  # Closest first.
    assert "Refresh job lists failed:" in today and "A network problem" in today  # The tray, in words.
    app.tray_seen = failed
    assert "failed:" not in dashboard.tray(app)


def test_get_started_walks_a_new_user_to_a_first_application(monkeypatch):
    """Today is Get started until a first application: each step opens the next, a resume being
    read doesn't hold up saying what you want, and Search settings it doesn't show are kept."""
    import yaml

    (config.PROFILE_DIR / "resume.md").unlink()
    (config.PROFILE_DIR / "search.yaml").write_text("fit_threshold: 70\n")
    tracker = Tracker(dashboard.DB_PATH)
    app = dashboard.Dashboard(config.Settings.from_env(require_model=False), 0)
    app.ensure_worker = lambda: None
    app.llm_missing = True
    server = ThreadingHTTPServer(("127.0.0.1", 0), None)
    app.port = server.server_port
    server.RequestHandlerClass = dashboard.handler_for(app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    post = {"Origin": f"http://127.0.0.1:{app.port}", "Referer": f"http://127.0.0.1:{app.port}/"}
    try:
        page = request(app.port, "GET", "/")[1]
        assert "<h1>Get started</h1>" in page and "Getting started 0/4" in page
        assert 'class="card step open" id=step-connect' in page and "step-resume" not in page  # One at a time.
        assert "Claude isn't connected yet" not in page  # Said once, by its step.
        token = re.search(r'name=token value="([^"]+)"', page).group(1)

        app.llm_missing = False
        page = request(app.port, "GET", "/")[1]
        assert 'id=step-resume' in page and 'action="/resumes/upload"' in page and 'value="/setup"' in page
        assert "step-wants" not in page

        reading = tracker.add_task("import_resume", arg="resume.pdf")
        tracker.set_task(reading, status="running")
        page = request(app.port, "GET", "/")[1]
        assert "Reading your resume" in page and 'action="/setup/wants"' in page  # Meanwhile, the next step.
        assert "data-live" in page  # It updates itself; the form is kept as you left it (data-keep).

        status, _, headers = request(app.port, "POST", "/setup/wants", "&".join([
            f"token={token}", "description=ML+internships", "job_types=internship",
            "locations=New+York%2C+NY%0ARemote", "terms=Summer+2027", "authorization=f1"]), post)
        assert status == 303 and headers["Location"].startswith("/setup")
        search = yaml.safe_load((config.PROFILE_DIR / "search.yaml").read_text())
        assert search["description"] == "ML internships" and search["locations"] == ["New York, NY", "Remote"]
        assert search["terms"] == ["Summer 2027"] and search["fit_threshold"] == 70  # Kept.
        assert config.load_profile()["work_authorization"]["authorized_in_us"] is True
        refresh = next(t for t in tracker.tasks(10) if t["kind"] == "refresh")
        assert refresh["arg"] == "rank"  # Ranked when found, for you to pick a first job.

        # Resume accepted, jobs found and ranked: pick one, or paste a link.
        tracker.set_task(reading, status="done")
        tracker.set_task(refresh["id"], status="done")
        (config.PROFILE_DIR / "resume.md").write_text("# Jane Doe\n")
        tracker.add_lead("https://job-boards.greenhouse.io/acme/jobs/1", "s", "Acme", "ML Intern", "New York, NY")
        page = request(app.port, "GET", "/")[1]
        assert "Getting started 3/4" in page and "Prepare this one" in page and 'action="/link"' in page

        job = tracker.all()[0]
        tracker.update(job["id"], status="preparing")
        tracker.update(job["id"], status="prepared")
        page = request(app.port, "GET", "/")[1]
        assert "Ask the assistant" in page and "Getting started" not in page  # Done: Today, for good.
        job = tracker.get(job["id"])
        assert "Your first application is ready" in dashboard.first_review_note(app, job)
        (config.DATA_DIR / dashboard.REVIEW_SEEN).touch()
        assert dashboard.first_review_note(app, job) == ""
    finally:
        server.shutdown()


def test_get_started_can_be_skipped_and_found_again(monkeypatch):
    app = dashboard.Dashboard(config.Settings.from_env(require_model=False), 0)
    server = ThreadingHTTPServer(("127.0.0.1", 0), None)
    app.port = server.server_port
    server.RequestHandlerClass = dashboard.handler_for(app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        page = request(app.port, "GET", "/")[1]
        token = re.search(r'name=token value="([^"]+)"', page).group(1)
        status, _, headers = request(app.port, "POST", "/setup/skip", f"token={token}",
                                     {"Origin": f"http://127.0.0.1:{app.port}"})
        assert status == 303
        page = request(app.port, "GET", "/")[1]
        assert "Ask the assistant" in page and 'href="/setup">Getting started' in page
        assert "<h1>Get started</h1>" in request(app.port, "GET", "/setup")[1]
    finally:
        server.shutdown()
