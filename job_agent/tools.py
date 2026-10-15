"""The tools each agent can call. The model sees declarations; Python runs them."""

import re
import secrets
import string
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from . import config, documents
from .browser import FINAL_SUBMIT, Browser
from .harness import StopRun, Tool, schema
from .tracker import Tracker

KEYCHAIN_SERVICE = "job-agent"


def ask_human(question: str) -> str:
    print("\a")  # Terminal bell: the agent is waiting on you.
    print("=" * 72)
    print(f"AGENT NEEDS YOU: {question}")
    print("=" * 72)
    return input("your answer > ").strip()


class BrowserTools:
    """Shared by both agents. Every action returns the page as it now stands."""

    def __init__(self, browser: Browser, allow_final_submit_click=False):
        self.browser = browser
        self.allow_final_submit_click = allow_final_submit_click

    def page(self, done: str) -> dict:
        return {"done": done, "page": self.browser.snapshot()}

    def snapshot(self):
        return self.browser.snapshot()

    def goto(self, url: str):
        self.browser.goto(url)
        return self.page(f"opened {url}")

    def click(self, ref: str):
        label = self.browser.describe(ref)
        if not self.allow_final_submit_click and FINAL_SUBMIT.match(label or ""):
            raise PermissionError(
                f"Element {ref} ({label!r}) looks like a final submit. "
                "Use submit_application for that step."
            )
        self.browser.click(ref)
        return self.page(f"clicked {ref} ({label[:60]!r})")

    def fill(self, ref: str, text: str):
        self.browser.fill(ref, text)
        return self.page(f"filled {ref}")

    def type_text(self, ref: str, text: str):
        self.browser.type_text(ref, text)
        return self.page(f"typed into {ref}")

    def select(self, ref: str, option: str):
        chosen = self.browser.select(ref, option)
        return self.page(f"selected {chosen!r} in {ref}")

    def set_checked(self, ref: str, checked: bool):
        self.browser.set_checked(ref, checked)
        return self.page(f"set {ref} checked={checked}")

    def press(self, key: str):
        self.browser.press(key)
        return self.page(f"pressed {key}")

    def scroll(self, direction: str):
        self.browser.scroll(direction)
        return self.page(f"scrolled {direction}")

    def back(self):
        self.browser.back()
        return self.page("went back")

    def tabs(self, switch_to: int | None = None):
        if switch_to is not None:
            self.browser.switch_tab(switch_to)
            return self.page(f"switched to tab {switch_to}")
        return self.browser.tabs()

    def read_text(self, offset: int = 0):
        return self.browser.read_text(offset)

    def declarations(self) -> list[Tool]:
        ref = {"type": "string", "description": "Element ref from the latest snapshot."}
        return [
            Tool("browser_snapshot", "Observe the current page: visible text and numbered interactive elements.",
                 schema(), self.snapshot, ephemeral=True),
            Tool("browser_goto", "Open an absolute URL in the current tab.",
                 schema({"url": {"type": "string"}}), self.goto, ephemeral=True),
            Tool("browser_click", "Click an element (button, link, option, checkbox label).",
                 schema({"ref": ref}), self.click, ephemeral=True),
            Tool("browser_fill", "Replace the contents of a text field, textarea or editable area.",
                 schema({"ref": ref, "text": {"type": "string"}}), self.fill, ephemeral=True),
            Tool("browser_type", "Type key by key, for autocomplete fields that ignore browser_fill. "
                 "Usually followed by choosing a suggestion or pressing Enter.",
                 schema({"ref": ref, "text": {"type": "string"}}), self.type_text, ephemeral=True),
            Tool("browser_select", "Choose an option in a native <select> by its visible text.",
                 schema({"ref": ref, "option": {"type": "string"}}), self.select, ephemeral=True),
            Tool("browser_set_checked", "Check or uncheck a checkbox, radio or switch.",
                 schema({"ref": ref, "checked": {"type": "boolean"}}), self.set_checked, ephemeral=True),
            Tool("browser_press", "Press a key in the focused element, such as Enter, Tab, ArrowDown or Escape.",
                 schema({"key": {"type": "string"}}), self.press, ephemeral=True),
            Tool("browser_scroll", "Scroll the page to reveal more content or lazy-loaded results.",
                 schema({"direction": {"enum": ["down", "up"]}}), self.scroll, ephemeral=True),
            Tool("browser_back", "Go back in the current tab's history.", schema(), self.back, ephemeral=True),
            Tool("browser_tabs", "List open tabs, or switch to one by index. New tabs become active automatically.",
                 schema({"switch_to": {"type": "integer"}}, required=[]), self.tabs, ephemeral=True),
            Tool("browser_read_text", "Read the full visible text of the page in 12,000-character chunks.",
                 schema({"offset": {"type": "integer", "minimum": 0}}, required=[]),
                 self.read_text, ephemeral=True),
        ]


def read_skill_tool() -> Tool:
    names = sorted(p.stem for p in config.SKILLS_DIR.glob("*.md"))

    def read_skill(name: str):
        return (config.SKILLS_DIR / f"{name}.md").read_text(encoding="utf-8")

    return Tool("read_skill", "Load a detailed playbook when it applies.",
                schema({"name": {"enum": names}}), read_skill)


def search_tools(browser: Browser, tracker: Tracker, target: int) -> list[Tool]:
    added: list[str] = []

    def add_job_lead(url: str, site: str, company: str, title: str, location: str, why: str):
        is_new, duplicate = tracker.add_lead(url, site, company, title, location)
        if not is_new:
            return {"added": False, "duplicate_of": {k: duplicate[k] for k in
                    ("company", "title", "status", "url")}}
        added.append(f"{title} at {company}")
        print(f"  + lead: {title} at {company} ({location}) — {why[:100]}")
        left = target - len(added)
        return {"added": True, "leads_this_run": len(added),
                "remaining": max(left, 0),
                **({"note": "Target reached. Call finish_search."} if left <= 0 else {})}

    def check_job(url: str, company: str = "", title: str = ""):
        duplicate = tracker.find_duplicate(url, company, title)
        return {"seen": bool(duplicate), **({"status": duplicate["status"]} if duplicate else {})}

    def finish_search(summary: str):
        raise StopRun(summary)

    def ask_user(question: str):
        return ask_human(question)

    return BrowserTools(browser).declarations() + [
        Tool("add_job_lead", "Record a matching posting to apply to later. Reports duplicates.",
             schema({"url": {"type": "string"}, "site": {"type": "string"},
                     "company": {"type": "string"}, "title": {"type": "string"},
                     "location": {"type": "string"},
                     "why": {"type": "string", "description": "One line on why it fits."}}),
             add_job_lead),
        Tool("check_job", "Check whether a posting was already seen or applied to.",
             schema({"url": {"type": "string"}, "company": {"type": "string"},
                     "title": {"type": "string"}}, required=["url"]), check_job),
        Tool("ask_user", "Ask the user to log in, solve a verification check, or clarify the search.",
             schema({"question": {"type": "string"}}), ask_user),
        Tool("finish_search", "End the search phase with a short summary.",
             schema({"summary": {"type": "string"}}), finish_search),
    ]


def apply_tools(browser: Browser, tracker: Tracker, settings: config.Settings,
                profile: dict, job: dict, job_dir: Path) -> list[Tool]:
    state: dict = {"resume": None, "cover_letter": None, "submit_clicked": False}
    master = config.read_master_resume()
    full_name = profile.get("contact", {}).get("name", "Applicant")
    file_stem = re.sub(r"[^A-Za-z0-9]+", "_", full_name).strip("_")
    answers_path = config.PROFILE_DIR / "answers.yaml"

    def load_answers() -> dict:
        if not answers_path.exists():
            return {}
        return yaml.safe_load(answers_path.read_text(encoding="utf-8")) or {}

    def read_profile():
        return {
            "profile": profile,
            "remembered_answers": load_answers(),
            "writing_sample": config.read_optional("writing-sample.md"),
        }

    def read_master_resume():
        return master

    def save_tailored_resume(markdown: str):
        invented = documents.unsupported_numbers(markdown, master)
        if invented:
            raise ValueError(
                "These numbers do not appear in the master resume: "
                f"{invented}. Use only the master resume's facts and figures."
            )
        state["resume"] = documents.save(
            browser, job_dir, f"{file_stem}_Resume", markdown, f"{full_name} Resume")
        tracker.update(job["id"], resume_path=state["resume"]["pdf"])
        return {"saved": state["resume"]}

    def save_cover_letter(text: str):
        state["cover_letter"] = documents.save(
            browser, job_dir, f"{file_stem}_Cover_Letter", text, f"{full_name} Cover Letter")
        tracker.update(job["id"], cover_letter_path=state["cover_letter"]["pdf"])
        return {"saved": state["cover_letter"], "text_for_pasting": text}

    def upload_document(ref: str, document: str):
        saved = state.get(document)
        if not saved:
            raise ValueError(f"Save the {document} first.")
        browser.upload(ref, Path(saved["pdf"]))
        return {"done": f"uploaded {Path(saved['pdf']).name}", "page": browser.snapshot()}

    def ask_user(question: str):
        return ask_human(question)

    def remember_answer(question: str, answer: str):
        answers = load_answers()
        answers[question.strip()] = answer.strip()
        answers_path.write_text(yaml.safe_dump(answers, sort_keys=True, allow_unicode=True),
                                encoding="utf-8")
        return {"remembered": True}

    def browser_fill_password(ref: str):
        host = urlsplit(browser.page.url).hostname or ""
        email = profile.get("contact", {}).get("email")
        if not host or not email:
            raise ValueError("Need a page host and contact.email in the profile.")
        service = f"{KEYCHAIN_SERVICE}:{host}"
        password = config.get_password(service, email)
        created = password is None
        if created:
            alphabet = string.ascii_letters + string.digits
            password = "".join(secrets.choice(alphabet) for _ in range(16)) + "!a7Q"
            config.set_password(service, email, password)
            tracker.record_account(host, email)
        browser.fill(ref, password)
        return {"done": f"filled password for {host}",
                "stored": "the session files (hosted)" if config.HOSTED else f"macOS Keychain, service {service!r}",
                "new_password": created, "page": browser.snapshot()}

    def submit_application(ref: str, summary: str):
        if tracker.submitted_today() >= settings.daily_submit_limit:
            raise PermissionError(
                f"Daily limit of {settings.daily_submit_limit} submissions reached. "
                "Call finish_job with status needs_you.")
        label = browser.describe(ref)
        if settings.submit_mode == "review":
            print("\n" + "-" * 72)
            print(f"READY TO SUBMIT: {job['title']} at {job['company']}")
            print(f"  {browser.page.url}")
            print(f"  button: {label!r}")
            for kind in ("resume", "cover_letter"):
                if state[kind]:
                    print(f"  {kind}: {state[kind]['pdf']}")
            print(f"\n{summary}\n" + "-" * 72)
            print("Check the browser window. Type y to submit, n to skip this job,")
            print("or type a correction for the agent to make first.")
            decision = input("submit? > ").strip()
            if decision.lower() not in ("y", "yes"):
                if decision.lower() in ("n", "no", ""):
                    return {"submitted": False, "user_decision": "declined",
                            "instruction": "Call finish_job with status skipped."}
                return {"submitted": False, "user_correction": decision,
                        "instruction": "Make this correction, then call submit_application again."}
        browser.click(ref)
        state["submit_clicked"] = True
        tracker.update(job["id"], notes="final submit clicked")
        return {"clicked": label, "page": browser.snapshot(),
                "instruction": "Confirm the site acknowledged the application, then call finish_job."}

    def finish_job(status: str, report: str):
        if status == "submitted" and not state["submit_clicked"]:
            raise ValueError("submit_application was never used, so this was not submitted.")
        tracker.update(job["id"], status=status, reason=report[:2000])
        raise StopRun(f"{status}: {report}")

    return BrowserTools(browser).declarations() + [
        read_skill_tool(),
        Tool("read_profile", "Read the user's profile, remembered answers and writing sample.",
             schema(), read_profile),
        Tool("read_master_resume", "Read the user's complete master resume (Markdown).",
             schema(), read_master_resume),
        Tool("save_tailored_resume", "Save a resume tailored to this job, as Markdown, and render its PDF. "
             "Rejected if it contains numbers absent from the master resume.",
             schema({"markdown": {"type": "string"}}), save_tailored_resume),
        Tool("save_cover_letter", "Save a cover letter for this job, as Markdown, and render its PDF.",
             schema({"text": {"type": "string"}}), save_cover_letter),
        Tool("upload_document", "Upload a saved document through a file input or upload button.",
             schema({"ref": {"type": "string"}, "document": {"enum": ["resume", "cover_letter"]}}),
             upload_document, ephemeral=True),
        Tool("browser_fill_password", "Fill a password field with this site's stored password, "
             "creating and storing a strong one in the Keychain if none exists. Use for both new "
             "accounts and sign-ins.", schema({"ref": {"type": "string"}}),
             browser_fill_password, ephemeral=True),
        Tool("ask_user", "Ask the user for a verification code, a CAPTCHA, a login, or an answer "
             "you cannot find in their profile.", schema({"question": {"type": "string"}}), ask_user),
        Tool("remember_answer", "Save an answer the user gave so future applications reuse it.",
             schema({"question": {"type": "string"}, "answer": {"type": "string"}}), remember_answer),
        Tool("submit_application", "Click the final submit button. The user may review first.",
             schema({"ref": {"type": "string"},
                     "summary": {"type": "string", "description":
                                 "What was entered: key answers, documents, anything uncertain."}}),
             submit_application),
        Tool("finish_job", "End this application and record its outcome.",
             schema({"status": {"enum": ["submitted", "skipped", "failed", "needs_you"]},
                     "report": {"type": "string"}}), finish_job),
    ]
