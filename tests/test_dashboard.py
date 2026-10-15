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


def test_goal_starts_a_planner_run_and_its_trace_renders(tmp_path, monkeypatch):
    import json

    from job_agent import planner

    monkeypatch.setattr(dashboard, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    settings = replace(config.Settings.from_env(require_model=False), submit_mode="review")
    app = dashboard.Dashboard(settings, 0)
    app.ensure_worker = lambda: None  # No real worker in this test.
    server = ThreadingHTTPServer(("127.0.0.1", 0), None)
    app.port = server.server_port
    server.RequestHandlerClass = dashboard.handler_for(app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port, origin = app.port, {"Origin": f"http://127.0.0.1:{app.port}"}
    try:
        today = request(port, "GET", "/")[1]
        assert "Give the agent a goal" in today
        token = re.search(r'name=token value="([^"]+)"', today).group(1)
        status, _, headers = request(port, "POST", "/plan", f"token={token}&goal=Find+3+ML+internships", origin)
        assert status == 303 and headers["Location"].startswith("/runs/")
        task_id = int(headers["Location"].split("/")[2].split("?")[0])
        assert app.tracker.task(task_id)["arg"] == "Find 3 ML internships"

        trace = planner.trace_path(task_id)
        trace.parent.mkdir(parents=True)
        trace.write_text("\n".join(json.dumps(e) for e in [
            {"t": 0, "kind": "task", "data": "{}"},
            {"t": 1, "kind": "thought", "data": "Checking the queue first."},
            {"t": 2, "kind": "call", "data": {"name": "queue_status", "args": {}}},
            {"t": 3, "kind": "result", "data": {"name": "queue_status", "ok": True, "result": {"prepared": 0}}},
            {"t": 4, "kind": "result", "data": {"name": "redo", "ok": False, "error": "ValueError: approved"}},
            {"t": 5, "kind": "finish", "data": "Nothing to do."}]))
        page = request(port, "GET", f"/runs/{task_id}")[1]
        for text in ("Checking the queue first.", "queue_status", "error: ValueError: approved", "Nothing to do.",
                     "Find 3 ML internships"):
            assert text in page, text
        assert f'href="/runs/{task_id}"' in request(port, "GET", "/")[1]
    finally:
        server.shutdown()
