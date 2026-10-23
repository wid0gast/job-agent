"""A2 end to end on the demo site's custom ATS (no coded adapter): the explore
agent (a scripted policy here) creates an account with an emailed code, fills
an iframe form and stops at submit; after approval, code replays it."""

import json
from dataclasses import replace
from urllib.parse import urlsplit

from job_agent import actions, config, demo_site, packets, prepare, submit
from test_pipeline import ScriptedModel, ctx, env, site  # noqa: F401 - fixtures


class ExploreModel(ScriptedModel):
    """Pipeline steps as in test_pipeline; agent turns from a simple policy:
    Apply, then every visible field once, then the submit button."""

    def __init__(self):
        super().__init__()
        self.done, self.calls = set(), []

    def complete(self, messages, tools, task="agent"):
        last = json.loads(messages[-1]["content"]) if messages[-1]["role"] == "tool" else None
        result = (last or {}).get("result")
        page = (result.get("page") if isinstance(result, dict) and "page" in result
                else result if isinstance(result, dict) and "elements" in result else None)
        name, args = ("snapshot", {}) if page is None else self.decide(page)
        self.calls.append((name, args))
        return {"role": "assistant", "tool_calls": [{"id": str(len(self.calls)), "type": "function",
                                                     "function": {"name": name, "arguments": json.dumps(args)}}]}

    def decide(self, page):
        path, elements = urlsplit(page["url"]).path, page["elements"]
        if path.startswith("/jobs/"):
            return "click", {"ref": next(e["ref"] for e in elements if e["name"] == "Apply")}
        for e in elements:
            key = (path, e.get("group") or e["name"])
            if key in self.done or e["tag"] in ("a", "button"):
                continue
            self.done.add(key)
            if e.get("type") == "password":
                return "fill_password", {"ref": e["ref"]}
            if e.get("type") == "file":
                return "upload_document", {"ref": e["ref"], "document": "resume"}
            if path == "/verify":
                return "enter_code", {"ref": e["ref"]}
            return "fill_field", {"ref": e["ref"]}
        button = next(e for e in elements if e["tag"] == "button")
        if button["name"].lower().startswith("submit"):
            return "final_step", {"ref": button["ref"], "summary": "Filled the DemoATS form."}
        return "click", {"ref": button["ref"]}


def test_explore_then_replay_on_a_custom_site(ctx, site, monkeypatch):  # noqa: F811 - pytest fixtures
    ctx.settings = replace(ctx.settings, research="off")  # Research has its own test.
    received, passwords = [], {}
    monkeypatch.setattr(demo_site, "report_submission", lambda job_id, data: received.append(data))
    monkeypatch.setattr(config, "get_password", lambda service, email: passwords.get((service, email)))
    monkeypatch.setattr(config, "set_password", lambda service, email, pw: passwords.update({(service, email): pw}))
    ctx.tracker.add_lead(f"{site}/jobs/102", "test", "Contoso AI", "Machine Learning Intern", "Remote",
                         ats="demoats", job_type="internship",
                         details={"locations": ["Remote"], "terms": ["Summer 2027"]})
    job_id = ctx.tracker.leads(10)[-1]["id"]
    email = config.load_profile()["contact"]["email"]
    asked = []
    ctx.ask = lambda question: asked.append(question) or (
        "yes" if "needs an account" in question else demo_site.ACCOUNTS[email]["code"])
    ctx.llm = ExploreModel()

    status = prepare.prepare(job_id, ctx, log=lambda *_: None)
    assert status == "prepared", ctx.tracker.get(job_id)["reason"]
    packet = packets.load(ctx.tracker.get(job_id))
    steps = packet["recording"]["steps"]
    assert [s["action"] for s in steps][-1] == "submit" and steps[0]["action"] == "click"
    assert any(s.get("secret") == "password" and s["auth"] for s in steps)
    assert any(s.get("secret") == "code" for s in steps) and len(asked) == 2  # Allow the account, then the code.
    # Nothing was typed that didn't come from the profile, the model's supported answers or documents.
    labels = {a["label"]: a for a in packet["answers"]}
    assert labels["First name"]["value"] == "Jane" and labels["First name"]["source"] != "model"
    assert labels["Why do you want to work here? (2–4 sentences)"]["value"] == "I build evaluation tools."
    assert "Leave this field empty" not in labels  # The honeypot never reached the agent.
    certify = next(a for a in packet["answers"] if a["label"].startswith("I certify"))
    assert certify["value"] in (None, "")  # Not the model's call: the user answers it.
    assert received == []  # Exploring never submits.

    # Review: the user ticks the certification, approves; code replays and submits.
    actions.edit_answer(ctx.tracker, job_id, certify["key"], "Yes")
    actions.approve(ctx.tracker, ctx.settings, job_id)
    assert submit.submit(job_id, ctx, ctx.ask, log=lambda *_: None) == "submitted"
    data = received[0]
    assert data["first_name"] == "Jane" and data["email"] == email and data["agree"] == "yes"
    assert data["resume_filename"].endswith(".pdf") and not data.get("website")
    assert demo_site.TRAP_WORD not in json.dumps(data).lower()
    assert len(asked) == 2  # Already signed in: replay skipped the account steps.


def test_no_account_without_the_users_yes(ctx, site, monkeypatch):  # noqa: F811 - pytest fixtures
    ctx.settings = replace(ctx.settings, research="off")
    monkeypatch.setattr(config, "get_password", lambda service, email: None)
    monkeypatch.setattr(config, "set_password", lambda *a: (_ for _ in ()).throw(AssertionError("stored")))
    ctx.tracker.add_lead(f"{site}/jobs/102", "test", "Contoso AI", "Machine Learning Intern", "Remote",
                         ats="demoats", job_type="internship",
                         details={"locations": ["Remote"], "terms": ["Summer 2027"]})
    job_id = ctx.tracker.leads(10)[-1]["id"]
    ctx.ask = lambda question: "no thanks"
    ctx.llm = ExploreModel()
    assert prepare.prepare(job_id, ctx, log=lambda *_: None) == "failed"
    assert "didn't allow an account" in ctx.tracker.get(job_id)["reason"]
    assert ctx.tracker.db.execute("select count(*) from accounts").fetchone()[0] == 0


def test_a_posting_inside_a_frame_is_read_and_page_data_stays_text(ctx, site):  # noqa: F811
    from job_agent import web

    company, title, _, desc = demo_site.JOBS["102"]
    posting = web.fetch(ctx, {"url": f"{site}/framed/102", "title": title})
    text = ctx.headless.visible_text(posting["html"])  # As prepare reads a page: no second unescape.
    assert desc.split(".")[0] in text and web.title_found(text, title)
    assert text.index(title) < text.index("Personal Data"), "the posting comes before the page's own text"
    assert not any("navbar" in h for h in ctx.headless.last_hidden), "page data isn't reported as hidden text"


def test_title_found_allows_word_forms_but_not_another_page():
    from job_agent.web import title_found

    page = "Machine Learning Internship (Summer) at Contoso. Responsibilities: train models."
    assert title_found(page, "Machine Learning Intern - Summer 2027 (MS/PhD)")
    assert title_found("R&D Engineering Intern, Cleveland", "R&D Engineer Intern - Research & Development Engineering")
    assert not title_found("This Website collects some Personal Data from its Users. " * 50,
                           "Quantitative Research Internship - PhD")


def test_a_page_that_never_names_the_job_stops_before_any_guessing(ctx, site, monkeypatch):  # noqa: F811
    from job_agent import web

    ctx.tracker.add_lead(f"{site}/jobs/102", "test", "Contoso AI", "Quantitative Research Internship", "Remote",
                         ats="other", job_type="internship", details={"locations": ["Remote"]})
    job_id = ctx.tracker.leads(10)[-1]["id"]
    monkeypatch.setattr(web, "fetch", lambda ctx, job: {
        "html": "<p>This Website collects some Personal Data from its Users.</p>" * 40, "url": job["url"]})
    assert prepare.prepare(job_id, ctx, log=lambda *_: None) == "failed"
    assert "never mentions" in ctx.tracker.get(job_id)["reason"]
    assert not any(name == "fit" for name, _ in ctx.llm.seen)


def test_prepare_reads_a_framed_posting_whole(ctx, site):  # noqa: F811
    # Unescaping a rendered page again once turned "&lt;!--" in page data into a comment that hid
    # the whole posting. With no model set up, prepare stops right after reading.
    ctx.llm = None
    for n, path in enumerate(("framed", "inline")):  # Different companies, or they'd be one job.
        ctx.tracker.add_lead(f"{site}/{path}/102", "test", f"Contoso AI {n}", "Machine Learning Intern", "Remote",
                             ats="other", job_type="internship", details={"locations": ["Remote"]})
        job = ctx.tracker.leads(10)[-1]
        assert job["url"].endswith(f"/{path}/102")
        assert prepare.prepare(job["id"], ctx, log=lambda *_: None) == "found", ctx.tracker.get(job["id"])["reason"]


def test_a_site_that_refuses_the_reader_says_so(ctx, site, monkeypatch):  # noqa: F811
    from job_agent import web

    ctx.tracker.add_lead(f"{site}/jobs/101", "test", "Keysight", "EDA Software Intern", "Remote",
                         ats="other", job_type="internship", details={"locations": ["Remote"]})
    job_id = ctx.tracker.leads(10)[-1]["id"]
    monkeypatch.setattr(web, "fetch", lambda ctx, job: {"html": "<h1>403 Forbidden</h1>", "url": job["url"],
                                                        "status": 403})
    assert prepare.prepare(job_id, ctx, log=lambda *_: None) == "failed"
    assert "answered 403" in ctx.tracker.get(job_id)["reason"]
