"""Lever: posting text from the public API, questions read from the form page.

Lever's form is server-rendered HTML. Every question is an
`li.application-question` and every input has a stable `name` (`name`,
`email`, `urls[LinkedIn]`, `cards[<id>][field0]`, `eeo[gender]`), which is
the field key. Uploading a resume makes Lever pre-fill fields from it, so the
resume goes first and everything else is typed over it. Submit runs an
invisible hCaptcha; a visible challenge is handed to you.
"""

from urllib.parse import quote

from .forms import (Closed, FillError, fill_all, same_text, text_value, answered, check_labeled, clean, combobox,  # noqa: F401
                    pick_option, select_native, type_into, visible_text, wait_for_result)

API = "https://api.lever.co/v0/postings"
API_EU = "https://api.eu.lever.co/v0/postings"
JOBS = "https://jobs.lever.co"

PAGE_FIELDS_JS = r"""
() => {
  const form = document.querySelector('#application-form, form[action*="/apply"]');
  if (!form) return null;
  const tidy = (t) => (t || '').replace(/\s+/g, ' ').trim().replace(/\s*[*✱]$/, '').trim();
  const shown = (el) => el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true }) ||
    [...(el.labels || [])].some((l) => l.checkVisibility()) ||
    (el.closest('label') && el.closest('label').checkVisibility());
  const fields = new Map();
  for (const el of form.querySelectorAll('input[name], select[name], textarea[name]')) {
    if (el.type === 'hidden' || el.type === 'submit' || !shown(el)) continue;
    const question = el.closest('.application-question');
    const labelEl = question && (question.querySelector('.application-label .text') ||
                                 question.querySelector('.application-label'));
    const rawLabel = labelEl ? labelEl.innerText : (el.placeholder || el.name);
    const key = el.name;
    if (!fields.has(key)) {
      let kind = 'text', widget;
      if (el.type === 'file') kind = 'file';
      else if (el.tagName === 'TEXTAREA') kind = 'textarea';
      else if (el.tagName === 'SELECT') { kind = 'select'; widget = 'native'; }
      else if (el.type === 'radio') { kind = 'select'; widget = 'radio'; }
      else if (el.type === 'checkbox') { kind = 'multiselect'; widget = 'checkbox'; }
      else if (el.id === 'location-input') { kind = 'search'; widget = 'lever-location'; }
      const options = el.tagName === 'SELECT'
        ? [...el.options].filter((o) => o.value).map((o) => tidy(o.text)) : [];
      fields.set(key, { key, kind, widget, options, label: tidy(rawLabel),
        required: el.required || /[*✱]\s*$/.test(rawLabel || '') ||
          !!(question && question.querySelector('.required, [class*=required]')) });
    }
    if (el.type === 'radio' || el.type === 'checkbox') {
      const text = el.closest('label') ? el.closest('label').innerText : el.value;
      fields.get(key).options.push(tidy(text) || el.value);
      fields.get(key).required = fields.get(key).required || el.required;
    }
  }
  return [...fields.values()];
}
"""


def resolve(client, job: dict) -> tuple[str | None, str]:
    return job["ats_board"], job["ats_job_id"]


def fetch(client, board: str, job_id: str) -> dict:
    for api in (API, API_EU):
        response = client.get(f"{api}/{board}/{job_id}", params={"mode": "json"})
        if response.status_code == 200:
            data = response.json()
            parts = [data.get("description", "")]
            parts += [f"<h3>{item['text']}</h3>{item['content']}" for item in data.get("lists", [])]
            parts.append(data.get("additional", ""))
            return {"title": data.get("text"), "html": "".join(parts), "data": data}
    raise Closed(f"Lever posting {board}/{job_id} not found.")


def apply_url(board: str, job_id: str) -> str:
    return f"{JOBS}/{quote(board)}/{job_id}/apply"


def page_fields(page) -> list[dict]:
    fields = page.evaluate(PAGE_FIELDS_JS)
    if fields is None:
        raise Closed("This Lever posting shows no application form.")
    for field in fields:
        field["section"] = "eeo" if field["key"].startswith("eeo[") else "main"
        field["description"] = ""
        if not field.get("widget"):
            field.pop("widget", None)
    return fields


def read_form(page, board: str, job_id: str, data: dict) -> tuple[list[dict], list[str]]:
    page.goto(apply_url(board, job_id), wait_until="domcontentloaded")
    try:
        page.locator("#application-form, form[action*='/apply']").first.wait_for(timeout=20000)
    except Exception:  # noqa: BLE001 - no form: closed or applications paused.
        raise Closed("This Lever posting shows no application form.") from None
    return page_fields(page), []


def named(page, key: str):
    return page.locator(f'[name="{key}"]')


def fill_field(page, field: dict, value, files: dict) -> None:
    key, kind, widget = field["key"], field["kind"], field.get("widget")
    if kind == "file":
        path = files.get(value)
        if not path:
            raise FillError(key, field["label"], f"no {value} file in the packet")
        named(page, key).first.set_input_files(str(path))
        # Lever uploads at once and pre-fills fields from the resume. Wait for a
        # sign it finished; if the upload failed, Lever's own error stops submit.
        try:
            page.wait_for_function(
                "name => [...document.querySelectorAll('.application-question, .resume-upload')]"
                ".some(q => q.innerText.includes(name) || /success/i.test(q.innerText))",
                arg=path.name, timeout=15000)
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(1500)
        return
    if widget == "native":
        select_native(named(page, key).first, str(value), field)
        return
    if widget in ("radio", "checkbox"):
        values = value if isinstance(value, list) else [value]
        inputs = page.locator(f'input[type="{widget}"][name="{key}"]')
        labels = [clean(inputs.nth(i).evaluate(
            "el => (el.closest('label') && el.closest('label').innerText) || el.value"))
            for i in range(inputs.count())]
        check_labeled(inputs, labels, values, field, only=widget == "checkbox")
        return
    if widget == "lever-location":
        box = page.locator("#location-input")
        box.fill("")
        box.press_sequentially(str(value)[:40], delay=30)
        options = page.locator(".dropdown-location, .dropdown-results div:visible")
        try:
            options.first.wait_for(timeout=6000)
            texts = [clean(t) for t in options.all_inner_texts()]
            # The suggestion that is this place ("Portland, ME" for "Portland, ME"), else the top one.
            options.nth(pick_option(texts, str(value), loose=True) or 0).click()
        except Exception:  # noqa: BLE001 - no suggestion list: the typed text stands.
            pass
        if not box.input_value().strip():
            raise FillError(key, field["label"], "location didn't stick")
        return
    type_into(named(page, key).first, str(value), field)


def fill_form(page, packet: dict, files: dict) -> None:
    fill_all(page, packet, files, fill_field)


def submit(page, ask) -> dict:
    before = visible_text(page)
    page.locator("#btn-submit, button[type=submit]").first.click()
    return wait_for_result(page, ask, before=before, done_url=("/thanks",),
                           errors=".application-error, .error-message:visible, [class*=error]:visible")
