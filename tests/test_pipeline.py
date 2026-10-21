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


def test_every_degree_gets_its_own_education_row(ctx):
    """Greenhouse shows one education row; "Add another" is clicked for each further degree,
    and each row is filled from that degree (dates never come from the model)."""
    import yaml

    path = config.PROFILE_DIR / "resume-profile.yaml"
    profile = yaml.safe_load(path.read_text())
    profile["education"] = [
        {"school": "Columbia University", "degree": "Master of Science", "major": "Data Science",
         "graduation": "May 2028"},
        *profile["education"]]
    path.write_text(yaml.safe_dump(profile))
    job_id = add_job(ctx, "4001")
    assert prepare.prepare(job_id, ctx, log=lambda *_: None) == "prepared"
    packet = json.loads(open(f"{ctx.tracker.get(job_id)['packet_dir']}/packet.json").read())
    assert by_key(packet, "school--0")["value"] == "Columbia University"
    assert by_key(packet, "degree--0")["value"] == "Master's Degree"
    assert by_key(packet, "school--1")["value"] == "New York University"
    assert by_key(packet, "degree--1")["value"] == "Bachelor's Degree"

    actions.approve(ctx.tracker, ctx.settings, job_id)
    assert submit.submit(job_id, ctx, lambda q: demo_greenhouse.LAST_CODE["value"], log=lambda *_: None) == "submitted"
    sent = demo_greenhouse.SUBMISSIONS[-1]
    assert (sent["school--0"], sent["school--1"]) == ("Columbia University", "New York University")
    assert sent["degree--1"] == "Bachelor's Degree"


def test_the_model_never_supplies_dates_your_material_lacks():
    from job_agent import answers

    class Model:
        asked = []

        def json(self, system, user, schema, name):
            self.asked += [q["key"] for q in json.loads(user)["questions"]]
            return {"answers": [{"key": "available_from", "value": "June 2026", "values": [], "supported": True,
                                 "evidence": "guess"},
                                {"key": "graduation_date", "value": "May 2027", "values": [], "supported": True,
                                 "evidence": "resume"}]}

    def field(key, label):
        return {"key": key, "label": label, "kind": "text", "required": True, "options": [], "description": ""}

    fields = [field("start-year--0", "Start date year"), field("available_from", "Start date (month and year)"),
              field("graduation_date", "Graduation date")]
    model = Model()
    answers.ask_model(model, fields, {"education": [{"graduation": "May 2027"}]}, "B.S., May 2027",
                      {"company": "Acme", "title": "Intern"}, "", None, None)
    assert "start-year--0" not in model.asked and "enter it yourself" in fields[0]["note"]  # Education rows: never asked.
    assert fields[1].get("value") is None and "dates are left for you" in fields[1]["note"]  # Not in the resume.
    assert fields[2]["value"] == "May 2027"  # Stated in the resume: allowed.


def test_an_optional_letter_is_written_by_default_and_can_be_added_later(ctx):
    """COVER_LETTER defaults to "always": an optional letter field gets a letter. With "required",
    the timeline says why there is none, and "Write a cover letter" adds one to the packet."""
    from dataclasses import replace

    from job_agent import trace

    class Letters(ScriptedModel):
        def json(self, system, user, schema, name):
            if name == "cover_letter":
                self.seen.append((name, user))
                return {"letter_markdown": "Dear Contoso team,\\n\\nI build evaluation tools.\\n\\nJane Doe"}
            return super().json(system, user, schema, name)

    ctx.llm = Letters()
    ctx.settings = replace(ctx.settings, cover_letter="required", research="off")
    job_id = add_job(ctx, "4001")
    prepare.prepare(job_id, ctx, log=lambda *_: None)
    packet = json.loads(open(f"{ctx.tracker.get(job_id)['packet_dir']}/packet.json").read())
    assert not packet["documents"]["cover_letter"]
    notes = [e["text"] for e in trace.read(trace.runs(job_id)[0]) if e["type"] == "note"]
    assert any("No cover letter" in n and "optional" in n for n in notes)

    assert prepare.add_letter(job_id, ctx, log=lambda *_: None).startswith("Cover letter written")
    packet = json.loads(open(f"{ctx.tracker.get(job_id)['packet_dir']}/packet.json").read())
    assert packet["documents"]["cover_letter"] and by_key(packet, "cover_letter")["value"] == "cover_letter"

    assert config.Settings.from_env(require_model=False).cover_letter in ("always", "required", "never")
    assert config.letter_mode({"preferences": {"cover_letter": "never"}}, ctx.settings) == "never"
    assert config.letter_mode({"preferences": {}}, replace(ctx.settings, cover_letter="always")) == "always"


def test_one_field_that_wont_fill_never_leaves_the_rest_empty(ctx):
    """An edited essay saved with \\r\\n line breaks still fills (it once stopped a submit), and a field
    that really can't be filled is reported after every other field has been filled; nothing is sent."""
    job_id = add_job(ctx, "4001")
    prepare.prepare(job_id, ctx, log=lambda *_: None)
    actions.edit_answer(ctx.tracker, job_id, "question_9003", "First line.\r\nSecond line, with a URL:\r\nhttps://x.org")
    packet = json.loads(open(f"{ctx.tracker.get(job_id)['packet_dir']}/packet.json").read())
    assert "\r" not in by_key(packet, "question_9003")["value"]  # Edits are saved with \n.
    from job_agent import packets as packet_files

    stored = packet_files.load(ctx.tracker.get(job_id))  # A packet saved before that, with \r\n.
    by_key(stored, "question_9003")["value"] = "First line.\r\nSecond line, with a URL:\r\nhttps://x.org"
    packet_files.store(stored)
    actions.approve(ctx.tracker, ctx.settings, job_id)
    assert submit.submit(job_id, ctx, lambda q: demo_greenhouse.LAST_CODE["value"], log=lambda *_: None) == "submitted"
    assert demo_greenhouse.SUBMISSIONS[-1]["question_9003"].replace("\r\n", "\n") == \
        "First line.\nSecond line, with a URL:\nhttps://x.org"

    from job_agent import forms

    class Page:
        pass

    filled = []

    def fill_field(page, answer, value, files):
        if answer["key"] in ("degree--0", "gender"):
            raise forms.FillError(answer["key"], answer["label"], "no option matching")
        filled.append(answer["key"])

    try:
        forms.fill_all(Page(), packet, {}, fill_field)
    except forms.FillError as error:
        assert error.label == "2 fields" and "Degree" in error.problem and "Gender" in error.problem
    assert "first_name" in filled and "question_9003" in filled  # Everything else was still filled.
