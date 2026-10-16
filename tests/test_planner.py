"""The planner agent drives the real pipeline (demo Greenhouse, headless browser)
with a scripted model: tools work, guards hold, and the trace records it all."""

import json
from dataclasses import replace

from job_agent import packets, planner
from job_agent.harness import read_trace
from test_pipeline import ScriptedModel, add_job, ctx, env, site  # noqa: F401 - fixtures


class AgentModel(ScriptedModel):
    """Pipeline steps answer as in test_pipeline; agent turns come from `steps`,
    each a function of the last tool result."""

    def __init__(self, steps):
        super().__init__()
        self.steps = list(steps)

    def complete(self, messages, tools):
        last = json.loads(messages[-1]["content"]) if messages[-1]["role"] == "tool" else None
        name, args = self.steps.pop(0)(last)
        return {"role": "assistant", "content": f"Next: {name}.",
                "tool_calls": [{"id": f"c{len(self.steps)}", "type": "function",
                                "function": {"name": name, "arguments": json.dumps(args)}}]}


def test_planner_prepares_asks_redoes_and_respects_guards(ctx):  # noqa: F811 - pytest fixture
    ctx.settings = replace(ctx.settings, research="off")  # Research has its own test.
    first, second = add_job(ctx, "4001"), add_job(ctx, "4002")
    steps = [
        lambda last: ("queue_status", {}),
        lambda last: ("list_jobs", {}),
        lambda last: ("check_caps", {"job_id": first}),
        lambda last: ("prepare_job", {"job_id": first}),
        lambda last: ("ask_user", {"question": "Two applications ask your start date. When can you start?"}),
        lambda last: ("fill_answer", {"job_id": first, "key": "question_x", "value": "Next Tuesday"}),
        lambda last: ("redo", {"job_id": first, "note": "Lead with the research experience."}),
        lambda last: ("finish", {"summary": "1 packet ready for review."}),
    ]
    results = []

    def step_with_capture(fn):
        return lambda last: (results.append(last), fn(last))[1]

    ctx.llm = AgentModel([step_with_capture(s) for s in steps])
    asked = []
    report = planner.run("Fill the queue with my best ML internship.", ctx,
                         lambda q: asked.append(q) or "I can start June 2027.", lambda *_: None, task_id=7)

    assert report == "1 packet ready for review."
    listed = results[2]["result"]
    assert {j["id"] for j in listed["jobs"]} == {first, second} and listed["jobs"][0]["how"] == "coded"
    assert results[3]["result"]["can_apply"] is True
    prepared = results[4]["result"]
    assert prepared["status"] == "prepared" and prepared["fit"]["score"] == 85
    assert asked == ["Two applications ask your start date. When can you start?"]
    # A value the user never said is refused, and the model is told why.
    assert results[6]["ok"] is False and "user gave" in results[6]["error"]
    # The redo note reached tailoring and is on the packet.
    assert any("Lead with the research experience." in user for name, user in ctx.llm.seen
               if name == "tailored_resume")
    assert packets.load(ctx.tracker.get(first))["reviewer_note"] == "Lead with the research experience."
    assert ctx.tracker.get(second)["status"] == "found"  # It never touched the other job.

    kinds = [e["kind"] for e in read_trace(planner.trace_path(7))]
    assert kinds.count("call") == 8 and kinds[-1] == "finish"
