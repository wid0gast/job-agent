"""Workday: an account per company, then a multi-page wizard, filled page by page.

Workday differs from the single-page systems:
- Each company's site wants its own account (your email, a generated password
  kept in the macOS Keychain per site, usually an emailed verification).
- Questions are spread over pages (My Information, My Experience, Application
  Questions, Voluntary Disclosures, Review); later pages appear only after
  earlier ones are filled.

So prepare drafts what it can see (often the first page, which many sites
show before sign-in) plus the standard Workday fields, and submit *walks*:
it fills each page with approved answers and goes on only while the footer
button says Next. A required question without an approved answer stops the
walk; it is added to the packet and the job comes back to you. The final
click happens only when the footer button says Submit.

Every field sits in a box `[data-automation-id="formField-<name>"]`, which is
the key. Widgets: text, radio sets, single checkboxes, dropdown buttons
(aria-haspopup=listbox), "prompt" search fields, date parts, file upload.
"""

import re
import secrets
import string
import time
from urllib.parse import urlsplit

from . import config

from .forms import (Closed, FillError, fill_all, same_text, text_value, check_labeled, clean, pick_option,  # noqa: F401
                    visible_text, wait_for_result)

KEYCHAIN = "job-agent"
MAX_PAGES = 12
FOOTER = '[data-automation-id="pageFooterNextButton"], [data-automation-id="bottom-navigation-next-button"]'
GATE = ('[data-automation-id="signInContent"], form[data-automation-id="signInFormo"], '
        '[data-automation-id="createAccountSubmitButton"], [data-automation-id="signInSubmitButton"]')
ERRORS = '[data-automation-id="errorMessage"], [data-automation-id="errorBanner"], [data-automation-id="inputAlert"]'
VERIFY = re.compile(r"verify (your )?(email|account)|verification (email|link|code)|check your email", re.I)
EXISTS = re.compile(r"already (exists|in use|registered)|account with this email", re.I)


class Passwords:
    """Site passwords: the macOS Keychain (tests swap in a dict)."""

    def get(self, site: str, email: str) -> str | None:
        return config.get_password(f"{KEYCHAIN}:{site}", email)

    def set(self, site: str, email: str, password: str) -> None:
        config.set_password(f"{KEYCHAIN}:{site}", email, password)


PASSWORDS = Passwords()


def new_password() -> str:
    """Workday wants upper, lower, digit and special characters, 8+ long."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(16)) + "!aZ7"


# ----- the posting ------------------------------------------------------------

def parts(url: str) -> tuple[str, str, str]:
    """(https://host/site, site, job/<location>/<title>_<req>) from a posting URL."""
    split = urlsplit(url)
    path = [p for p in split.path.split("/") if p]
    index = path.index("job")
    site = path[index - 1]
    tail = re.sub(r"/apply(/.*)?$", "", "/".join(path[index:]))
    # Keep any prefix before the site (a locale such as en-US).
    return f"{split.scheme}://{split.netloc}/{'/'.join(path[:index])}", site, tail


def resolve(client, job: dict) -> tuple[str | None, str | None]:
    try:
        board, _, tail = parts(job["url"])
    except ValueError:
        return None, None
    return board, tail


def fetch(client, board: str, job_path: str) -> dict:
    split = urlsplit(board)
    site = split.path.strip("/").split("/")[-1]
    tenant = split.netloc.split(".")[0]
    response = client.get(f"{split.scheme}://{split.netloc}/wday/cxs/{tenant}/{site}/{job_path}",
                          headers={"Accept": "application/json"})
    if response.status_code in (404, 410):
        raise Closed("Workday posting not found.")
    response.raise_for_status()
    info = response.json().get("jobPostingInfo") or {}
    if info.get("canApply") is False:
        raise Closed("This Workday posting no longer accepts applications.")
    return {"title": info.get("title"), "html": info.get("jobDescription", ""), "data": info}


def apply_url(board: str, job_path: str) -> str:
    return f"{board}/{job_path}/apply/applyManually"


def site_of(page_or_url) -> str:
    url = page_or_url if isinstance(page_or_url, str) else page_or_url.url
    return urlsplit(url).netloc


# ----- reading a page -------------------------------------------------------

FIELDS_JS = r"""
() => {
  const tidy = (t) => (t || '').replace(/\s+/g, ' ').trim().replace(/\s*\*$/, '').trim();
  const visible = (el) => el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true });
  const fields = [];
  for (const box of document.querySelectorAll('[data-automation-id^="formField-"]')) {
    if (!visible(box) || box.parentElement.closest('[data-automation-id^="formField-"]')) continue;
    const key = box.dataset.automationId;
    box.setAttribute('data-agent-key', key);
    const title = box.querySelector('legend, label');
    const label = tidy(title ? title.innerText : key.replace('formField-', ''));
    const required = !!box.querySelector('abbr, [aria-required="true"], [required]') ||
      /\*\s*$/.test(title ? title.innerText : '');
    const radios = [...box.querySelectorAll('input[type=radio]')];
    const checks = [...box.querySelectorAll('input[type=checkbox]')];
    const labelOf = (input) => tidy(document.querySelector(`label[for="${CSS.escape(input.id)}"]`)?.innerText ||
                                    input.closest('label')?.innerText || input.value);
    const field = { key, label, required, options: [], kind: 'text' };
    const dates = [...box.querySelectorAll('[data-automation-id^="dateSection"][data-automation-id$="-input"]')];
    if (radios.length) Object.assign(field, { kind: 'select', widget: 'radio', options: radios.map(labelOf) });
    else if (checks.length === 1) Object.assign(field, { kind: 'select', widget: 'toggle', options: ['Yes', 'No'] });
    else if (checks.length > 1) Object.assign(field, { kind: 'multiselect', widget: 'checkbox', options: checks.map(labelOf) });
    else if (box.querySelector('button[aria-haspopup="listbox"]')) Object.assign(field, { kind: 'select', widget: 'dropdown' });
    else if (box.querySelector('[data-automation-id="multiSelectContainer"], [data-automation-id="promptIcon"]'))
      Object.assign(field, { kind: 'search', widget: 'prompt' });
    else if (dates.length) {
      for (const part of dates) {
        const name = part.dataset.automationId.replace('dateSection', '').replace('-input', '').toLowerCase();
        part.setAttribute('data-agent-key', `${key}|${name}`);
        fields.push({ key: `${key}|${name}`, label: `${label} (${name})`, required, options: [], kind: 'text', widget: 'date' });
      }
      continue;
    } else if (box.querySelector('textarea')) field.kind = 'textarea';
    fields.push(field);
  }
  // The upload input itself is visually hidden; its section shows whether it's on this page.
  const upload = [...document.querySelectorAll('input[data-automation-id="file-upload-input-ref"]')]
    .find((input) => visible(input.closest('[data-automation-id*="ttachments"], section, [role=group]') || input.parentElement));
  if (upload) {
    const section = upload.closest('[data-automation-id*="ttachments"], section, [role=group]') || upload.parentElement;
    section.setAttribute('data-agent-key', 'resume-upload');
    fields.push({ key: 'resume-upload', label: 'Resume/CV', kind: 'file', required: true, options: [] });
  }
  return fields;
}
"""


def page_fields(page, read_dropdowns: bool = True) -> list[dict]:
    fields = page.evaluate(FIELDS_JS)
    for field in fields:
        field.setdefault("description", "")
        field["section"] = step(page) or "workday"
        if field.get("widget") == "dropdown" and read_dropdowns:
            field["options"] = dropdown_options(page, field["key"])
        elif field.get("widget") == "prompt" and read_dropdowns and not prefilled_prompt(page, field):
            # The top level of a prompt's choices ("Job Board", "Referral"...).
            field["options"] = prompt_options(page, field["key"])
            if field["options"]:
                field["kind"] = "select"
    return fields


def step(page) -> str:
    active = page.locator('[data-automation-id="progressBarActiveStep"]')
    try:
        return clean(active.first.inner_text(timeout=1000)) if active.count() else ""
    except Exception:  # noqa: BLE001
        return ""


def box(page, key: str):
    found = page.locator(f'[data-agent-key="{key}"]')
    if not found.count():
        page.evaluate(FIELDS_JS)
    return found.first


def open_list(page):
    return page.locator('[role="listbox"]:visible:not([data-automation-id="selectedItemList"]) [role="option"]')


def dropdown_options(page, key: str) -> list[str]:
    button = box(page, key).locator('button[aria-haspopup="listbox"]')
    try:
        button.click(timeout=4000)
        page.wait_for_timeout(500)
        options = [clean(o) for o in open_list(page).all_inner_texts()]
    except Exception:  # noqa: BLE001
        options = []
    close_popups(page)
    return [o for o in options if o and o.lower() != "select one"]


CHOICES = """(wid) => {
  document.querySelectorAll('[data-agent-choice]').forEach((e) => e.removeAttribute('data-agent-choice'));
  const open = [...document.querySelectorAll('[data-automation-id="promptOption"]')]
    .filter((e) => (e.offsetWidth || e.offsetHeight) && !e.closest('[data-automation-id="selectedItemList"]'));
  // Workday draws the open list apart from its field, tied to it by data-associated-widget;
  // other menus' chosen items can show as options too, so keep only this field's list.
  const mine = wid ? open.filter((e) => e.closest(`[data-associated-widget="${wid}"]`)) : [];
  (mine.length ? mine : open.filter((e) => !e.closest('[data-associated-widget]')))
    .forEach((e) => e.setAttribute('data-agent-choice', ''));
}"""


def choices(page, holder):
    """The choices showing in this prompt field's open list (not other menus', not chosen pills)."""
    widget = holder.locator("[data-uxi-multiselect-id]").first
    wid = widget.get_attribute("data-uxi-multiselect-id") if widget.count() else None
    page.evaluate(CHOICES, wid)
    return page.locator("[data-agent-choice]")


def find_choice(page, holder, value: str, wait: float = 5) -> tuple[list[str], int | None]:
    """Read this field's open list and find `value` in it. Long lists only draw the
    choices in view (Salesforce shows 9 of 14), so scroll down until it appears or the list
    ends. Waits for a list that's still loading (a category's choices come from the server)."""
    texts: list[str] = []
    deadline = time.time() + wait
    while not texts and time.time() < deadline:
        texts = [clean(t) for t in choices(page, holder).all_inner_texts()]
        if not texts:
            page.wait_for_timeout(300)
    seen = list(texts)
    for _ in range(15):
        index = match_choice(seen, value) if seen else None
        if index is not None or not seen:
            return seen, index
        choices(page, holder).last.scroll_into_view_if_needed()
        page.mouse.wheel(0, 300)
        page.wait_for_timeout(500)
        more = [t for t in (clean(t) for t in choices(page, holder).all_inner_texts()) if t not in seen]
        if not more:
            break
        seen += more
    return seen, None


def click_choice(page, holder, text: str) -> None:
    """Click the choice with exactly this text in this field's open list."""
    options = choices(page, holder)
    texts = [clean(t) for t in options.all_inner_texts()]
    target = options.nth(texts.index(text))
    target.scroll_into_view_if_needed()
    target.click()


def prefilled_prompt(page, field) -> bool:
    return bool(box(page, field["key"]).locator('[data-automation-id="selectedItem"]').count())


def prompt_options(page, key: str) -> list[str]:
    try:
        holder = box(page, key)
        holder.locator("input").first.click(timeout=4000)
        page.wait_for_timeout(900)
        options = [clean(o) for o in choices(page, holder).all_inner_texts()]
    except Exception:  # noqa: BLE001
        options = []
    close_popups(page)
    return [o for o in options if o]


def close_popups(page) -> None:
    page.keyboard.press("Escape")
    page.wait_for_timeout(250)
    if page.locator('[role="listbox"]:visible:not([data-automation-id="selectedItemList"]), '
                    '[data-automation-id="promptOption"]:visible').count():
        page.mouse.click(5, 5)  # Click on the page margin to close what's still open.
        page.wait_for_timeout(250)


def at_gate(page) -> bool:
    return page.locator(GATE).first.is_visible() if page.locator(GATE).count() else False


def read_form(page, board: str, job_path: str, data: dict) -> tuple[list[dict], list[str]]:
    page.goto(apply_url(board, job_path), wait_until="domcontentloaded")
    settle(page)
    if at_gate(page):
        return [], ["This Workday site asks you to sign in first; its questions are read page by "
                    "page when applying, and new ones come back to you."]
    fields = page_fields(page)
    return fields, ["Workday shows its questions page by page. Questions on later pages are "
                    "read when applying; new ones come back to you before anything is submitted."]


ERROR_PAGE = re.compile(r"something went wrong", re.I)


def recover(page, tries: int = 3) -> None:
    """Workday sometimes shows "Something went wrong. Please refresh the page" (often right
    after signing in); a refresh clears it."""
    for _ in range(tries):
        heading = page.get_by_text(ERROR_PAGE)
        if not heading.count() or page.locator('[data-automation-id^="formField-"]:visible').count():
            return
        page.reload(wait_until="domcontentloaded")
        settle(page)


def settle(page, timeout: float = 20) -> None:
    deadline = time.time() + timeout
    page.wait_for_timeout(1200)
    while time.time() < deadline:
        busy = page.locator('[data-automation-id="loadingSpinner"]:visible, [data-automation-id="resumeParsing"]:visible')
        # The footer appears before the questions load; wait for questions (or the
        # sign-in form, or an upload-only page), then a moment for late fields.
        ready = page.locator(f'[data-automation-id^="formField-"]:visible, {GATE}, '
                             '[data-automation-id="file-upload-input-ref"], '
                             '[data-automation-id="applyFlowReviewPage"]:visible')
        if page.get_by_text(ERROR_PAGE).count():
            return  # recover() refreshes it.
        if not busy.count() and ready.count():
            page.wait_for_timeout(800)
            return
        time.sleep(0.5)


# ----- filling ----------------------------------------------------------------

def fill_field(page, field: dict, value, files: dict) -> None:
    key, kind, widget = field["key"], field["kind"], field.get("widget")
    holder = box(page, key)
    if kind == "file":
        path = files.get(value)
        if not path:
            raise FillError(key, field["label"], f"no {value} file in the packet")
        holder.locator('input[data-automation-id="file-upload-input-ref"]').set_input_files(str(path))
        holder.locator('[data-automation-id="file-upload-successful"], '
                       f'[data-automation-id="file-upload-item"]:has-text("{path.name}")').first.wait_for(timeout=30000)
        settle(page)
        return
    if widget == "radio":
        inputs = holder.locator("input[type=radio]")
        labels = [clean(inputs.nth(i).evaluate(
            "el => document.querySelector(`label[for=\"${CSS.escape(el.id)}\"]`)?.innerText || el.value"))
            for i in range(inputs.count())]
        check_labeled(inputs, labels, [value], field)
        return
    if widget == "checkbox":
        inputs = holder.locator("input[type=checkbox]")
        labels = [clean(inputs.nth(i).evaluate(
            "el => document.querySelector(`label[for=\"${CSS.escape(el.id)}\"]`)?.innerText || el.value"))
            for i in range(inputs.count())]
        check_labeled(inputs, labels, value if isinstance(value, list) else [value], field, only=True)
        return
    if widget == "toggle":
        holder.locator("input[type=checkbox]").set_checked(str(value).lower().startswith("y"), force=True)
        return
    if widget == "dropdown":
        button = holder.locator('button[aria-haspopup="listbox"]')
        if clean(button.inner_text()).lower() == clean(str(value)).lower():
            return
        button.click()
        page.wait_for_timeout(400)
        options = open_list(page)
        texts = [clean(t) for t in options.all_inner_texts()]
        index = pick_option(texts, str(value), loose=True)
        if index is None:
            close_popups(page)
            raise FillError(key, field["label"], f"no option {value!r}", [t for t in texts if t.lower() != "select one"])
        options.nth(index).click()
        page.wait_for_timeout(300)
        if pick_option([clean(button.inner_text())], texts[index], loose=False) is None:
            raise FillError(key, field["label"], "choice didn't stick")
        return
    if widget == "prompt":
        values = value if isinstance(value, list) else [value]
        for item in values:
            choose_prompt(page, holder, str(item), field)
        return
    target = holder if widget == "date" else holder.locator("textarea, input:not([type=hidden])").first
    target.fill(text_value(value))
    target.press("Tab")
    if widget != "date" and not same_text(target.input_value(), value):
        raise FillError(key, field["label"], "value didn't stick")


# "How did you hear" answers that mean the company's own site, as Workday sites word it.
OWN_SITE = re.compile(r"career|company (web)?site|company page|corporate site|website", re.I)


def match_choice(texts: list[str], value: str) -> int | None:
    """The choice that means `value`. "Company careers page" and the like mean the site's own
    careers choice ("Salesforce Careers Website", or the "External Career Site Sources"
    category that holds it), tried before loose word matching, which can land on any choice
    sharing a word."""
    exact = pick_option(texts, value, loose=False)
    if exact is not None:
        return exact
    if OWN_SITE.search(value):
        for pattern in (r"careers? (web ?site|site|page)", r"career", r"(company|corporate) (web ?site|page)|web ?site"):
            index = next((i for i, t in enumerate(texts) if re.search(pattern, t, re.I)), None)
            if index is not None:
                return index
    return pick_option(texts, value, loose=True)


def chosen(holder) -> str:
    items = holder.locator('[data-automation-id="selectedItem"]')
    return " | ".join(clean(t) for t in items.all_inner_texts()) if items.count() else ""


def choose_prompt(page, holder, value: str, field: dict) -> None:
    """Prompt fields: a list that may have two levels ("Job Board" opens "LinkedIn", "Indeed").
    `value` is a final choice, a category, or "Category > Choice". A different choice saved
    from an earlier visit is removed first. Tries the visible list, then the site's search;
    a category's own choices are picked from in turn."""
    parts = [p.strip() for p in value.split(">") if p.strip()] or [value]
    now = chosen(holder)
    if now and (parts[-1].lower() in now.lower() or pick_option([now], parts[-1], loose=False) is not None):
        return
    for _ in range(3):  # A saved draft's choice: remove it (Workday keeps drafts between visits).
        charm = holder.locator('[data-automation-id="selectedItem"] [data-automation-id="DELETE_charm"]')
        if not charm.count():
            break
        charm.first.click(force=True)
        page.wait_for_timeout(500)
    box_input = holder.locator("input").first
    box_input.click()
    page.wait_for_timeout(900)
    texts, index = find_choice(page, holder, parts[0], wait=2)
    if index is None:  # Not in the top level: search the final choices.
        close_popups(page)
        box_input.click()
        box_input.fill(parts[-1])
        box_input.press("Enter")
        page.wait_for_timeout(1200)
        texts, index = find_choice(page, holder, parts[-1], wait=3)
    if index is None:
        close_popups(page)
        raise FillError(field["key"], field["label"], f"no choice matching {value!r}", texts or field.get("options"))
    picked = texts[index]
    click_choice(page, holder, picked)
    page.wait_for_timeout(900)
    if not chosen(holder):
        # A category: its own choices load and show in the same list.
        target = parts[1] if len(parts) > 1 else parts[0]
        deadline = time.time() + 6
        inner: list[str] = []
        while time.time() < deadline:  # Wait for the category's list to replace the top level.
            inner = [clean(t) for t in choices(page, holder).all_inner_texts()]
            if inner and picked not in inner:
                break
            page.wait_for_timeout(300)
        inner, pick = find_choice(page, holder, target)
        if pick is None and len(inner) == 1:
            pick = 0
        if pick is None:
            close_popups(page)
            raise FillError(field["key"], field["label"], f"{picked!r} needs a more specific choice",
                            [f"{picked} > {t}" for t in inner if t])
        click_choice(page, holder, inner[pick])
        page.wait_for_timeout(700)
    close_popups(page)
    if not chosen(holder):
        raise FillError(field["key"], field["label"], f"choosing {picked!r} didn't stick", field.get("options"))


# ----- signing in -------------------------------------------------------------

def sign_in_or_create(page, email: str, ask, tracker, log) -> None:
    """Get past the account wall: sign in with the stored password, or create the account."""
    site = site_of(page)
    password = PASSWORDS.get(site, email)
    if password is None:
        log(f"  creating a Workday account on {site}")
        create_link = page.locator('[data-automation-id="createAccountLink"]')
        if create_link.count() and create_link.first.is_visible():
            create_link.first.click()
            page.wait_for_timeout(800)
        password = new_password()
        PASSWORDS.set(site, email, password)
        fill_auth(page, email, password, confirm=True)
        checkbox = page.locator('[data-automation-id="createAccountCheckbox"]')
        if checkbox.count():
            checkbox.first.set_checked(True, force=True)
        press(page, "createAccountSubmitButton")
        page.wait_for_timeout(2500)
        text = visible_text(page)
        if EXISTS.search(text):
            raise FillError("account", "Workday account",
                            f"an account for {email} already exists on {site}. Sign in there once in the "
                            "Chrome window (use 'Forgot password' if needed), then approve again.")
        tracker.record_account(site, email)
        if VERIFY.search(text):
            ask(f"Workday ({site}) sent a verification email to {email}. Open it and click the "
                "link (or enter the code in the Chrome window), then reply 'done'.")
            page.wait_for_timeout(1000)
    if at_gate(page):
        log(f"  signing in on {site}")
        link = page.locator('[data-automation-id="signInLink"]')
        if link.count() and link.first.is_visible():
            link.first.click()
            page.wait_for_timeout(800)
        fill_auth(page, email, password, confirm=False)
        press(page, "signInSubmitButton")
        settle(page)
    if at_gate(page):
        text = visible_text(page)
        if VERIFY.search(text):
            ask(f"Workday ({site}) wants you to verify {email} before signing in. Click the link in "
                "the email, then reply 'done'.")
            fill_auth(page, email, password, confirm=False)
            press(page, "signInSubmitButton")
            settle(page)
    if at_gate(page):
        raise FillError("account", "Workday sign-in",
                        "couldn't get past the sign-in page. Sign in in the Chrome window, then approve again.")


def fill_auth(page, email: str, password: str, confirm: bool) -> None:
    def field(auto_id):
        return page.locator(f'input[data-automation-id="{auto_id}"]:visible')

    field("email").first.fill(email)
    field("password").first.fill(password)
    if confirm and field("verifyPassword").count():
        field("verifyPassword").first.fill(password)


def press(page, auto_id: str) -> None:
    """Workday covers its auth buttons with a click_filter overlay; click that when present."""
    button = page.locator(f'[data-automation-id="{auto_id}"]')
    overlay = button.locator("xpath=..").locator('[data-automation-id="click_filter"]')
    target = overlay.first if overlay.count() else button.first
    target.click(force=True)


# ----- the walk ---------------------------------------------------------------

# Fields most Workday sites ask, drafted at prepare time even when the pages
# aren't visible yet (not required here: a site may not ask them).
STANDARD = [
    ("formField-legalName--firstName", "First Name", "text"),
    ("formField-legalName--lastName", "Last Name", "text"),
    ("formField-emailAddress", "Email", "text"),
    ("formField-phoneType", "Phone Device Type", "select"),
    ("formField-phoneNumber", "Phone Number", "text"),
    ("formField-country", "Country", "select"),
    ("formField-city", "City", "text"),
    ("formField-source", "How Did You Hear About Us?", "search"),
    ("formField-candidateIsPreviousWorker", "Have you been employed by this company in the past?", "select"),
    ("resume-upload", "Resume/CV", "file"),
    ("formField-gender", "Gender", "select"),
    ("formField-ethnicity", "Race/Ethnicity", "select"),
    ("formField-hispanicOrLatino", "Are you Hispanic or Latino?", "select"),
    ("formField-veteranStatus", "Veteran Status", "select"),
]


def standard_fields(seen: set[str]) -> list[dict]:
    return [{"key": key, "label": label, "kind": kind, "required": False, "options": [],
             "section": "Workday (standard)", "description": ""}
            for key, label, kind in STANDARD if key not in seen]


def walk(page, packet: dict, files: dict, ask, *, email: str, tracker, before_final, known,
         log) -> dict:
    """Fill page after page. Returns one of:
    {"status": "submitted" | "needs_you", "detail"} or
    {"status": "new_questions", "fields": [...], "detail"}.
    `known(field)` gives an answer from your profile, saved or earlier-approved
    answers for a field the packet lacks (recorded in the packet), or None.
    `before_final()` runs right before the Submit click (marks the job submitting).
    """
    page.goto(packet["job"]["apply_url"], wait_until="domcontentloaded")
    settle(page)
    if at_gate(page):
        sign_in_or_create(page, email, ask, tracker, log)
    approved = {a["key"]: a for a in packet["answers"]}
    by_label = {clean(a["label"]).lower(): a for a in packet["answers"]}
    seen_steps = []
    for _ in range(MAX_PAGES):
        settle(page)
        if at_gate(page):
            sign_in_or_create(page, email, ask, tracker, log)
            settle(page)
        recover(page)
        name = step(page) or f"page {len(seen_steps) + 1}"
        seen_steps.append(name)
        fields = page_fields(page)
        new = []
        for field in fields:
            answer = approved.get(field["key"]) or by_label.get(clean(field["label"]).lower())
            value = answer.get("value") if answer else None
            if value in (None, "", []):
                value = known(field)
            if value in (None, "", []):
                if field["required"] and not filled(page, field):
                    new.append({**field, "section": name})
                continue
            try:
                fill_field(page, field, value, files)
            except FillError as error:
                # The approved answer isn't possible on this site (e.g. not an option).
                new.append({**field, "section": name, "note": f"Couldn't use {value!r}: {error.problem}",
                            "rejected": value, **({"options": error.options, "kind": "select"}
                                                  if error.options else {})})
        if new:
            return {"status": "new_questions", "fields": new,
                    "detail": f"{len(new)} new question(s) on “{name}”."}
        footer = page.locator(FOOTER).first
        label = clean(footer.inner_text())
        if re.search(r"submit", label, re.I):
            before_final()
            footer.click()
            return wait_for_result(page, ask, errors=ERRORS)
        footer.click()
        page.wait_for_timeout(1500)
        settle(page)
        problems = [m for m in (clean(t) for t in page.locator(ERRORS).all_inner_texts()) if m]
        if problems and step(page) == name:
            return {"status": "needs_you",
                    "detail": f"Workday didn't accept “{name}”: " + "; ".join(problems[:4])}
    return {"status": "needs_you", "detail": "Too many pages; finish in the Chrome window."}


def filled(page, field: dict) -> bool:
    """Workday pre-fills some fields (email, country); those need no answer."""
    holder = box(page, field["key"])
    try:
        if field.get("widget") == "dropdown":
            text = clean(holder.locator('button[aria-haspopup="listbox"]').inner_text()).lower()
            return bool(text) and text != "select one"
        if field.get("widget") == "prompt":
            return bool(holder.locator('[data-automation-id="selectedItem"]').count())
        if field.get("widget") in ("radio", "checkbox", "toggle"):
            return bool(holder.locator("input:checked").count())
        if field["kind"] == "file":
            return bool(page.locator('[data-automation-id="file-upload-successful"]').count())
        target = holder if field.get("widget") == "date" else holder.locator("textarea, input:not([type=hidden])").first
        return bool(target.input_value().strip())
    except Exception:  # noqa: BLE001
        return False
