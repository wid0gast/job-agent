import json

from job_agent.harness import AgentRun, StopRun, Tool, read_trace, schema


class ScriptedModel:
    """Returns pre-written assistant messages and records what it was sent."""

    def __init__(self, replies):
        self.replies, self.seen = list(replies), []

    def complete(self, messages, tools, task="agent"):
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


def test_finishing_before_any_tool_ran_is_refused_once(tmp_path):
    """An agent that gives up before calling a tool (often: it tried a tool of its own) is told how tools work."""
    import json

    from job_agent.harness import AgentRun, StopRun, Tool, schema

    replies = [("finish", {"summary": "No search tool, so no one found."}), ("look", {}),
               ("finish", {"summary": "Done."})]

    class Model:
        seen = []

        def complete(self, messages, tools, task="agent"):
            if messages[-1]["role"] == "tool":
                self.seen.append(json.loads(messages[-1]["content"]))
            name, args = replies.pop(0)
            return {"tool_calls": [{"id": name, "type": "function",
                                    "function": {"name": name, "arguments": json.dumps(args)}}]}

    def finish(summary):
        raise StopRun(summary)

    model = Model()
    run = AgentRun(model, "system", [Tool("look", "Look.", schema(), lambda: "seen"),
                                     Tool("finish", "Finish.", schema({"summary": {"type": "string"}}), finish)],
                   tmp_path / "t.jsonl")
    assert run.run("task") == "Done."
    assert "No tool has run yet" in model.seen[0]["error"]


def test_a_batch_is_checked_before_any_of_it_runs(tmp_path):
    ran = []

    def save(job_id, document):
        ran.append((job_id, document))
        return "saved"

    def finish(report):
        raise StopRun(report)

    tools = [Tool("save", "", schema({"job_id": {"type": "integer"},
                                      "document": {"type": "string", "enum": ["resume", "cover_letter"]}}), save),
             Tool("finish", "", schema({"report": {"type": "string"}}), finish)]
    model = ScriptedModel([
        {"role": "assistant", "tool_calls": [call("save", {"job_id": 3, "document": "resume"}, "1"),
                                             call("save", {"job_id": "3", "document": "letter"}, "2")]},
        {"role": "assistant", "tool_calls": [call("finish", {"report": "done"}, "3")]},
    ])
    AgentRun(model, "system", tools, tmp_path / "trace.jsonl").run("go")
    assert ran == [(3, "resume")], "the bad call never ran"
    refused = next(m for m in model.seen[-1] if m.get("tool_call_id") == "2")["content"]
    assert "Not run" in refused and "'job_id' should be integer" in refused and "one of resume" in refused


def test_a_task_you_stop_ends_at_its_next_step(tmp_path):
    import pytest

    from job_agent import stopping, trace
    from job_agent.tracker import Tracker

    tracker = Tracker(tmp_path / "jobs.db")
    queued = tracker.add_task("prepare", 1)
    assert tracker.stop_task(queued) == "Cancelled." and tracker.task(queued)["status"] == "stopped"

    submit = tracker.add_task("submit", 2)
    tracker.claim_task(kinds=("submit",))
    with pytest.raises(ValueError, match="can't be stopped"):
        tracker.stop_task(submit)  # The click may already have happened.

    prepare = tracker.add_task("prepare", 3)
    tracker.claim_task(kinds=("prepare",))
    stopping.watch(lambda: tracker.stop_asked(prepare))
    with trace.step("Fit check"):
        pass  # Not asked yet: work goes on.
    assert tracker.stop_task(prepare).startswith("Stopping")
    with pytest.raises(stopping.Stopped):
        with trace.step("Tailoring the resume"):
            raise AssertionError("a stopped task doesn't start its next step")
    stopping.watch(None)
