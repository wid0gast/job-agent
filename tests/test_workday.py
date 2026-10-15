"""Workday on the demo site: account, verification, page-by-page walk, new questions."""

import json
import urllib.request

import pytest

from job_agent import actions, demo_workday, prepare, submit, workday
from job_agent.worker import Context

from test_systems import Model
from test_pipeline import env as env, site as site  # noqa: F401 - fixtures


class Passwords:
    """Stands in for the macOS Keychain."""

    def __init__(self):
        self.saved = {}

    def get(self, site, email):
        return self.saved.get((site, email))

    def set(self, site, email, password):
        self.saved[(site, email)] = password


@pytest.fixture()
def ctx(env, site, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(workday, "PASSWORDS", Passwords())
    demo_workday.ACCOUNTS.clear()
    demo_workday.SUBMISSIONS.clear()
    settings, tracker = env
    context = Context(settings, tracker, profile_dir=tmp_path / "chrome", headless_window=True)
    context.llm = Model()
    context.site = site
    yield context
    context.close()


def test_workday_account_walk_new_question_then_submit(ctx):
    url = f"{ctx.site}/wd/{demo_workday.SITE}/{demo_workday.JOB_PATH}"
    ctx.tracker.add_lead(url, "test", demo_workday.COMPANY, demo_workday.TITLE, "New York, NY",
                         ats="workday", job_type="internship",
                         details={"locations": ["NYC"], "terms": ["Summer 2027"]})
    job = ctx.tracker.leads(10)[-1]["id"]

    # Prepare: the site wants an account first, so only standard fields are drafted.
    assert prepare.prepare(job, ctx, log=print) == "prepared", ctx.tracker.get(job)["reason"]
    packet = json.loads(open(ctx.tracker.get(job)["packet_dir"] + "/packet.json").read())
    keys = {a["key"]: a for a in packet["answers"]}
    assert keys["formField-legalName--firstName"]["value"] == "Jane"
    assert keys["formField-source"]["value"] == "Company careers page"
    assert demo_workday.TRAP_WORD not in " ".join(user for _, user in ctx.llm.seen)
    actions.approve(ctx.tracker, ctx.settings, job)

    questions = []

    def ask(question):
        questions.append(question)
        if "verification" in question.lower() or "verify" in question.lower():
            urllib.request.urlopen(ctx.site + demo_workday.LAST_LINK["value"]).read()
        return "done"

    # First walk: creates the account, verifies, fills pages 1-2, finds a new question
    # on Application Questions and stops before anything is submitted.
    assert submit.submit(job, ctx, ask, log=print) == "needs_you"
    assert any("verif" in q.lower() for q in questions)
    row = ctx.tracker.get(job)
    assert "new question" in row["reason"] and demo_workday.SUBMISSIONS == []
    packet = json.loads(open(row["packet_dir"] + "/packet.json").read())
    new = next(a for a in packet["answers"] if a["key"] == "formField-q-why")
    assert new["value"] in (None, "") and new["section"] == "Application Questions"
    # The sponsorship question on the same page was answered from the profile.
    assert next(a for a in packet["answers"] if a["key"] == "formField-q-sponsor")["value"] == "No"

    # The model drafts the new answer (a separate step), you approve again.
    prepare.draft_missing(job, ctx, log=print)
    assert ctx.tracker.get(job)["status"] == "prepared"
    actions.approve(ctx.tracker, ctx.settings, job)

    # Second walk: signs in with the stored password and submits on Review.
    assert submit.submit(job, ctx, ask, log=print) == "submitted", ctx.tracker.get(job)["reason"]
    sent = demo_workday.SUBMISSIONS[-1]
    assert sent["legalName--firstName"] == "Jane" and sent["source"] == "Company Careers Page"
    assert sent["country"] == "United States of America" and sent["phoneType"] == "Mobile"
    assert sent["candidateIsPreviousWorker"] == "No" and sent["q-sponsor"] == "No"
    assert "retrieval" in sent["q-why"] and sent["acceptTerms"] == "yes"
    assert sent["gender"] == "Decline to self-identify"
    assert sent["resume_filename"].endswith("_Resume.pdf")
    assert len(demo_workday.ACCOUNTS) == 1                  # one account, reused
