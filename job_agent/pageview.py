"""What the explore agent sees and does on a page: numbered visible elements.

The model never writes selectors. Each snapshot tags the visible interactive
elements in every frame with `data-agent-ref`; actions name those refs. Only
what a person could see is reported, so hidden text and hidden (honeypot)
fields never reach the model. For recording, each element also reports a
stable target: its name attribute, label, and the question its group asks.
"""

import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError

from .browser import VISIBLE_TEXT_JS

SNAPSHOT_JS = r"""
(start) => {
  const shown = (el) => {
    // [inert] can't be used; aria-hidden only hides from screen readers (Meta sets it on the whole page).
    if (el.closest('[hidden], [inert]')) return false;
    if (!el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    // Elements parked far outside the document are not visible to a person.
    if (r.right + scrollX < 0 || r.bottom + scrollY < 0) return false;
    return true;
  };
  const labelsOf = (el) => {
    const found = [...(el.labels || [])];
    const wrap = el.closest('label');
    if (wrap && !found.includes(wrap)) found.push(wrap);
    return found;
  };
  const clean = (t, n = 120) => (t || '').replace(/\s+/g, ' ').trim().slice(0, n);
  const nameOf = (el) => {
    const by = el.getAttribute('aria-labelledby');
    if (by) {
      const t = by.split(/\s+/).map((id) => document.getElementById(id)?.innerText || '').join(' ');
      if (clean(t)) return clean(t);
    }
    const label = labelsOf(el).map((l) => l.innerText).join(' ');
    return clean(el.getAttribute('aria-label')) || clean(label) ||
      clean(el.getAttribute('placeholder')) || clean(el.getAttribute('title')) ||
      (['INPUT', 'SELECT', 'TEXTAREA'].includes(el.tagName) ? '' : clean(el.innerText)) ||
      clean(el.getAttribute('name')) || clean(el.getAttribute('alt'));
  };
  const query = [
    'a[href]', 'button', 'input:not([type=hidden])', 'select', 'textarea',
    '[role=button]', '[role=link]', '[role=checkbox]', '[role=radio]', '[role=combobox]',
    '[role=option]', '[role=menuitem]', '[role=tab]', '[role=switch]', '[role=textbox]',
    '[contenteditable=true]',
  ].join(',');
  document.querySelectorAll('[data-agent-ref]').forEach((el) => el.removeAttribute('data-agent-ref'));
  const items = [];
  let ref = start;
  for (const el of document.querySelectorAll(query)) {
    const type = (el.getAttribute('type') || '').toLowerCase();
    // Styled checkboxes and upload buttons often hide the native input behind a
    // visible label; those count as visible when their label does.
    const viaLabel = ['checkbox', 'radio', 'file'].includes(type) && labelsOf(el).some(shown);
    const fileInput = type === 'file' && !el.closest('[aria-hidden="true"]');
    if (!shown(el) && !viaLabel && !fileInput) continue;
    el.setAttribute('data-agent-ref', String(ref));
    const item = { ref: String(ref), tag: el.tagName.toLowerCase(), name: nameOf(el) };
    const attr = el.getAttribute('name');
    if (attr) item.field = attr;
    if (['checkbox', 'radio'].includes(type) || ['checkbox', 'radio'].includes(el.getAttribute('role'))) {
      const group = el.closest('fieldset, [role=radiogroup], [role=group]');
      const legend = group && (group.querySelector('legend') ||
        document.getElementById(group.getAttribute('aria-labelledby') || '-'));
      const question = clean(legend ? legend.innerText : group?.getAttribute('aria-label'));
      if (question) item.group = question;
    }
    if (type) item.type = type;
    const role = el.getAttribute('role');
    if (role) item.role = role;
    if (el.required || el.getAttribute('aria-required') === 'true') item.required = true;
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') item.disabled = true;
    if (el.tagName === 'A') item.href = el.href.slice(0, 200);
    if (['checkbox', 'radio'].includes(type)) item.checked = el.checked;
    else if (role === 'checkbox' || role === 'radio' || role === 'switch')
      item.checked = el.getAttribute('aria-checked') === 'true';
    if (el.tagName === 'SELECT') {
      item.value = clean(el.selectedOptions[0]?.text, 80);
      item.options = [...el.options].slice(0, 40).map((o) => clean(o.text, 80));
    } else if (type === 'password') {
      item.value = el.value ? '(filled)' : '';
    } else if (type === 'file') {
      item.value = [...el.files].map((f) => f.name).join(', ');
    } else if ('value' in el && ['INPUT', 'TEXTAREA'].includes(el.tagName)) {
      item.value = clean(el.value, 200);
    }
    if (role === 'combobox') item.expanded = el.getAttribute('aria-expanded') === 'true';
    items.push(item);
    ref++;
  }
  return { items, next: ref };
}
"""

CAPTCHA_FRAME = re.compile(
    r"recaptcha|hcaptcha|challenges\.cloudflare\.com|turnstile|arkoselabs|funcaptcha|geetest", re.I)
CAPTCHA_TEXT = re.compile(
    r"verify (that )?you are (a )?human|are you a robot|i'?m not a robot|security check|"
    r"press (and|&) hold|unusual traffic", re.I)
FINAL_SUBMIT = re.compile(
    r"^\s*(submit( (my |your )?application)?|send application|finish( application)?|"
    r"complete application|apply now and submit)\s*$", re.I)


# Connection failures that pass: a dropped socket, a VPN or Wi-Fi change, a DNS hiccup.
TRANSIENT_NET = re.compile(
    r"net::ERR_(SOCKET_NOT_CONNECTED|CONNECTION_(RESET|CLOSED|ABORTED|REFUSED|TIMED_OUT)|NETWORK_CHANGED|"
    r"TIMED_OUT|EMPTY_RESPONSE|NAME_RESOLUTION_FAILED|INTERNET_DISCONNECTED|HTTP2_PROTOCOL_ERROR)")


def goto(page, url: str, tries: int = 3, **options):
    """page.goto with wait_until=domcontentloaded, tried again after a passing network error."""
    options.setdefault("wait_until", "domcontentloaded")
    for attempt in range(tries):
        try:
            return page.goto(url, **options)
        except PlaywrightError as error:
            found = TRANSIENT_NET.search(str(error))
            if not found:
                raise
            if attempt == tries - 1:
                raise PlaywrightError(f"The page didn't load after {tries} tries ({found.group(0)} at {url}). "
                                      "This is a network problem on the way to the site, not the job; "
                                      "trying again later is safe.") from error
            page.wait_for_timeout(1000 * 2 ** attempt)


def follow_new_tab(context, page, before: int, wait: float = 1.5):
    """After a click: the tab it opened (loaded), else the same page after a short pause."""
    deadline = time.time() + wait
    while time.time() < deadline:
        if len(context.pages) > before:
            page = context.pages[-1]
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10000)
            except PlaywrightError:
                pass
            return page
        page.wait_for_timeout(200)  # Lets Playwright process the new-tab event (time.sleep wouldn't).
    return page



class PageView:
    """Snapshots and ref-based actions on one tab (new tabs it opens become current)."""

    def __init__(self, context, page):
        self.context, self.page = context, page
        self.refs: dict[str, object] = {}       # ref -> frame
        self.elements: dict[str, dict] = {}     # ref -> snapshot item
        context.on("page", self._adopt)

    def _adopt(self, page) -> None:
        self.page = page

    def settle(self) -> None:
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=10000)
            self.page.wait_for_load_state("networkidle", timeout=4000)
        except PlaywrightError:
            pass  # Long-polling pages never go idle; the DOM is usable anyway.

    def snapshot(self, max_text: int = 5000) -> dict:
        self.settle()
        self.refs, self.elements, items, texts, captcha = {}, {}, [], [], False
        next_ref = 1
        for frame in self.page.frames:
            if frame != self.page.main_frame and CAPTCHA_FRAME.search(frame.url or ""):
                captcha = True
                continue
            try:
                result = frame.evaluate(SNAPSHOT_JS, next_ref)
                frame_text = frame.evaluate(VISIBLE_TEXT_JS)["text"]
            except PlaywrightError:
                continue  # Detached or cross-origin frames that refuse scripts.
            for item in result["items"]:
                self.refs[item["ref"]] = frame
                self.elements[item["ref"]] = {**item, "frame": urlsplit(frame.url).path}
            items.extend(result["items"])
            next_ref = result["next"]
            if frame_text.strip():
                texts.append(frame_text)
        text = "\n\n".join(texts)
        captcha = captcha or bool(CAPTCHA_TEXT.search(text[:3000]))
        snapshot = {"url": self.page.url, "title": self.page.title(), "text": text[:max_text],
                    "text_truncated": len(text) > max_text,
                    "elements": [{k: v for k, v in i.items() if k != "field"} for i in items[:250]]}
        if captcha:
            snapshot["captcha_detected"] = ("A human-verification challenge is present. Don't try to "
                                            "solve it: call ask_user so the user completes it.")
        return snapshot

    def has_password_field(self) -> bool:
        return any(e.get("type") == "password" for e in self.elements.values())

    def element(self, ref: str) -> dict:
        if str(ref) not in self.elements:
            raise ValueError(f"Unknown ref {ref!r}. Take a new snapshot first.")
        return self.elements[str(ref)]

    def locate(self, ref: str):
        self.element(ref)
        return self.refs[str(ref)].locator(f'[data-agent-ref="{ref}"]').first

    def goto(self, url: str) -> None:
        if not re.match(r"^https?://", url):
            raise ValueError("Use an absolute http(s) URL.")
        goto(self.page, url)

    def click(self, ref: str) -> None:
        locator = self.locate(ref)
        before = len(self.context.pages)
        try:
            locator.scroll_into_view_if_needed(timeout=5000)
            locator.click(timeout=8000)
        except PlaywrightError:
            # A visually hidden checkbox or radio is reachable through its label.
            if not locator.evaluate("el => { const l = (el.labels && el.labels[0]) || el.closest('label');"
                                    " if (l) { l.click(); return true; } return false; }"):
                raise
        self.page = follow_new_tab(self.context, self.page, before)


    def fill(self, ref: str, text: str) -> None:
        locator = self.locate(ref)
        locator.scroll_into_view_if_needed(timeout=5000)
        if locator.evaluate("el => el.isContentEditable"):
            locator.click()
            self.page.keyboard.press("ControlOrMeta+A")
            self.page.keyboard.insert_text(text)
        else:
            locator.fill(text)
        locator.evaluate("el => el.dispatchEvent(new Event('blur', {bubbles: true}))")  # Validate on blur.

    def select(self, ref: str, option: str) -> str:
        locator = self.locate(ref)
        try:
            locator.select_option(label=option)
        except PlaywrightError:
            locator.select_option(value=option)
        return locator.evaluate("el => el.selectedOptions[0]?.text || ''")

    def set_checked(self, ref: str, checked: bool) -> None:
        locator = self.locate(ref)
        try:
            locator.set_checked(checked, timeout=5000)
        except PlaywrightError:
            current = locator.evaluate("el => el.checked ?? el.getAttribute('aria-checked') === 'true'")
            if bool(current) != checked:
                self.click(ref)

    def upload(self, ref: str, path: Path) -> None:
        locator = self.locate(ref)
        if locator.evaluate("el => el.tagName === 'INPUT' && el.type === 'file'"):
            locator.set_input_files(str(path))
            return
        with self.page.expect_file_chooser(timeout=8000) as chooser:  # A styled upload button.
            locator.click()
        chooser.value.set_files(str(path))

    def press(self, key: str) -> None:
        self.page.keyboard.press(key)
        time.sleep(0.4)

    def scroll(self, direction: str) -> None:
        self.page.mouse.wheel(0, 900 if direction == "down" else -900)
        time.sleep(0.4)

    def read_text(self, offset: int = 0, length: int = 12000) -> dict:
        texts = []
        for frame in self.page.frames:
            if CAPTCHA_FRAME.search(frame.url or ""):
                continue
            try:
                texts.append(frame.evaluate(VISIBLE_TEXT_JS)["text"])
            except PlaywrightError:
                continue
        text = "\n\n".join(t for t in texts if t.strip())
        return {"url": self.page.url, "offset": offset, "total_length": len(text),
                "text": text[offset:offset + length]}
