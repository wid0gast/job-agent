"""The dashboard renders and refuses requests that don't come from itself."""

import http.client
import re
import threading
from dataclasses import replace
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


def test_pages_and_request_checks(tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "DB_PATH", tmp_path / "t.db")
    Tracker(tmp_path / "t.db").add_lead("https://job-boards.greenhouse.io/acme/jobs/1", "s",
                                        "Acme", "ML Intern", "NYC")
    settings = replace(config.Settings.from_env(require_model=False), submit_mode="review")
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
        assert "ML Intern" in request(port, "GET", "/jobs?status=found")[1]
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
    settings = replace(config.Settings.from_env(require_model=False), submit_mode="review")
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
    monkeypatch.setattr(dashboard, "DB_PATH", tmp_path / "t.db")
    (config.PROFILE_DIR / "resume.md").unlink(missing_ok=True)
    (config.PROFILE_DIR / "search.yaml").write_text("description: data science\n")
    Tracker(tmp_path / "t.db").add_lead("https://job-boards.greenhouse.io/acme/jobs/1", "s", "Acme", "ML Intern", "NYC")
    settings = replace(config.Settings.from_env(require_model=False), submit_mode="review")
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
