"""A2: application systems without a coded adapter (iCIMS, SmartRecruiters,
custom career pages), handled by an agent that explores and code that replays.

EXPLORE (during prepare): an agent works through the site in the browser.
Every value it puts in a form comes from the same answer pipeline as coded
systems (profile, saved answers, then the model, which must cite support),
the user's own replies, the stored site password or the packet's documents;
it never types free text. It stops at the final submit and records every step
by stable target (name attribute, label, group question), not page refs.

REPLAY (at submit, after approval): code walks the recording with the
approved values. A missing step stops it as needs_you; the final click only
ever happens here. Submit calls walk() exactly as it does for Workday.
"""

import json
import secrets
import string
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError

from . import answers, config, forms
from .harness import AgentRun, StopRun, Tool, schema
from .pageview import FINAL_SUBMIT, PageView, follow_new_tab

MAX_QUESTIONS = 3
EXPLORES = True  # prepare.build: the form is read by exploring, after the documents exist.
KEYCHAIN = "job-agent"


def trace_name(job_id: int) -> str:
    return f"explore-{job_id}"


# ----- the posting ---------------------------------------------------------

def fetch(ctx, job: dict) -> dict:
    """The posting page, rendered (custom career sites are often built in JavaScript)."""
    page = ctx.headless.new_page()
    try:
        response = page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
        if response and response.status in (404, 410):
            raise forms.Closed(f"The posting page returns {response.status}.")
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except PlaywrightError:
            pass
        return {"html": page.inner_html("body"), "url": page.url}
    finally:
        page.close()


# ----- explore -------------------------------------------------------------

def explore(ctx, job: dict, description: str, document, log, facts=lambda: []) -> dict:
    """Run the explore agent. `document(kind)` returns a PDF path ("resume",
    "cover_letter"), making the letter on first request. Returns answers,
    recording and the agent's report; raises ValueError if it didn't reach
    the final step."""
    from .planner import trace_path
    from .prompts import EXPLORE_PROMPT

    browser = ctx.browser()
    page = browser.context.new_page()
    view = PageView(browser.context, page)
    view.goto(job["url"])
    explorer = Explorer(ctx, job, description, view, document, log,
                        getattr(ctx, "ask", None) or (lambda question: ""), facts)
    profile = config.load_profile()
    agent = AgentRun(ctx.llm, EXPLORE_PROMPT, explorer.tools(), trace_path(trace_name(job["id"])),
                     max_steps=70, max_seconds=15 * 60,
                     observer=lambda kind, data: log(f"    [{kind}] {str(data)[:160]}")
                     if kind in ("call", "finish") else None)
    report = agent.run(json.dumps({
        "job": {k: job[k] for k in ("company", "title", "location", "url")},
        "applicant": {"name": profile.get("contact", {}).get("name"),
                      "email": profile.get("contact", {}).get("email")},
        "documents": ["resume", "cover_letter (made when you upload it)"],
    }, ensure_ascii=False, indent=1))
    try:
        page.close()
    except PlaywrightError:
        pass
    if not explorer.final:
        raise ValueError(f"The agent didn't reach the final step: {report}")
    return {"answers": explorer.answers, "report": report,
            "recording": {"start": job["url"], "steps": explorer.steps}}


class Explorer:
    def __init__(self, ctx, job, description, view: PageView, document, log, ask, facts=lambda: []):
        self.ctx, self.job, self.description, self.view = ctx, job, description, view
        self.document, self.log, self.ask, self.facts = document, log, ask, facts
        self.profile, self.saved = config.load_profile(), config.load_answers()
        self.answers: list[dict] = []
        self.steps: list[dict] = []
        self.replies: list[str] = []
        self.auth_pages: set[str] = set()
        self.allowed_hosts: set[str] = set()
        self.final = False

    # ----- recording --------------------------------------------------------

    def target(self, ref: str) -> dict:
        element = self.view.element(ref)
        return {k: element.get(k) for k in ("frame", "field", "name", "tag", "type", "role", "group")
                if element.get(k) not in (None, "")}

    def record(self, ref: str | None, action: str, auth: bool = False, **extra) -> None:
        here = urlsplit(self.view.page.url).path
        auth = auth or here in self.auth_pages or self.view.has_password_field()
        if auth:
            self.auth_pages.add(here)
        step = {"action": action, "auth": auth, **extra}
        if ref is not None:
            step["target"] = self.target(ref)
        self.steps.append(step)

    def account_consent(self) -> None:
        """A page with a password box means an account in the user's name: ask first."""
        if not self.view.has_password_field():
            return
        email = self.profile.get("contact", {}).get("email") or "your email"
        host = urlsplit(self.view.page.url).hostname or ""
        if host in self.allowed_hosts or config.get_password(f"{KEYCHAIN}:{host}", email):
            return
        reply = self.ask(f"To apply to {self.job['title']} at {self.job['company']}, {host} needs an account "
                         f"with {email}. Reply yes to let the agent create one (the password is generated "
                         "and stored for you), or anything else to stop. Nothing is sent before you answer.")
        if reply.strip().lower() not in ("yes", "y", "ok", "yes please"):
            raise StopRun(f"Gave up: you didn't allow an account on {host}.")
        self.allowed_hosts.add(host)

    def observed(self, done: str) -> dict:
        return {"done": done, "page": self.view.snapshot()}

    # ----- tools ------------------------------------------------------------

    def snapshot(self):
        return self.view.snapshot()

    def read_text(self, offset: int = 0):
        return self.view.read_text(offset)

    def goto(self, url: str):
        self.view.goto(url)
        self.record(None, "goto", url=url)
        return self.observed(f"opened {url}")

    def click(self, ref: str):
        element = self.view.element(ref)
        self.account_consent()
        if FINAL_SUBMIT.match(element.get("name") or ""):
            raise PermissionError(f"{ref} ({element['name']!r}) looks like the final submit. "
                                  "Call final_step with it instead.")
        if element.get("type") in ("checkbox", "radio") or element.get("tag") in ("input", "select", "textarea"):
            raise ValueError("Use fill_field for form fields; click is for buttons and links.")
        self.record(ref, "click")
        self.view.click(ref)
        return self.observed(f"clicked {element.get('name')!r}")

    def scroll(self, direction: str):
        self.view.scroll(direction)
        return self.observed(f"scrolled {direction}")

    def fill_field(self, ref: str):
        """Answer a question from the profile/saved answers/model and fill it in."""
        element = self.view.element(ref)
        self.account_consent()
        if element.get("type") == "password":
            raise ValueError("That's a password box: use fill_password.")
        if element.get("type") == "file":
            raise ValueError("That's a file field: use upload_document.")
        field = self.field_for(ref, element)
        answer = next((a for a in self.answers if a["target"] == field["target"]), None)
        if answer is None:
            drafted, open_fields = answers.draft([field], self.profile, self.job, self.saved)
            answer = drafted[0]
            if open_fields and self.ctx.llm is not None:
                from .research import wanted

                answers.ask_model(self.ctx.llm, open_fields, self.profile, config.read_master_resume(),
                                  self.job, self.description, config.read_optional("story-bank.md"),
                                  config.read_optional("writing-sample.md"),
                                  self.facts() if wanted(field) else None)
            self.answers.append(answer)
        value = answer.get("value")
        action = {"choice": "choose", "check": "check"}.get(field["widget"], "fill" if field["kind"] != "select" else "select")
        self.record(ref, action, key=answer["key"])
        if value in (None, "", []):
            return {"left_blank": field["label"], "why": answer.get("note") or "No supported answer.",
                    "note": "The user will answer it in review; move on."}
        self.apply(ref, field, value)
        return self.observed(f"filled {field['label'][:80]!r} with {str(value)[:80]!r} ({answer['source']})")

    def upload_document(self, ref: str, document: str):
        if document not in ("resume", "cover_letter"):
            raise ValueError("document is resume or cover_letter.")
        element = self.view.element(ref)
        self.account_consent()
        if element.get("type") != "file" and element.get("tag") not in ("button", "a") \
                and element.get("role") != "button":
            raise ValueError("That isn't a file upload. A cover letter text box is a question: use "
                             "fill_field, or leave an optional one alone.")
        path = self.document(document)
        self.view.upload(ref, path)
        self.record(ref, "upload", document=document)
        return self.observed(f"uploaded the {document.replace('_', ' ')}")

    def fill_password(self, ref: str):
        email = self.profile.get("contact", {}).get("email")
        host = urlsplit(self.view.page.url).hostname or ""
        if not email:
            raise ValueError("The profile has no email for an account.")
        self.account_consent()
        password = config.get_password(f"{KEYCHAIN}:{host}", email)
        if password is None:
            password = new_password()
            config.set_password(f"{KEYCHAIN}:{host}", email, password)
            self.ctx.tracker.record_account(host, email)
        self.view.fill(ref, password)
        self.record(ref, "fill", auth=True, secret="password", host=host)
        return self.observed("filled the site password (stored for this site; never shown to you)")

    def enter_code(self, ref: str):
        code = self.ask_user("The site sent a verification code to your email. Enter it here.")["reply"]
        self.view.fill(ref, code.strip())
        self.record(ref, "fill", auth=True, secret="code")
        return self.observed("entered the code")

    def ask_user(self, question: str):
        if len(self.replies) >= MAX_QUESTIONS:
            raise ValueError("You've asked the user enough; leave the rest blank for review.")
        reply = self.ask(question.strip()[:1500])
        self.replies.append(reply)
        return {"reply": reply}

    def final_step(self, ref: str, summary: str):
        element = self.view.element(ref)
        if element.get("tag") not in ("button", "input", "a") and element.get("role") not in ("button", "link"):
            raise ValueError("final_step takes the submit button.")
        self.record(ref, "submit")
        folder = Path(self.job_folder())
        folder.mkdir(parents=True, exist_ok=True)
        try:
            self.view.page.screenshot(path=str(folder / "review.png"), full_page=True)
        except PlaywrightError:
            pass
        self.final = True
        raise StopRun(summary)

    def give_up(self, reason: str):
        raise StopRun(f"Gave up: {reason}")

    # ----- plumbing ---------------------------------------------------------

    def job_folder(self) -> str:
        from . import packets

        return str(packets.directory(self.job))

    def field_for(self, ref: str, element: dict) -> dict:
        tag, kind_attr = element.get("tag"), element.get("type", "")
        if kind_attr == "radio":
            options = [e["name"] for e in self.view.elements.values()
                       if e.get("field") == element.get("field") and e.get("type") == "radio"]
            label, kind, widget = element.get("group") or element.get("field") or "Choice", "select", "choice"
        elif kind_attr == "checkbox":
            options, kind, widget = ["Yes", "No"], "select", "check"
            label = element.get("name") or element.get("group") or "Checkbox"
        elif tag == "select":
            options, kind, widget = [o for o in element.get("options", []) if o and not o.endswith("…")
                                     and not o.lower().startswith("select")], "select", "native"
            label = element.get("name") or element.get("field") or "Select"
        else:
            options, widget = [], "text"
            kind = "textarea" if tag == "textarea" else "text"
            label = element.get("name") or element.get("field") or "Field"
        label = forms.clean(label)  # "First name *" -> "First name", so profile rules match.
        target = json.dumps([element.get("frame"), element.get("field") or label])
        return {"key": f"web_{len(self.answers) + 1}", "label": label, "kind": kind,
                "required": bool(element.get("required")), "options": options, "section": "",
                "description": "", "widget": widget, "target": target}

    def apply(self, ref: str, field: dict, value) -> None:
        if field["widget"] == "choice":
            match = next((r for r, e in self.view.elements.items() if e.get("type") == "radio"
                          and e.get("field") == self.view.element(ref).get("field")
                          and forms.norm(e["name"]) == forms.norm(str(value))), None)
            if match is None:
                raise ValueError(f"No option {value!r}.")
            self.view.set_checked(match, True)
        elif field["widget"] == "check":
            self.view.set_checked(ref, str(value).lower() in ("yes", "true"))
        elif field["kind"] == "select":
            self.view.select(ref, str(value))
        else:
            self.view.fill(ref, str(value))

    def tools(self) -> list[Tool]:
        ref = {"type": "string", "description": "Element ref from the latest snapshot."}
        return [
            Tool("snapshot", "The page: visible text and numbered interactive elements.", schema(),
                 self.snapshot, ephemeral=True),
            Tool("read_text", "All visible text of the page, 12,000 characters at a time.",
                 schema({"offset": {"type": "integer"}}, []), self.read_text, ephemeral=True),
            Tool("goto", "Open a URL (only to reach the application from the posting).",
                 schema({"url": {"type": "string"}}), self.goto, ephemeral=True),
            Tool("click", "Click a button or link (Apply, Next, Create account, Verify). Not the final "
                 "submit.", schema({"ref": ref}), self.click, ephemeral=True),
            Tool("scroll", "Scroll to reveal more.", schema({"direction": {"enum": ["down", "up"]}}),
                 self.scroll, ephemeral=True),
            Tool("fill_field", "Answer a form question (text, select, radio group, checkbox) from the "
                 "applicant's profile and resume, and fill it in. You never choose the value.",
                 schema({"ref": ref}), self.fill_field, ephemeral=True),
            Tool("upload_document", "Upload the tailored resume or the cover letter to a file field.",
                 schema({"ref": ref, "document": {"enum": ["resume", "cover_letter"]}}),
                 self.upload_document, ephemeral=True),
            Tool("fill_password", "Fill a password field (account creation or sign-in) with the "
                 "site's stored password.", schema({"ref": ref}), self.fill_password, ephemeral=True),
            Tool("enter_code", "Ask the user for an emailed verification code and enter it.",
                 schema({"ref": ref}), self.enter_code, ephemeral=True),
            Tool("ask_user", f"Ask the user something (max {MAX_QUESTIONS}), e.g. to complete a "
                 "human check in the browser.", schema({"question": {"type": "string"}}), self.ask_user),
            Tool("final_step", "Stop at the final submit button: name it and summarize what you "
                 "filled. Code clicks it later, after the user approves.",
                 schema({"ref": ref, "summary": {"type": "string"}}), self.final_step),
            Tool("give_up", "Stop if the application can't be completed (closed, needs a human).",
                 schema({"reason": {"type": "string"}}), self.give_up),
        ]


def new_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(16)) + "!a7Q"


# ----- replay --------------------------------------------------------------

def find(page, target: dict, strict: bool = False):
    """The element a recorded step acted on, by name attribute, then role and label.
    `strict`: only in a frame at the recorded path (account pages: an "Email" box
    elsewhere is a different question)."""
    frames = [f for f in page.frames if urlsplit(f.url).path == target.get("frame")]
    if not frames and not strict:
        frames = page.frames
    for frame in frames:
        candidates = []
        if target.get("field"):
            tag = target.get("tag") or "*"
            candidates.append(frame.locator(f'{tag}[name="{target["field"]}"]'))
        name = target.get("name") or ""
        if name:
            if target.get("tag") in ("button", "a") or target.get("role") in ("button", "link"):
                role = "link" if target.get("tag") == "a" or target.get("role") == "link" else "button"
                candidates.append(frame.get_by_role(role, name=name, exact=True))
            candidates.append(frame.get_by_label(name, exact=True))
        for locator in candidates:
            try:
                if locator.count():
                    return locator
            except PlaywrightError:
                continue
    return None


def walk(page, packet, files, ask, *, email, tracker, before_final, known, log):
    """Replay the approved recording. Same contract as workday.walk."""
    recording = packet["recording"]
    values = {a["key"]: a.get("value") for a in packet["answers"]}
    by_key = {a["key"]: a for a in packet["answers"]}
    context = page.context
    log("  replaying the recorded application")
    page.goto(recording["start"], wait_until="domcontentloaded")
    for number, step in enumerate(recording["steps"], 1):
        settle(page)
        if step["action"] == "goto":
            page.goto(step["url"], wait_until="domcontentloaded")
            continue
        target = step["target"]
        found = find(page, target, strict=step["auth"])
        if found is None:
            if step["auth"]:
                continue  # Already signed in: the account steps aren't shown.
            return {"status": "needs_you",
                    "detail": f"The form changed: step {number} (“{target.get('name') or target.get('field')}”) "
                              "isn't there any more. Prepare it again."}
        if step["action"] == "submit":
            before_final()
            found.first.click()
            return forms.wait_for_result(page, ask)
        pages = len(context.pages)
        replay_step(page, found, step, values, by_key, files, ask)
        if step["action"] == "click":  # It may open the application in a new tab.
            page = follow_new_tab(context, page, pages)
    return {"status": "needs_you", "detail": "The recording ended before the final step."}


def replay_step(page, found, step, values, by_key, files, ask) -> None:
    action, target = step["action"], step["target"]
    if step.get("secret") == "password":
        email = config.load_profile().get("contact", {}).get("email")
        found.first.fill(config.get_password(f"{KEYCHAIN}:{step['host']}", email) or "")
    elif step.get("secret") == "code":
        found.first.fill(ask("The site sent a verification code to your email. Enter it here.").strip())
    elif action == "click":
        found.first.click()
    elif action == "upload":
        found.first.set_input_files(str(files[step["document"]]))
    else:
        value = values.get(step["key"])
        if value in (None, "", []):
            if by_key[step["key"]].get("required"):
                raise forms.FillError(step["key"], by_key[step["key"]]["label"], "has no approved answer")
            return  # Optional and left blank.
        if action == "choose":
            options = found  # Radios sharing the name attribute: pick by label.
            for index in range(options.count()):
                option = options.nth(index)
                label = option.evaluate("el => ((el.labels && el.labels[0]) || el.closest('label'))?.innerText || ''")
                if forms.norm(label) == forms.norm(str(value)):
                    try:
                        option.check(timeout=5000)
                    except PlaywrightError:
                        option.evaluate("el => ((el.labels && el.labels[0]) || el.closest('label')).click()")
                    return
            raise forms.FillError(step["key"], target.get("group", ""), f"no option {value!r}")
        if action == "check":
            want = str(value).lower() in ("yes", "true")
            try:
                found.first.set_checked(want, timeout=5000)
            except PlaywrightError:
                found.first.evaluate("el => ((el.labels && el.labels[0]) || el.closest('label')).click()")
        elif action == "select":
            found.first.select_option(label=str(value))
        else:
            found.first.fill(str(value))


def settle(page) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=10000)
        page.wait_for_load_state("networkidle", timeout=3000)
    except PlaywrightError:
        pass


def summary(recording: dict) -> list[str]:
    """Plain steps for the review page."""
    lines = []
    for step in recording.get("steps", []):
        target = step.get("target", {})
        what = target.get("group") or target.get("name") or target.get("field") or step.get("url", "")
        lines.append(f"{step['action']}{' (account)' if step['auth'] else ''}: {what}")
    return lines
