"""Start hosted/app.py like the platform does, with a fake broker and a scripted model."""

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESUME = (ROOT / "profile.example/resume.md").read_text()


def call(name, args):
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": f"c{time.time_ns()}", "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)}}]}


def last_tool(messages):
    return json.loads(next(m for m in reversed(messages) if m["role"] == "tool")["content"])


SEARCH = [
    lambda m: call("search_jobs", {"query": "intern"}),
    lambda m: call("add_job_lead", {"job_id": "101", "why": "Python internship in NYC"}),
    lambda m: call("finish_search", {"summary": "One lead."}),
]
EMAIL = "jane.doe@example.com"
APPLY = [
    lambda m: call("read_job", {"job_id": "101"}),
    lambda m: call("save_tailored_resume", {"markdown": "# Jane\nServed 500 users."}),
    lambda m: call("save_tailored_resume", {"markdown": RESUME}),
    lambda m: call("save_cover_letter", {"text": "Dear Northwind Labs team, ..."}),
    lambda m: call("get_application_form", {}),
    lambda m: call("create_account", {"email": EMAIL}),
    lambda m: call("ask_user", {"question": "What is the verification code?"}),
    lambda m: call("verify_account", {"email": EMAIL, "code": last_tool(m)["result"]}),
    lambda m: call("fill_application", {"answers": {
        "first_name": "Jane", "last_name": "Doe", "email": EMAIL, "authorized": "Yes",
        "sponsorship": "No", "why": "I like internal tools.", "certify": "Yes"}}),
    lambda m: call("attach_resume", {}),
    lambda m: call("submit_application", {"summary": "Filled required fields."}),
    lambda m: call("finish_job", {"status": "submitted", "report": "Confirmed."}),
]
SEEN = {"profiles": set(), "errors": []}


class FakeBroker(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send_json(self, value):
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        assert self.headers["Authorization"] == "Bearer test-token"
        self.send_json({"mode": "hosted", "simulated": False, "runId": "run-1",
                        "inference": {"selection": {"modelId": "fake/model",
                                                    "profileId": "fake-profile"}}})

    def do_POST(self):
        SEEN["profiles"].add(self.headers.get("X-Workshop-Expected-Profile"))
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        messages = body["messages"]
        script = SEARCH if messages[0]["content"].startswith("You find") else APPLY
        step = sum(1 for m in messages if m["role"] == "assistant")
        for m in messages:
            if m["role"] == "tool" and '"ok": false' in m["content"]:
                SEEN["errors"].append(m["content"])
        message = script[step](messages)
        self.send_json({"choices": [{"message": message, "finish_reason": "tool_calls"}]})


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def request(port, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as response:
        raw = response.read()
        return json.loads(raw) if path != "/" and path != "/healthz" else raw.decode()


def wait_for(port, predicate, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = request(port, "/state")
        if predicate(state):
            return state
        time.sleep(0.1)
    raise AssertionError(f"Timed out. Last state: {json.dumps(state)[:1500]}")


def test_hosted_app_end_to_end():
    broker = ThreadingHTTPServer(("127.0.0.1", 0), FakeBroker)
    threading.Thread(target=broker.serve_forever, daemon=True).start()
    port = free_port()
    env = {**os.environ, "PORT": str(port), "WORKSHOP_RUN_ID": "run-1",
           "WORKSHOP_RUN_TOKEN": "test-token",
           "WORKSHOP_BROKER_URL": f"http://127.0.0.1:{broker.server_port}"}
    app = subprocess.Popen([sys.executable, "hosted/app.py"], cwd=ROOT, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(50):
            try:
                assert request(port, "/healthz") == "ok"
                break
            except OSError:
                time.sleep(0.1)
        assert "Job Agent" in request(port, "/")
        sample = request(port, "/state")["sample"]
        request(port, "/run", {"request": "SWE internships", "max_jobs": 1,
                               "profile": sample["profile"], "resume": sample["resume"]})

        state = wait_for(port, lambda s: (s["pending"] or {}).get("type") == "question")
        code = state["inbox"][0].rsplit(" ", 1)[1]
        request(port, "/reply", {"id": state["pending"]["id"], "text": code})

        state = wait_for(port, lambda s: (s["pending"] or {}).get("type") == "approval")
        assert state["pending"]["answers"]["authorized"] == "Yes"
        request(port, "/reply", {"id": state["pending"]["id"], "decision": "approve"})

        state = wait_for(port, lambda s: not s["busy"])
        assert state["error"] == "", state["error"]
        assert [j["status"] for j in state["jobs"]] == ["submitted"]
        assert all(v for k, v in state["checks"][0].items() if k != "job")
        assert {d["kind"] for d in state["documents"]} == {"Tailored resume", "Cover letter"}
        assert state["model"] == "fake/model"
        assert SEEN["profiles"] == {"fake-profile"}
        # The invented "500 users" was rejected and that is the only tool error.
        assert len(SEEN["errors"]) >= 1 and all("500" in e for e in SEEN["errors"])
    finally:
        app.terminate()
        broker.shutdown()
