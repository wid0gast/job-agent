"""A visible Chrome window the agent drives through numbered element references.

The model never writes selectors. Each snapshot tags the visible interactive
elements in every frame with `data-agent-ref`, and actions name those refs.
Only what a person could see is reported, so hidden text and hidden fields on
a page never reach the model.
"""

import re
import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

from . import config

# Text a person can actually see. innerText alone still includes white-on-white
# and 1px text, which is how pages hide instructions aimed at AI agents.
VISIBLE_TEXT_JS = r"""
() => {
  const rgba = (c) => (c.match(/[\d.]+/g) || []).map(Number);
  const background = (el) => {
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      const c = rgba(getComputedStyle(n).backgroundColor);
      if (c.length >= 3 && (c[3] === undefined || c[3] > 0.5)) return c;
    }
    return [255, 255, 255];
  };
  const hiddenCache = new Map();
  const hidden = (el) => {
    if (hiddenCache.has(el)) return hiddenCache.get(el);
    let result = false;
    if (el.closest('[aria-hidden="true"], [hidden], script, style, noscript, template')) result = true;
    else {
      const s = getComputedStyle(el);
      const r = el.getBoundingClientRect();
      const fg = rgba(s.color), bg = background(el);
      const contrast = Math.abs(fg[0] - bg[0]) + Math.abs(fg[1] - bg[1]) + Math.abs(fg[2] - bg[2]);
      // checkVisibility covers display:none ancestors, visibility and opacity.
      result = !el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true }) ||
        parseFloat(s.fontSize) < 6 || (fg[3] !== undefined && fg[3] < 0.1) || contrast < 30 ||
        (r.width < 1 && r.height < 1) || r.right + scrollX < 0 || r.bottom + scrollY < 0 ||
        (s.overflow === 'hidden' && (r.width < 2 || r.height < 2));
    }
    hiddenCache.set(el, result);
    return result;
  };
  const out = [];
  const walker = document.createTreeWalker(document.body || document, NodeFilter.SHOW_TEXT);
  let last = null;
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const text = node.nodeValue.replace(/\s+/g, ' ').trim();
    const el = node.parentElement;
    if (!text || !el || hidden(el)) continue;
    const block = el.closest('p,div,li,h1,h2,h3,h4,h5,h6,tr,section,article,label,button,a,dt,dd,legend,option');
    out.push((last && block !== last ? '\n' : ' ') + text);
    last = block;
  }
  return out.join('').replace(/\n{3,}/g, '\n\n').trim();
}
"""

SNAPSHOT_JS = r"""
(start) => {
  const shown = (el) => {
    if (el.closest('[aria-hidden="true"], [hidden], [inert]')) return false;
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
    r"recaptcha|hcaptcha|challenges\.cloudflare\.com|turnstile|arkoselabs|funcaptcha|geetest",
    re.I,
)
CAPTCHA_TEXT = re.compile(
    r"verify (that )?you are (a )?human|are you a robot|i'?m not a robot|security check|"
    r"press (and|&) hold|unusual traffic",
    re.I,
)
FINAL_SUBMIT = re.compile(
    r"^\s*(submit( (my |your )?application)?|send application|finish( application)?|"
    r"complete application|apply now and submit)\s*$",
    re.I,
)


class Headless:
    """A headless browser for printing PDFs and reading public pages.

    Headed Chrome can't print, and reading a public form needs no window.
    """

    # Letter paper minus the PDF margins below, in CSS pixels (96 per inch).
    PAGE_WIDTH, PAGE_HEIGHT = (8.5 - 1.2) * 96, (11 - 1.0) * 96

    def __init__(self, playwright, channel: str = "chrome"):
        self.playwright, self.channel, self._browser = playwright, channel, None

    _shared: dict = {}  # Hosted: one Chromium per Playwright, for reading and applying alike (memory is tight).

    @property
    def browser(self):
        if self._browser is None:
            if config.HOSTED:
                from . import hosted

                shared = Headless._shared.get(id(self.playwright))
                if shared is None or not shared.is_connected():
                    shared = self.playwright.chromium.launch(**hosted.chromium_options())
                    Headless._shared[id(self.playwright)] = shared
                self._browser = shared
                return shared
            try:
                self._browser = self.playwright.chromium.launch(channel=self.channel or None)
            except PlaywrightError:
                self._browser = self.playwright.chromium.launch()
        return self._browser

    def new_page(self) -> Page:
        page = self.browser.new_page()
        page.set_default_timeout(15000)
        return page

    def render_pdf(self, html: str, output: Path) -> int:
        """Write the PDF and return how many pages it has."""
        page = self.browser.new_page(viewport={"width": int(self.PAGE_WIDTH), "height": 900})
        try:
            page.set_content(html, wait_until="load")
            page.emulate_media(media="print")
            height = page.evaluate("() => document.documentElement.scrollHeight")
            page.pdf(
                path=str(output),
                format="Letter",
                margin={"top": "0.5in", "bottom": "0.5in", "left": "0.6in", "right": "0.6in"},
                print_background=True,
            )
            return max(1, -(-int(height) // int(self.PAGE_HEIGHT)))
        finally:
            page.close()

    def visible_text(self, html: str) -> str:
        """Text a person would see in this HTML; hidden instructions are dropped."""
        page = self.browser.new_page()
        try:
            page.set_content(f"<!doctype html><body>{html}</body>", wait_until="load")
            return page.evaluate(VISIBLE_TEXT_JS)
        finally:
            page.close()

    def close(self) -> None:
        if self._browser:
            try:
                self._browser.close()
            except PlaywrightError:
                pass  # Hosted: the shared browser was already closed.
            self._browser = None


class Browser:
    """Owns one persistent Chrome profile so site logins survive between runs."""

    def __init__(self, profile_dir: Path, channel: str = "chrome", headless=False,
                 playwright=None):
        self._owns_playwright = playwright is None
        self.playwright = playwright or sync_playwright().start()
        self.channel = channel
        self._printer = Headless(self.playwright, channel)
        options = dict(
            user_data_dir=str(profile_dir),
            headless=headless,
            viewport=None if not headless else {"width": 1280, "height": 900},
            accept_downloads=True,
        )
        if config.HOSTED:  # Sessions don't persist, so no saved profile: a fresh context each visit.
            self.context = self._printer.browser.new_context(
                viewport={"width": 1280, "height": 900}, accept_downloads=True, ignore_https_errors=True)
        else:
            try:
                self.context = self.playwright.chromium.launch_persistent_context(
                    channel=channel or None, **options
                )
            except PlaywrightError:
                # Fall back to Playwright's Chromium when Chrome is unavailable.
                self.context = self.playwright.chromium.launch_persistent_context(**options)
        self.context.set_default_timeout(15000)
        self.page: Page = (
            self.context.pages[0] if self.context.pages else self.context.new_page()
        )
        self.context.on("page", self._adopt)
        self.refs: dict[str, object] = {}

    def _adopt(self, page: Page) -> None:
        # "Apply" buttons commonly open the employer's form in a new tab.
        self.page = page
        page.set_default_timeout(15000)

    def close(self) -> None:
        try:
            self._printer.close()
            self.context.close()
        finally:
            if self._owns_playwright:
                self.playwright.stop()

    # ----- observation -------------------------------------------------------

    def settle(self) -> None:
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=10000)
            self.page.wait_for_load_state("networkidle", timeout=4000)
        except PlaywrightError:
            pass  # Long-polling pages never go idle; the DOM is usable anyway.

    def snapshot(self, max_text: int = 5000) -> dict:
        self.settle()
        self.refs, items, texts, captcha = {}, [], [], False
        next_ref = 1
        for frame in self.page.frames:
            if frame != self.page.main_frame and CAPTCHA_FRAME.search(frame.url or ""):
                captcha = True
                continue
            try:
                result = frame.evaluate(SNAPSHOT_JS, next_ref)
                frame_text = frame.evaluate(VISIBLE_TEXT_JS)
            except PlaywrightError:
                continue  # Detached or cross-origin frames that refuse scripts.
            for item in result["items"]:
                self.refs[item["ref"]] = frame
            items.extend(result["items"])
            next_ref = result["next"]
            if frame_text.strip():
                texts.append(frame_text)
        text = "\n\n".join(texts)
        captcha = captcha or bool(CAPTCHA_TEXT.search(text[:3000]))
        snapshot = {
            "url": self.page.url,
            "title": self.page.title(),
            "tabs": len(self.context.pages),
            "text": text[:max_text],
            "text_truncated": len(text) > max_text,
            "elements": items[:250],
        }
        if len(items) > 250:
            snapshot["elements_truncated"] = len(items)
        if captcha:
            snapshot["captcha_detected"] = True
            snapshot["captcha_instruction"] = (
                "A human-verification challenge is present. Do not try to solve "
                "or bypass it. Call ask_user so the user can complete it in the "
                "browser window, then take a new snapshot."
            )
        return snapshot

    def read_text(self, offset: int = 0, length: int = 12000) -> dict:
        texts = []
        for frame in self.page.frames:
            if CAPTCHA_FRAME.search(frame.url or ""):
                continue
            try:
                texts.append(frame.evaluate(VISIBLE_TEXT_JS))
            except PlaywrightError:
                continue
        text = "\n\n".join(t for t in texts if t.strip())
        return {
            "url": self.page.url,
            "offset": offset,
            "total_length": len(text),
            "text": text[offset : offset + length],
        }

    # ----- actions -----------------------------------------------------------

    def locate(self, ref: str):
        frame = self.refs.get(str(ref))
        if frame is None:
            raise ValueError(f"Unknown ref {ref!r}. Take a new snapshot first.")
        return frame.locator(f'[data-agent-ref="{ref}"]').first

    def describe(self, ref: str) -> str:
        locator = self.locate(ref)
        return locator.evaluate(
            "el => (el.innerText || el.value || el.getAttribute('aria-label') || '').trim()"
        )

    def goto(self, url: str) -> None:
        if not re.match(r"^https?://", url):
            raise ValueError("Use an absolute http(s) URL.")
        self.page.goto(url, wait_until="domcontentloaded")

    def click(self, ref: str) -> None:
        locator = self.locate(ref)
        try:
            locator.scroll_into_view_if_needed(timeout=5000)
            locator.click(timeout=8000)
        except PlaywrightError:
            # A visually hidden checkbox or radio is reachable through its label.
            clicked = locator.evaluate(
                "el => { const l = (el.labels && el.labels[0]) || el.closest('label');"
                " if (l) { l.click(); return true; } return false; }"
            )
            if not clicked:
                raise
        time.sleep(0.6)

    def fill(self, ref: str, text: str) -> None:
        locator = self.locate(ref)
        locator.scroll_into_view_if_needed(timeout=5000)
        editable = locator.evaluate("el => el.isContentEditable")
        if editable:
            locator.click()
            self.page.keyboard.press("ControlOrMeta+A")
            self.page.keyboard.insert_text(text)
        else:
            locator.fill(text)
        # Many forms validate on blur.
        locator.evaluate("el => el.dispatchEvent(new Event('blur', {bubbles: true}))")

    def type_text(self, ref: str, text: str) -> None:
        """Key-by-key typing for autocomplete fields that ignore fill()."""
        locator = self.locate(ref)
        locator.click()
        locator.press_sequentially(text, delay=40)

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
            current = locator.evaluate(
                "el => el.checked ?? el.getAttribute('aria-checked') === 'true'"
            )
            if bool(current) != checked:
                self.click(ref)

    def upload(self, ref: str, path: Path) -> None:
        locator = self.locate(ref)
        is_file_input = locator.evaluate(
            "el => el.tagName === 'INPUT' && el.type === 'file'"
        )
        if is_file_input:
            locator.set_input_files(str(path))
            return
        # A styled "Upload" button opens the file chooser.
        with self.page.expect_file_chooser(timeout=8000) as chooser:
            locator.click()
        chooser.value.set_files(str(path))

    def press(self, key: str) -> None:
        self.page.keyboard.press(key)
        time.sleep(0.4)

    def scroll(self, direction: str) -> None:
        self.page.mouse.wheel(0, 900 if direction == "down" else -900)
        time.sleep(0.4)

    def back(self) -> None:
        self.page.go_back(wait_until="domcontentloaded")

    def switch_tab(self, index: int) -> None:
        pages = self.context.pages
        if not 0 <= index < len(pages):
            raise ValueError(f"Tab index must be 0..{len(pages) - 1}.")
        self.page = pages[index]
        self.page.bring_to_front()

    def tabs(self) -> list[dict]:
        return [
            {"index": i, "url": p.url, "active": p == self.page}
            for i, p in enumerate(self.context.pages)
        ]

    def render_pdf(self, html: str, output: Path) -> int:
        return self._printer.render_pdf(html, output)
