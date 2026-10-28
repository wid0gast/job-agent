"""Ashby: the form schema from Ashby's public job-page API, widgets from the page.

The job page loads its form from a GraphQL endpoint that needs no login; it
gives each field's path, type, label, required flag and options. On the page,
each field sits in a box carrying `data-field-path` (survey questions: a
fieldset whose label points at the path). Reading and filling both run TAG_JS,
which marks every box with `data-agent-key`, so they always agree on which box
is which. Widgets: text inputs, yes/no buttons, radios, checkboxes, type-ahead
comboboxes (location, school) and month/year dropdowns (education dates).
"""

from .pageview import goto
from .forms import (Closed, FillError, fill_all, same_text, text_value, answered, check_labeled, clean, combobox,  # noqa: F401
                    select_native, type_into, visible_text, wait_for_result)

GRAPHQL = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting"
JOBS = "https://jobs.ashbyhq.com"
QUERY = """
query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) {
  jobPosting(organizationHostedJobsPageName: $organizationHostedJobsPageName,
             jobPostingId: $jobPostingId) {
    id title descriptionHtml
    applicationForm { sections { fieldEntries { ... on FormFieldEntry {
      id field isRequired descriptionHtml } } } }
    surveyForms { sections { fieldEntries { ... on FormFieldEntry {
      id field isRequired descriptionHtml } } } }
  }
}
"""

TAG_JS = r"""
() => {
  const tidy = (t) => (t || '').replace(/\s+/g, ' ').trim().replace(/\s*\*$/, '').trim();
  const fields = [];
  const visible = (el) => el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true });
  const describe = (box, key, label, required, section) => {
    box.setAttribute('data-agent-key', key);
    const radios = [...box.querySelectorAll('input[type=radio]')];
    const checks = [...box.querySelectorAll('input[type=checkbox]')]
      .filter((c) => !c.closest('.ashby-application-form-input-yesno'));
    const labelOf = (input) => tidy(document.querySelector(`label[for="${CSS.escape(input.id)}"]`)?.innerText || input.name);
    let kind = 'text', widget, options = [];
    if (box.querySelector('.ashby-application-form-input-yesno')) {
      kind = 'select'; widget = 'yesno'; options = ['Yes', 'No'];
    } else if (radios.length) { kind = 'select'; widget = 'radio'; options = radios.map(labelOf); }
    else if (checks.length) { kind = 'multiselect'; widget = 'checkbox'; options = checks.map(labelOf); }
    else if (box.querySelector('input[type=file]')) kind = 'file';
    else if (box.querySelector('[role=combobox]')) { kind = 'search'; widget = 'combobox'; }
    else if (box.querySelector('select')) {
      kind = 'select'; widget = 'native';
      options = [...box.querySelector('select').options].filter((o) => o.value).map((o) => tidy(o.text));
    } else if (box.querySelector('textarea')) kind = 'textarea';
    fields.push({ key, label, kind, widget, options, required, section });
  };
  // Field boxes: <div data-field-path> entries, and fieldsets for radio groups and
  // surveys (their label id is "<form id>_<path>").
  const ENTRY = '.ashby-application-form-field-entry, fieldset[class*=_fieldEntry]';
  for (const entry of document.querySelectorAll(ENTRY)) {
    if (!visible(entry) || entry.parentElement.closest(ENTRY)) continue;
    const title = entry.querySelector('.ashby-application-form-question-title, label, legend');
    const raw = entry.dataset.fieldPath || title?.htmlFor || '';
    const key = raw.replace(/^[0-9a-f]{8}-[0-9a-f-]{27}_/, '');
    if (!key) continue;
    const label = tidy(title?.innerText);
    const required = !!title && /_required/.test(title.className);
    if (entry.querySelector('.ashby-application-form-input-education-entry')) {
      // One block of sub-fields (the first school); dates are month + year dropdowns.
      for (const sub of entry.querySelectorAll('.ashby-application-form-input-education-entry label[for]')) {
        const name = `${label}: ${tidy(sub.innerText)}`;
        const subRequired = /_required/.test(sub.className);
        // The sub-field's control has the id its label points at: an input, a
        // month + year pair of dropdowns, or a "still a student" checkbox.
        const control = document.getElementById(sub.htmlFor);
        if (control && control.type === 'checkbox') {
          control.setAttribute('data-agent-key', sub.htmlFor);
          fields.push({ key: sub.htmlFor, label: name, kind: 'select', widget: 'toggle',
            options: ['Yes', 'No'], required: subRequired, section: 'education' });
          continue;
        }
        const selects = control ? [...control.querySelectorAll('select')] : [];
        if (selects.length > 1) {
          selects.forEach((select, i) => {
            const part = i === 0 ? 'month' : 'year';
            const key2 = `${sub.htmlFor}|${part}`;
            select.setAttribute('data-agent-key', key2);
            fields.push({ key: key2, label: `${name} (${part})`, kind: 'select', widget: 'native',
              options: [...select.options].filter((o) => o.value).map((o) => tidy(o.text)),
              required: subRequired, section: 'education' });
          });
        } else {
          describe(control && control.tagName !== 'INPUT' ? control : sub.parentElement,
                   sub.htmlFor, name, subRequired, 'education');
        }
      }
      continue;
    }
    describe(entry, key, label, required, entry.tagName === 'FIELDSET' ? 'eeo' : 'main');
  }
  return fields;
}
"""


def resolve(client, job: dict) -> tuple[str | None, str]:
    return job["ats_board"], job["ats_job_id"]


def fetch(client, board: str, job_id: str) -> dict:
    response = client.post(GRAPHQL, json={
        "operationName": "ApiJobPosting", "query": QUERY,
        "variables": {"organizationHostedJobsPageName": board, "jobPostingId": job_id}})
    response.raise_for_status()
    body = response.json()
    posting = (body.get("data") or {}).get("jobPosting")
    if not posting:
        raise Closed(f"Ashby posting {board}/{job_id} not found.")
    return {"title": posting.get("title"), "html": posting.get("descriptionHtml", ""), "data": posting}


def apply_url(board: str, job_id: str) -> str:
    return f"{JOBS}/{board}/{job_id}/application"


def schema_fields(data: dict) -> dict:
    """path → {kind, options, required, description} from the API."""
    out = {}
    forms = [data.get("applicationForm") or {}] + list(data.get("surveyForms") or [])
    for form in forms:
        for section in form.get("sections", []):
            for entry in section.get("fieldEntries", []):
                field = entry.get("field") or {}
                path = field.get("path")
                if not path:
                    continue
                out[path] = {"type": field.get("type"), "required": bool(entry.get("isRequired")),
                             "options": [v.get("label") for v in field.get("selectableValues") or []],
                             "description": entry.get("descriptionHtml") or ""}
    return out


def page_fields(page, schema: dict | None = None) -> list[dict]:
    fields = page.evaluate(TAG_JS)
    from .sources import html_to_text

    for field in fields:
        known = (schema or {}).get(field["key"])
        if known:
            field["required"] = field["required"] or known["required"]
            if known["options"] and field.get("widget") in ("combobox", None) and field["kind"] != "file":
                field["kind"] = "multiselect" if known["type"] == "MultiValueSelect" else "select"
                field["options"] = known["options"]
            field["description"] = html_to_text(known["description"])[:600]
        else:
            field.setdefault("description", "")
        if not field.get("widget"):
            field.pop("widget", None)
    return fields


def read_form(page, board: str, job_id: str, data: dict) -> tuple[list[dict], list[str]]:
    goto(page, apply_url(board, job_id))
    try:
        page.locator(".ashby-application-form-field-entry").first.wait_for(timeout=20000)
    except Exception:  # noqa: BLE001
        raise Closed("This Ashby posting shows no application form.") from None
    page.wait_for_timeout(800)
    schema = schema_fields(data)
    fields = page_fields(page, schema)
    seen = {f["key"] for f in fields}
    notes = [f"Not shown on the form: {path}" for path, info in schema.items()
             if path not in seen and info["required"] and info["type"] != "EducationHistory"]
    return fields, notes


def box(page, key: str):
    """The field's box. Ashby re-renders parts of the form as you fill it, which
    drops our tags, so tag again when a box is missing."""
    found = page.locator(f'[data-agent-key="{key}"]')
    if not found.count():
        page.evaluate(TAG_JS)
    return found.first


def fill_field(page, field: dict, value, files: dict) -> None:
    kind, widget = field["kind"], field.get("widget")
    holder = box(page, field["key"])
    if kind == "file":
        path = files.get(value)
        if not path:
            raise FillError(field["key"], field["label"], f"no {value} file in the packet")
        holder.locator("input[type=file]").set_input_files(str(path))
        holder.filter(has_text=path.name).wait_for(timeout=15000)
        return
    if widget == "toggle":
        holder.set_checked(str(value).lower().startswith("y"), force=True)
        return
    if widget == "yesno":
        option = "yes" if str(value).lower().startswith("y") else "no"
        button = holder.locator(f'button[data-option="{option}"]')
        button.click()
        if button.get_attribute("aria-pressed") != "true":
            raise FillError(field["key"], field["label"], "yes/no didn't stick")
        return
    if widget in ("radio", "checkbox"):
        values = value if isinstance(value, list) else [value]
        inputs = holder.locator(f"input[type={widget}]")
        if widget == "checkbox":
            inputs = holder.locator("input[type=checkbox]:not(.ashby-application-form-input-yesno input)")
        labels = [clean(inputs.nth(i).evaluate(
            "el => document.querySelector(`label[for=\"${CSS.escape(el.id)}\"]`)?.innerText || el.name"))
            for i in range(inputs.count())]
        check_labeled(inputs, labels, values, field, only=widget == "checkbox")
        return
    if widget == "native":
        target = holder if holder.evaluate("el => el.tagName") == "SELECT" else holder.locator("select")
        select_native(target, str(value), field)
        return
    if widget == "combobox" or kind == "search":
        combobox(page, holder.locator("[role=combobox]"), str(value), field)
        return
    type_into(holder.locator("input:not([type=file]):not([type=checkbox]):not([type=radio]), textarea").first,
              str(value), field)


def fill_form(page, packet: dict, files: dict) -> None:
    page.evaluate(TAG_JS)  # Tag the boxes on this page load.
    fill_all(page, packet, files, fill_field)


def submit(page, ask) -> dict:
    before = visible_text(page)
    page.locator("button").filter(has_text="Submit Application").first.click()
    return wait_for_result(page, ask, before=before, errors="[class*=_error]:visible, .ashby-application-form-error")
