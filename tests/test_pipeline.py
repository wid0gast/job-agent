"""Prepare → approve → submit against the demo Greenhouse, with a scripted model.

Real browser (headless), real form filling, no network and no model cost.
"""

import json
import shutil
import threading
from dataclasses import replace
from http.server import ThreadingHTTPServer

import pytest

from job_agent import (actions, answers, companies, config, demo_greenhouse, demo_site,
                       greenhouse, prepare, submit)
from job_agent.tracker import Tracker
from job_agent.worker import Context

ROOT = config.ROOT


class ScriptedModel:
    """Stands in for the model's json() and records what each step was sent."""

    model = "scripted/model"

    def __init__(self):
        self.seen: list[tuple[str, str]] = []

    def json(self, system, user, schema, name):
        self.seen.append((name, user))
        data = json.loads(user)
        if name == "fit":
            return {"score": 85, "summary": "Strong match.", "flags": [], "requirements": [
                {"requirement": "PyTorch", "must_have": True, "met": "yes", "evidence": "resume"}]}
        if name == "tailored_resume":
            from job_agent import latex

            return {**latex.from_markdown(data["resume"]), "matched": ["PyTorch"],
                    "emphasized": ["research"], "gaps": []}
        if name == "form_answers":
            replies = []
            for q in data["questions"]:
                if q["label"].startswith("Why"):
                    replies.append({"key": q["key"], "value": "I build evaluation tools.", "values": [],
                                    "supported": True, "evidence": "resume: NYU Data Lab"})
                elif q["kind"] == "multiselect":
                    pick = "Python" if "languages" in q["label"] else "NLP"
                    replies.append({"key": q["key"], "value": "", "values": [pick],
                                    "supported": True, "evidence": "resume"})
                else:
                    replies.append({"key": q["key"], "value": "", "values": [],
                                    "supported": False, "evidence": "not in profile"})
            return {"answers": replies}
        raise AssertionError(f"unexpected step {name}")


@pytest.fixture()
def site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), demo_site.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture()
def env(site, tmp_path, monkeypatch):
    """Profile, data and the demo Greenhouse wired in; returns (settings, tracker)."""
    profile = tmp_path / "profile"
    shutil.copytree(ROOT / "profile.example", profile)
    monkeypatch.setattr(config, "PROFILE_DIR", profile)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(answers, "APPROVED_BANK", tmp_path / "data" / "approved-answers.json")
    monkeypatch.setattr(companies, "COMPANIES_FILE", tmp_path / "companies.csv")
    monkeypatch.setattr(companies, "LEARNED_FILE", tmp_path / "data" / "learned.yaml")
    monkeypatch.setattr(greenhouse, "API", f"{site}/gh-api/v1/boards")
    monkeypatch.setattr(greenhouse, "BOARDS", f"{site}/gh")
    monkeypatch.setattr(demo_greenhouse, "FORM_EXTRA_REQUIRED", False)
    demo_greenhouse.SUBMISSIONS.clear()
    settings = replace(config.Settings.from_env(require_model=False), backend="off",
                       browser_channel="chromium", cover_letter="required")
    return settings, Tracker(tmp_path / "data" / "t.db")


@pytest.fixture()
def ctx(env, tmp_path):
    settings, tracker = env
    context = Context(settings, tracker, profile_dir=tmp_path / "chrome", headless_window=True)
    context.llm = ScriptedModel()
    yield context
    context.close()


def add_job(ctx, job_id):
    tracker = ctx.tracker if hasattr(ctx, "tracker") else ctx
    tracker.add_lead(f"https://demo.example/gh/contoso/jobs/{job_id}", "test", "Contoso AI",
                         demo_greenhouse.JOBS[job_id], "New York, NY", ats="greenhouse",
                         ats_job_id=job_id, ats_board=None, job_type="internship",
                         details={"locations": ["NYC"], "terms": ["Summer 2027"]})
    return tracker.leads(10)[-1]["id"]


def by_key(packet, key):
    return next(a for a in packet["answers"] if a["key"] == key)


def test_prepare_approve_submit(ctx):
    job_id = add_job(ctx, "4001")
    assert prepare.prepare(job_id, ctx, log=lambda *_: None) == "prepared"
    job = ctx.tracker.get(job_id)
    packet = json.loads(open(f"{job['packet_dir']}/packet.json").read())

    # The board was found from the job ID alone, and hidden text never reached the model.
    assert packet["job"]["board"] == "contoso"
    assert all(demo_greenhouse.TRAP_WORD not in sent for _, sent in ctx.llm.seen)
    # Profile answers come from code; the model only saw what was left.
    assert by_key(packet, "first_name")["value"] == "Jane" and by_key(packet, "first_name")["source"] == "profile"
    assert by_key(packet, "question_9001")["value"] == "Yes"      # authorized to work
    assert by_key(packet, "question_9002")["value"] == "No"       # sponsorship
    assert by_key(packet, "gender")["value"] == "Decline to self-identify"
    assert by_key(packet, "degree--0")["value"] == "Bachelor's Degree"   # page-only field
    assert by_key(packet, "country")["kind"] == "search"
    assert by_key(packet, "question_9003")["source"] == "model-essay"
    assert by_key(packet, "question_9005")["value"] == ["NLP"]
    assert "website_hp" not in {a["key"] for a in packet["answers"]}, "honeypot exposed"
    asked = json.loads(next(u for n, u in ctx.llm.seen if n == "form_answers"))["questions"]
    assert {q["key"] for q in asked} == {"question_9003", "question_9005", "question_9006"}
    assert by_key(packet, "question_9006")["widget"] == "checkbox"

    actions.approve(ctx.tracker, ctx.settings, job_id)
    assert ctx.tracker.get(job_id)["status"] == "approved"

    asked_codes = []

    def ask(question):
        asked_codes.append(question)
        return demo_greenhouse.LAST_CODE["value"]

    assert submit.submit(job_id, ctx, ask, log=lambda *_: None) == "submitted"
    assert asked_codes and "security code" in asked_codes[0]
    sent = demo_greenhouse.SUBMISSIONS[-1]
    assert sent["first_name"] == "Jane" and sent["question_9001"] == "Yes"
    assert sent["school--0"] == "New York University" and sent["country"] == "United States +1"
    assert sent["question_9005"] == "NLP" and sent["resume_filename"].endswith("_Resume.pdf")
    assert sent["question_9006[]"] == ["Python"]
    assert not sent.get("website_hp")
    assert demo_greenhouse.TRAP_WORD not in json.dumps(sent)
    job = ctx.tracker.get(job_id)
    assert job["status"] == "submitted"
    packet = json.loads(open(f"{job['packet_dir']}/packet.json").read())
    assert packet["submission"]["screenshot"].endswith("submitted.png")


def test_changed_form_goes_back_to_you(ctx, monkeypatch):
    job_id = add_job(ctx, "4002")
    prepare.prepare(job_id, ctx, log=lambda *_: None)
    actions.approve(ctx.tracker, ctx.settings, job_id)
    monkeypatch.setattr(demo_greenhouse, "FORM_EXTRA_REQUIRED", True)
    assert submit.submit(job_id, ctx, lambda q: "", log=lambda *_: None) == "needs_you"
    job = ctx.tracker.get(job_id)
    assert job["status"] == "needs_you" and "New required question" in job["reason"]
    assert demo_greenhouse.SUBMISSIONS == []


def test_approval_blocks_missing_and_edits_reset_approval(ctx):
    job_id = add_job(ctx, "4001")
    prepare.prepare(job_id, ctx, log=lambda *_: None)
    actions.edit_answer(ctx.tracker, job_id, "question_9003", "")
    with pytest.raises(ValueError, match="Answer these first"):
        actions.approve(ctx.tracker, ctx.settings, job_id)
    actions.edit_answer(ctx.tracker, job_id, "question_9003", "I like evaluation work.")
    actions.edit_answer(ctx.tracker, job_id, "question_9002", "Yes")
    with pytest.raises(ValueError, match="profile says"):
        actions.approve(ctx.tracker, ctx.settings, job_id)  # contradicts work authorization
    actions.edit_answer(ctx.tracker, job_id, "question_9002", "No")
    actions.approve(ctx.tracker, ctx.settings, job_id)
    actions.edit_answer(ctx.tracker, job_id, "question_9004", "https://linkedin.com/in/jane")
    assert ctx.tracker.get(job_id)["status"] == "prepared"  # approval covers exact content
