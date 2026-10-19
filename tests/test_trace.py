"""Traces: every run on a job records its steps, model calls and the checks code made,
for the job's "How it was made" page."""

import json

from job_agent import llm, prepare, trace
from test_pipeline import ScriptedModel, add_job, ctx, env, site  # noqa: F401 - fixtures


def entries_of(job_id):
    return trace.read(trace.runs(job_id)[0])


def test_prepare_records_its_steps_and_checks(ctx):  # noqa: F811
    job_id = add_job(ctx, "4001")
    assert prepare.prepare(job_id, ctx, log=lambda *_: None) == "prepared"
    entries = entries_of(job_id)
    steps = [e["name"] for e in entries if e["type"] == "step"]
    assert steps[:4] == ["Your filters and limits", "Reading the posting", "Fit check",
                         "Reading the application form"]
    assert "Tailoring the resume" in steps and "Checking against your profile" in steps
    checks = {e["name"]: e for e in entries if e["type"] == "check"}
    hidden = checks["Only text a person can see reaches the model"]
    assert not hidden["passed"] and "hidden passage" in hidden["detail"]  # The demo posting hides a trap.
    assert checks["Tailored resume uses only numbers from your resume"]["passed"]
    ends = {e["of"]: e for e in entries if e["type"] == "end"}
    fit = next(e for e in entries if e["type"] == "step" and e["name"] == "Fit check")
    assert ends[fit["n"]]["summary"].startswith("Score 85")
    assert entries[-1]["type"] == "end_run" and entries[-1]["status"] == "prepared"

    shown = trace.tree(entries)
    assert shown["totals"]["caught"] >= 1 and shown["end"]["status"] == "prepared"


def test_a_stopped_prepare_says_why(ctx):  # noqa: F811
    class LowFit(ScriptedModel):
        def json(self, system, user, schema, name):
            reply = super().json(system, user, schema, name)
            return {**reply, "score": 20} if name == "fit" else reply

    ctx.llm = LowFit()
    job_id = add_job(ctx, "4001")
    assert prepare.prepare(job_id, ctx, log=lambda *_: None) == "filtered_out"
    entries = entries_of(job_id)
    caught = [e for e in entries if e["type"] == "check" and not e["passed"]]
    assert caught[-1]["name"].startswith("Fit is at or above your threshold")
    assert entries[-1]["status"] == "filtered_out" and "below your threshold" in entries[-1]["detail"]


def test_model_calls_are_recorded_with_usage(tmp_path, monkeypatch):
    from job_agent import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    client = llm.OpenRouter("key", "some/model")
    replies = [{"wrong": True}, {"score": 3}]

    def post(payload):
        body = {"model": "some/model", "usage": {"prompt_tokens": 1200, "completion_tokens": 80, "cost": 0.01},
                "choices": [{"message": {"content": json.dumps(replies.pop(0))}, "finish_reason": "stop"}]}
        client.count(body)
        return body

    monkeypatch.setattr(client, "post", post)
    schema = {"type": "object", "properties": {"score": {"type": "integer"}}, "required": ["score"]}
    with trace.run("prepare", 7), trace.step("Fit check"):
        assert client.json("You judge fit.", '{"job": "x"}', schema, "fit") == {"score": 3}
    entries = entries_of(7)
    models = [e for e in entries if e["type"] == "model"]
    # Both requests show, with their tokens, and the retry is a check.
    assert len(models) == 2 and models[0]["usage"]["input"] == 1200 and models[0]["prompt"] == '{"job": "x"}'
    assert [e["type"] for e in entries].count("check") == 1
    assert trace.tree(entries)["totals"]["calls"] == 2
    assert not trace.active()


def test_old_traces_are_pruned(tmp_path, monkeypatch):
    from job_agent import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    for n in range(trace.KEEP_RUNS + 3):
        (trace.folder(3)).mkdir(parents=True, exist_ok=True)
        (trace.folder(3) / f"2026010{n:02d}-000000-prepare.jsonl").write_text("{}\n")
    trace.prune(3)
    assert len(trace.runs(3)) == trace.KEEP_RUNS


def test_pages_show_the_timeline(ctx, tmp_path, monkeypatch):  # noqa: F811
    import threading
    from http.server import ThreadingHTTPServer

    from job_agent import dashboard
    from test_dashboard import request

    job_id = add_job(ctx, "4001")
    prepare.prepare(job_id, ctx, log=lambda *_: None)
    monkeypatch.setattr(dashboard, "DB_PATH", tmp_path / "data" / "t.db")
    app = dashboard.Dashboard(ctx.settings, 0)
    server = ThreadingHTTPServer(("127.0.0.1", 0), None)
    app.port = server.server_port
    server.RequestHandlerClass = dashboard.handler_for(app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert f'href="/job/{job_id}/trace"' in request(app.port, "GET", f"/job/{job_id}")[1]
        page = request(app.port, "GET", f"/job/{job_id}/trace")[1]
        for text in ("How this was made", "Fit check", "Score 85", "Tailoring the resume",
                     "Only text a person can see reaches the model", "⚑ Caught", "✓ Check",
                     "Show prompts and replies", 'src="/trace.js"'):
            assert text in page, text
        assert "Recent runs" in request(app.port, "GET", "/activity")[1]
        how = request(app.port, "GET", "/how")[1]
        assert "The journey of one application" in how and "Why does code submit" in how
        assert request(app.port, "GET", "/trace.js")[0] == 200
        assert "No runs recorded" in request(app.port, "GET", "/job/999/trace")[1]
    finally:
        server.shutdown()
