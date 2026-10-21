"""Greenhouse: questions from the public API and the form page, and a coded filler.

The API lists custom, EEO and demographic questions with their options. The
page adds fields the API leaves out (phone country, city, education), so
prepare reads both. Submit is code: it types the approved values into fields
found by their DOM id and checks each one took. No model is involved.

Every Greenhouse form field is either a plain input/textarea whose id is the
field name, a file input, or a react-select combobox whose options live in
`#react-select-{id}-listbox`.
"""

import re
import time
from urllib.parse import parse_qs, urlsplit

from .forms import (Closed, FillError, fill_all, same_text, text_value, answered, clean, norm, pick_option,  # noqa: F401
                    unexpected, wait_for_result)

API = "https://boards-api.greenhouse.io/v1/boards"
BOARDS = "https://job-boards.greenhouse.io"
# Comboboxes with more options than this (countries, schools) are typed into.
MAX_LISTED_OPTIONS = 60
# ----- reading the job ------------------------------------------------------

def board_for(client, job_id: str, board: str | None, hints: list[str] = ()) -> str | None:
    """The board token. Job IDs from company sites (?gh_jid=) don't name it; the
    embed URL usually redirects to one that does, and otherwise likely names
    (from the company and its website) are checked against the API."""
    if board:
        return board
    response = client.get(f"{BOARDS}/embed/job_app", params={"token": job_id})
    found = parse_qs(urlsplit(str(response.url)).query).get("for", [None])[0]
    if found:
        return found
    for guess in dict.fromkeys(hints):
        if guess and client.get(f"{API}/{guess}/jobs/{job_id}").status_code == 200:
            return guess
    return None


def board_guesses(job: dict) -> list[str]:
    """"Pinterest" + pinterestcareers.com → pinterest, pinterestcareers, ..."""
    company = re.sub(r"[^a-z0-9]+", " ", (job.get("company") or "").lower()).split()
    host = urlsplit(job.get("url") or "").netloc.lower().removeprefix("www.").removeprefix("careers.")
    site = host.split(".")[0] if host else ""
    guesses = ["".join(company), "-".join(company), company[0] if company else "", site,
               re.sub(r"(careers|jobs)$", "", site)]
    return [g for g in guesses if g]


def fetch_job(client, board: str, job_id: str) -> dict:
    response = client.get(f"{API}/{board}/jobs/{job_id}", params={"questions": "true"})
    if response.status_code == 404:
        raise Closed(f"Greenhouse job {board}/{job_id} not found.")
    response.raise_for_status()
    return response.json()


def apply_url(board: str, job_id: str) -> str:
    return f"{BOARDS}/embed/job_app?for={board}&token={job_id}"


def api_fields(data: dict) -> list[dict]:
    """Questions the API reports, in form order, keyed by the field's DOM id."""
    fields = []

    def add(key, label, kind, required, options, section, description=""):
        fields.append({"key": key, "label": clean(label), "kind": kind, "required": bool(required),
                       "options": options, "section": section,
                       "description": clean_html(description or "")[:600]})

    for question in data.get("questions", []) + data.get("location_questions", []):
        inputs = [f for f in question["fields"] if f["type"] != "input_hidden"]
        if not inputs:
            continue
        # Resume and cover letter offer "attach" or "enter manually"; we attach.
        field = next((f for f in inputs if f["type"] == "input_file"), inputs[0])
        key = field["name"].removesuffix("[]")
        options = [v["label"] for v in field.get("values", [])]
        kind = {"input_file": "file", "textarea": "textarea",
                "multi_value_single_select": "select",
                "multi_value_multi_select": "multiselect"}.get(field["type"], "text")
        if key == "location":
            key, kind = "candidate-location", "search"
        section = "location" if question in data.get("location_questions", []) else "main"
        add(key, question["label"], kind, question.get("required"), options, section,
            question.get("description"))
    for block in data.get("compliance") or []:
        for question in block.get("questions", []):
            field = question["fields"][0]
            add(field["name"], question["label"], "select", question.get("required"),
                [v["label"] for v in field.get("values", [])], "eeo", block.get("description"))
    demographic = data.get("demographic_questions") or {}
    for question in demographic.get("questions", []):
        kind = "multiselect" if question["type"] == "multi_value_multi_select" else "select"
        add(str(question["id"]), question["label"], kind, question.get("required"),
            [o["label"] for o in question.get("answer_options", [])], "demographic")
    return fields


def clean_html(text: str) -> str:
    from .sources import html_to_text

    return html_to_text(text)


# ----- reading the form page -----------------------------------------------

PAGE_FIELDS_JS = r"""
() => {
  const form = document.querySelector('#application-form') || document.querySelector('form');
  if (!form) return [];
  const out = [];
  const tidy = (t) => (t || '').replace(/\s+/g, ' ').trim().replace(/\*$/, '').trim();
  const seen = (el) => el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true }) ||
    [...(el.labels || [])].some((l) => l.checkVisibility());
  // Checkbox groups (fieldset of name="question_X[]") become one multiselect field.
  const groups = new Map();
  for (const el of form.querySelectorAll('input[type=checkbox][name]')) {
    if (!seen(el)) continue;
    const key = el.name.replace(/\[\]$/, '');
    const fieldset = el.closest('fieldset');
    if (!groups.has(key)) {
      const legend = fieldset && fieldset.querySelector('legend');
      const field = { key, kind: 'multiselect', widget: 'checkbox', options: [],
        label: tidy(legend ? legend.innerText : el.labels[0]?.innerText),
        required: (fieldset && fieldset.getAttribute('aria-required') === 'true') || el.required };
      groups.set(key, field);
      out.push(field);
    }
    groups.get(key).options.push(tidy(el.labels[0]?.innerText || el.value));
  }
  for (const el of form.querySelectorAll('input[id], textarea[id]')) {
    if (el.id.startsWith('iti-') || el.type === 'hidden' || el.type === 'checkbox') continue;
    const isFile = el.type === 'file';
    const group = isFile ? el.closest('.file-upload') : null;
    const target = group || el;
    if (!target.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) continue;
    const r = target.getBoundingClientRect();
    if (r.width < 2 || r.height < 2 || r.right + scrollX < 0 || r.bottom + scrollY < 0) continue;
    const labelEl = isFile
      ? document.getElementById('upload-label-' + el.id)
      : document.getElementById(el.id + '-label') || (el.labels && el.labels[0]);
    const rawLabel = labelEl ? labelEl.innerText : (el.getAttribute('aria-label') || '');
    const combobox = el.getAttribute('role') === 'combobox';
    out.push({
      key: el.id,
      label: rawLabel.replace(/\s+/g, ' ').trim().replace(/\*$/, '').trim(),
      kind: isFile ? 'file' : combobox ? 'select' : el.tagName === 'TEXTAREA' ? 'textarea' : 'text',
      required: (group ? group.getAttribute('aria-required') === 'true' : false) ||
        el.getAttribute('aria-required') === 'true' || el.required || /\*\s*$/.test(rawLabel),
    });
  }
  return out;
}
"""


def education_rows(page) -> None:
    """One education row per degree in your profile: "Add another" until there are enough."""
    from . import config

    wanted = len(config.load_profile().get("education") or [])
    button = page.locator(".education--container button.add-another-button").first
    for _ in range(4):
        rows = page.locator('[id^="school--"][role="combobox"]').count()
        if not rows or rows >= wanted or not button.count():
            return
        button.click()
        page.locator(f'[id="school--{rows}"]').wait_for(timeout=5000)


def page_fields(page) -> list[dict]:
    """Visible fields on the open form, with combobox options read by opening each."""
    education_rows(page)
    fields = page.evaluate(PAGE_FIELDS_JS)
    for field in fields:
        if field["kind"] == "select":
            options, multi = read_options(page, field["key"])
            field["kind"] = "multiselect" if multi else "select"
            if not options or len(options) > MAX_LISTED_OPTIONS:
                field["kind"], options = "search", []
            field["options"] = options
        else:
            field.setdefault("options", [])
    return fields


def read_options(page, key: str) -> tuple[list[str], bool]:
    box = page.locator(f'[id="{key}"]')
    try:
        box.click(timeout=5000)
        listbox = page.locator(f'[id="react-select-{key}-listbox"]')
        listbox.wait_for(timeout=2500)
        options = listbox.locator('[role="option"]').all_inner_texts()
        multi = listbox.get_attribute("aria-multiselectable") == "true"
    except Exception:  # noqa: BLE001 - no menu without typing (search fields).
        options, multi = [], False
    page.keyboard.press("Escape")
    return [clean(o) for o in options if clean(o) and clean(o) != "No options"], multi


def merge(api: list[dict], page: list[dict]) -> tuple[list[dict], list[str]]:
    """API fields (authoritative labels and options) plus page-only fields.

    Returns the fields and notes about API questions the page doesn't show.
    """
    on_page = {f["key"]: f for f in page}
    merged, notes = [], []
    for field in api:
        seen = on_page.pop(field["key"], None)
        if seen is None:
            notes.append(f"Not shown on the form: {field['label']}")
            continue
        field["required"] = field["required"] or seen["required"]
        if seen.get("widget"):
            field["widget"] = seen["widget"]
        merged.append(field)
    for field in on_page.values():
        merged.append({**field, "section": "page", "description": ""})
    return merged, notes


def read_form(page, board: str, job_id: str, data: dict) -> tuple[list[dict], list[str]]:
    page.goto(apply_url(board, job_id), wait_until="domcontentloaded")
    page.locator("#application-form, form").first.wait_for(timeout=20000)
    page.wait_for_timeout(800)
    return merge(api_fields(data), page_fields(page))


# ----- filling --------------------------------------------------------------

def choose(page, field: dict, value: str) -> str:
    """Pick the option matching `value`; returns the option's full text."""
    key = field["key"]
    box = page.locator(f'[id="{key}"]')
    box.scroll_into_view_if_needed(timeout=5000)
    box.click()
    loose = field["kind"] == "search"
    box.press_sequentially(value[:40], delay=25)
    listbox = page.locator(f'[id="react-select-{key}-listbox"]')
    deadline = time.time() + (6 if loose else 3)
    index = None
    while time.time() < deadline:
        try:
            options = listbox.locator('[role="option"]').all_inner_texts()
        except Exception:  # noqa: BLE001 - menu re-rendering.
            options = []
        index = pick_option([clean(o) for o in options], value, loose)
        if index is not None:
            break
        time.sleep(0.4)
    if index is None:
        page.keyboard.press("Escape")
        raise FillError(key, field["label"], f"no option matching {value!r}")
    option = listbox.locator('[role="option"]').nth(index)
    text = clean(option.inner_text())
    option.click()
    page.wait_for_timeout(250)
    return text


def selected(page, key: str) -> list[str]:
    return page.locator(f'[id="{key}"]').evaluate(
        "i => { const c = i.closest('.select__control'); if (!c) return [];"
        " const multi = [...c.querySelectorAll('.select__multi-value__label')].map(x => x.innerText);"
        " if (multi.length) return multi;"
        " const one = c.querySelector('.select__single-value'); return one ? [one.innerText] : []; }")


def fill_field(page, field: dict, value, files: dict) -> None:
    key, kind = field["key"], field["kind"]
    if kind == "file":
        path = files.get(value)
        if not path:
            raise FillError(key, field["label"], f"no {value} file in the packet")
        page.locator(f'input[type="file"][id="{key}"]').set_input_files(str(path))
        group = page.locator(f'.file-upload[aria-labelledby="upload-label-{key}"]')
        group.filter(has_text=path.name).wait_for(timeout=15000)
        return
    if field.get("widget") == "checkbox":
        tick(page, field, value if isinstance(value, list) else [value])
        return
    if kind in ("select", "search", "multiselect"):
        values = value if isinstance(value, list) else [value]
        picked = [norm(choose(page, field, str(item))) for item in values]
        got = [norm(v) for v in selected(page, key)]
        # Some widgets show a shortened value ("+1" for "United States +1").
        for item in picked:
            if not any(g == item or (g and g in item) for g in got):
                raise FillError(key, field["label"], f"shows {got}, expected {item!r}")
        return
    box = page.locator(f'[id="{key}"]')
    box.scroll_into_view_if_needed(timeout=5000)
    box.fill(text_value(value))
    box.evaluate("el => el.dispatchEvent(new Event('blur', {bubbles: true}))")
    if not same_text(box.input_value(), value):
        raise FillError(key, field["label"], "value didn't stick")


def tick(page, field: dict, values: list[str]) -> None:
    """Check exactly the boxes whose labels match `values`."""
    key = field["key"]
    boxes = page.locator(f'input[type="checkbox"][name="{key}[]"], input[type="checkbox"][name="{key}"]')
    labels = [clean(boxes.nth(i).evaluate("el => el.labels[0]?.innerText || el.value"))
              for i in range(boxes.count())]
    wanted = set()
    for value in values:
        index = pick_option(labels, str(value), loose=False)
        if index is None:
            raise FillError(key, field["label"], f"no checkbox labeled {value!r}")
        wanted.add(index)
    for index in range(len(labels)):
        box = boxes.nth(index)
        if box.is_checked() != (index in wanted):
            box.set_checked(index in wanted, force=True)
    got = {i for i in range(len(labels)) if boxes.nth(i).is_checked()}
    if got != wanted:
        raise FillError(key, field["label"], "checkboxes didn't stick")


def fill_form(page, packet: dict, files: dict) -> None:
    fill_all(page, packet, files, fill_field)


# ----- submitting -----------------------------------------------------------

def submit(page, ask) -> dict:
    """Click submit and wait for the confirmation (or a security code to ask you for)."""
    button = page.locator('button[type="submit"]').filter(has_text=re.compile("submit", re.I)).first
    button.click()
    return wait_for_result(
        page, ask, done_url=("confirmation",),
        errors=".helper-text--error, [role='alert'] .error, .error-message",
        resubmit=lambda: page.locator('button[type="submit"]').first.click())


# ----- the common interface (see forms.py) ------------------------------------

def resolve(client, job: dict) -> tuple[str | None, str]:
    return board_for(client, job["ats_job_id"], job["ats_board"], board_guesses(job)), job["ats_job_id"]


def fetch(client, board: str, job_id: str) -> dict:
    data = fetch_job(client, board, job_id)
    return {"title": data.get("title"), "html": data.get("content", ""), "data": data}
