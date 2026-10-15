"""Browsers: a headless one for PDFs and reading pages, and the Chrome window submits use."""

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
