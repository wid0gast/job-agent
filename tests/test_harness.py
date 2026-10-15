import json

from job_agent.harness import AgentRun, StopRun, Tool, read_trace, schema


class ScriptedModel:
    """Returns pre-written assistant messages and records what it was sent."""

    def __init__(self, replies):
        self.replies, self.seen = list(replies), []

    def complete(self, messages, tools):
        self.seen.append(json.loads(json.dumps(messages)))
        return self.replies.pop(0)


def call(name, args, id_):
    return {"id": id_, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def test_loop_runs_tools_reports_errors_prunes_snapshots_and_stops(tmp_path):
    def finish(report):
        raise StopRun(report)

    def explode():
        raise ValueError("boom")

    tools = [
        Tool("snap", "", schema(), lambda: {"page": "x" * 50}, ephemeral=True),
        Tool("explode", "", schema(), explode),
        Tool("finish", "", schema({"report": {"type": "string"}}), finish),
    ]
    model = ScriptedModel([
        {"role": "assistant", "content": "Looking first.", "tool_calls": [call("snap", {}, "1")]},
        {"role": "assistant", "tool_calls": [call("snap", {}, "2"), call("explode", {}, "3")]},
        {"role": "assistant", "content": "Thinking out loud."},  # No tools: nudged once.
        {"role": "assistant", "tool_calls": [call("snap", {}, "4")]},
        {"role": "assistant", "tool_calls": [call("finish", {"report": "done"}, "5")]},
    ])
    run = AgentRun(model, "system", tools, tmp_path / "trace.jsonl", keep_snapshots=2)
    assert run.run("go") == "done"

    last_request = model.seen[-1]
    snaps = [m["content"] for m in last_request if m.get("name") == "snap"]
    assert snaps[0].startswith('{"pruned"') and "xxxx" in snaps[-1]
    error = next(m for m in last_request if m.get("name") == "explode")
    assert "boom" in error["content"] and '"ok": false' in error["content"]
    assert any("call finish" in m["content"] for m in last_request if m["role"] == "user")
    kinds = [e["kind"] for e in read_trace(tmp_path / "trace.jsonl")]
    assert kinds[0] == "task" and kinds[-1] == "finish" and "thought" in kinds and "result" in kinds


def test_step_limit_ends_the_run(tmp_path):
    tools = [Tool("noop", "", schema(), lambda: "ok")]
    model = ScriptedModel([{"role": "assistant", "tool_calls": [call("noop", {}, str(i))]} for i in range(5)])
    run = AgentRun(model, "system", tools, tmp_path / "trace.jsonl", max_steps=3)
    assert "after 3 steps" in run.run("go")
