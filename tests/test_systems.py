"""Prepare → approve → submit on the demo Lever and Ashby, with a scripted model."""

import json

import pytest

from job_agent import actions, ashby, demo_ashby, demo_lever, lever, prepare, submit
from job_agent.worker import Context

from test_pipeline import ScriptedModel, env as env, site as site  # noqa: F401 - fixtures


class Model(ScriptedModel):
    """Also answers yes/no availability questions and picks the first listed area."""

    def json(self, system, user, schema, name):
        if name != "form_answers":
            return super().json(system, user, schema, name)
        self.seen.append((name, user))
        replies = []
        for q in json.loads(user)["questions"]:
            reply = {"key": q["key"], "value": "", "values": [], "supported": True, "evidence": "resume"}
            if q["kind"] == "textarea":
                reply["value"] = "I build retrieval systems and evaluations."
            elif q["kind"] == "multiselect":
                reply["values"] = [q["options"][0]]
            elif "Yes" in q["options"]:
                reply["value"] = "Yes"
            else:
                reply.update(supported=False, evidence="not in profile")
            replies.append(reply)
        return {"answers": replies}


@pytest.fixture()
def ctx(env, site, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(lever, "API", f"{site}/lever-api/v0/postings")
    monkeypatch.setattr(lever, "API_EU", f"{site}/lever-api/v0/postings")
    monkeypatch.setattr(lever, "JOBS", f"{site}/lever")
    monkeypatch.setattr(ashby, "GRAPHQL", f"{site}/ashby-api/non-user-graphql")
    monkeypatch.setattr(ashby, "JOBS", f"{site}/ashby")
    demo_lever.SUBMISSIONS.clear()
    demo_ashby.SUBMISSIONS.clear()
    settings, tracker = env
    context = Context(settings, tracker, profile_dir=tmp_path / "chrome", headless_window=True)
    context.llm = Model()
    yield context
    context.close()


def run(ctx, ats, board, job_id, title):
    ctx.tracker.add_lead(f"https://demo.example/{ats}/{job_id}", "test", "Demo Co", title,
                         "New York, NY", ats=ats, ats_board=board, ats_job_id=job_id,
                         job_type="internship", details={"locations": ["NYC"], "terms": ["Summer 2027"]})
    job = ctx.tracker.leads(10)[-1]["id"]
    assert prepare.prepare(job, ctx, log=print) == "prepared", ctx.tracker.get(job)["reason"]
    actions.approve(ctx.tracker, ctx.settings, job)
    assert submit.submit(job, ctx, lambda q: "", log=print) == "submitted", ctx.tracker.get(job)["reason"]
    sent_to_model = " ".join(user for _, user in ctx.llm.seen)
    assert demo_lever.TRAP_WORD not in sent_to_model, "hidden posting text reached the model"


def test_lever(ctx):
    run(ctx, "lever", demo_lever.BOARD, demo_lever.JOB, demo_lever.TITLE)
    sent = demo_lever.SUBMISSIONS[-1]
    card = f"cards[{demo_lever.CARD}]"
    assert sent["name"] == "Jane Doe"                      # typed over Lever's resume pre-fill
    assert sent["selectedLocation"] == "New York, NY, United States"
    assert sent[f"{card}[field0]"] == "No"                 # visa sponsorship, from the profile
    assert sent[f"{card}[field1]"] == "Search"             # checkbox group
    assert sent[f"{card}[field2]"] == "Yes"                # radio group
    assert sent["eeo[gender]"] == "Decline to self-identify"
    assert sent["resume_filename"].endswith("_Resume.pdf") and not sent.get("website_hp")


def test_ashby(ctx):
    run(ctx, "ashby", demo_ashby.BOARD, demo_ashby.JOB, demo_ashby.TITLE)
    sent = demo_ashby.SUBMISSIONS[-1]
    assert sent["_systemfield_name"] == "Jane Doe" and sent["location"] == "New York, NY, USA"
    assert "q-sponsor" not in sent                          # "No" pressed: the checkbox stays off
    assert sent[f"{demo_ashby.FORM_ID}_q-source"] == "Company careers page"
    assert sent["school"] == "New York University" and sent["degree"] == "B.S. Computer Science"
    assert (sent["end_month"], sent["end_year"]) == ("May", "2027") and sent["still_student"] == "on"
    assert "retrieval" in sent["q-why"] and not sent.get("website_hp")
    assert sent["resume_filename"].endswith("_Resume.pdf")
