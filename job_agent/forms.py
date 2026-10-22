"""What every application system's filler shares: option matching, standard
widgets, and waiting for the result after submit.

Each system (greenhouse.py, lever.py, ashby.py) describes its form as fields:
    {"key", "label", "kind", "required", "options", "section", "description",
     "widget"?}
kind is text | textarea | file | select | multiselect | search (typed into,
then a suggestion is picked). widget says how a choice is shown when it isn't
the system's default: native (<select>), radio, checkbox, yesno, combobox.
"""

import re
import time

CONFIRMED = re.compile(
    r"thank you for (applying|your application|your interest)|thanks for applying|"
    r"application (was |has been )?(successfully )?(submitted|received)|"
    r"we('ve| have) received your application|your application (is|was) (in|complete)", re.I)
SECURITY_CODE = re.compile(r"security code|verification code|confirm you'?re a human", re.I)
LIMIT_REACHED = re.compile(r"(reached|exceeded) (the |your )?(maximum|application limit|limit)", re.I)
CHALLENGE_FRAME = re.compile(r"recaptcha.*bframe|hcaptcha.*(challenge|frame=challenge)|challenges\.cloudflare")


class Closed(Exception):
    """The posting no longer exists or no longer takes applications."""


class FillError(Exception):
    def __init__(self, key: str, label: str, problem: str, options: list[str] | None = None):
        super().__init__(f"{label or key}: {problem}")
        self.key, self.label, self.problem = key, label, problem
        self.options = options or []  # The site's real choices, when known, for you to pick from.


def clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return re.sub(r"\s*[*✱]\s*$", "", text).strip()


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9+]+", " ", (text or "").lower()).strip()


def pick_option(options: list[str], wanted: str, loose: bool) -> int | None:
    target = norm(wanted)
    texts = [norm(o) for o in options]
    for index, text in enumerate(texts):
        if text == target:
            return index
    if loose:  # Search fields: "New York" → "New York, New York, United States".
        # Several may fit ("United States" → "...of America", "...Minor Outlying
        # Islands"); the shortest adds the least, so it's the likeliest meaning.
        for test in (lambda t: t.startswith(target + " "),
                     lambda t: all(w in t.split() for w in target.split())):
            hits = [i for i, t in enumerate(texts) if test(t)]
            if hits:
                return min(hits, key=lambda i: len(texts[i]))
    return None


def unexpected(fields: list[dict], packet: dict) -> list[str]:
    """Differences between the live form's fields and what you approved. Any → don't submit."""
    approved = {a["key"]: a for a in packet["answers"]}
    problems = []
    for field in fields:
        answer = approved.get(field["key"])
        if answer is None:
            if field["required"]:
                problems.append(f"New required question: {field['label']}")
            continue
        if field["kind"] in ("select", "multiselect") and answer.get("options") and field["options"]:
            if set(map(norm, field["options"])) != set(map(norm, answer["options"])):
                problems.append(f"Options changed: {field['label']}")
    return problems


def answered(packet: dict) -> list[dict]:
    """Answers to fill, files first: some systems read the resume and pre-fill fields."""
    todo = [a for a in packet["answers"] if a.get("value") not in (None, "", [])]
    return sorted(todo, key=lambda a: a["kind"] != "file")


# ----- standard widgets -------------------------------------------------------

def text_value(value) -> str:
    """Text as a form field holds it: line breaks as \n (a browser's text box stores \r\n as \n)."""
    return str(value).replace("\r\n", "\n").replace("\r", "\n")


def same_text(shown: str, wanted) -> bool:
    return text_value(shown).strip() == text_value(wanted).strip()


def fill_all(page, packet: dict, files: dict, fill_field) -> None:
    """Fill every answer, then report every field that failed (one problem never leaves the rest
    of the form empty). Nothing is submitted while any failed."""
    failed = []
    for answer in answered(packet):
        try:
            fill_field(page, answer, answer["value"], files)
        except FillError as error:
            failed.append(error)
    if len(failed) == 1:
        raise failed[0]
    if failed:
        raise FillError(failed[0].key, f"{len(failed)} fields",
                        "; ".join(f"“{e.label[:80]}”: {e.problem}" for e in failed))


def type_into(locator, value: str, field: dict) -> None:
    locator.scroll_into_view_if_needed(timeout=5000)
    locator.fill(text_value(value))
    locator.evaluate("el => el.dispatchEvent(new Event('blur', {bubbles: true}))")
    if not same_text(locator.input_value(), value):
        raise FillError(field["key"], field["label"], "value didn't stick")


def select_native(locator, value: str, field: dict) -> None:
    options = [clean(t) for t in locator.locator("option").all_inner_texts()]
    index = pick_option(options, value, loose=False)
    if index is None:
        raise FillError(field["key"], field["label"], f"no option {value!r}")
    locator.select_option(index=index)


def check_labeled(inputs, labels: list[str], values: list[str], field: dict, only=False) -> None:
    """Radios or checkboxes: check those whose label matches `values` (and, with
    only=True, uncheck the rest)."""
    wanted = set()
    for value in values:
        index = pick_option(labels, str(value), loose=False)
        if index is None:
            raise FillError(field["key"], field["label"], f"no choice labeled {value!r}")
        wanted.add(index)
    for index in range(len(labels)):
        box = inputs.nth(index)
        if index in wanted and not box.is_checked():
            click_choice(box)
        elif only and index not in wanted and box.is_checked():
            click_choice(box)
    got = {i for i in range(len(labels)) if inputs.nth(i).is_checked()}
    if not wanted <= got or (only and got != wanted):
        raise FillError(field["key"], field["label"], "choice didn't stick")


def click_choice(box) -> None:
    """Styled radios and checkboxes often hide the input; click its label then."""
    try:
        box.check(timeout=3000) if box.get_attribute("type") == "radio" else box.click(timeout=3000)
    except Exception:  # noqa: BLE001 - covered or invisible input.
        clicked = box.evaluate(
            "el => { const l = (el.labels && el.labels[0]) || el.closest('label');"
            " if (l) { l.click(); return true; } el.click(); return true; }")
        if not clicked:
            raise


def combobox(page, box, value: str, field: dict, loose: bool = True, wait: float = 6) -> str:
    """An ARIA combobox: type, wait for suggestions, click the matching one.

    Location fields often know "New York, NY, USA" but not "New York, USA", so a
    search that finds nothing is retried with just the part before the comma.
    """
    box.scroll_into_view_if_needed(timeout=5000)
    queries = [str(value)]
    if "," in str(value):
        queries.append(str(value).split(",")[0].strip())
    index, options, listbox = None, [], suggestions(page, box)
    for query in queries:
        box.click()
        box.fill("")
        box.press_sequentially(query[:40], delay=25)
        deadline = time.time() + wait
        while time.time() < deadline:
            try:
                options = [clean(o) for o in listbox.all_inner_texts()]
            except Exception:  # noqa: BLE001 - re-rendering.
                options = []
            index = pick_option(options, query, loose)
            if index is not None:
                break
            time.sleep(0.4)
        if index is not None:
            break
    if index is None:
        page.keyboard.press("Escape")
        raise FillError(field["key"], field["label"], f"no suggestion matching {value!r}")
    picked = options[index]
    listbox.nth(index).click()
    page.wait_for_timeout(300)
    return picked


def suggestions(page, box):
    controls = box.get_attribute("aria-controls") or box.get_attribute("aria-owns")
    if controls:
        return page.locator(f'[id="{controls}"] [role="option"]')
    return page.locator('[role="listbox"]:visible [role="option"], [role="option"]:visible')


# ----- after submit ------------------------------------------------------------

def wait_for_result(page, ask, *, done_url: tuple[str, ...] = (), errors: str = "",
                    resubmit=None, timeout: float = 45) -> dict:
    """Wait for a confirmation, handing codes and human checks to you.

    `ask(question)` returns your answer (the dashboard shows the question).
    `resubmit()` clicks submit again after a code is entered.
    Returns {"status": "submitted" | "needs_you", "detail": ...}.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        page.wait_for_timeout(1000)
        text = visible_text(page)
        if any(part in page.url for part in done_url) or CONFIRMED.search(text):
            return {"status": "submitted", "detail": first_line(CONFIRMED, text) or page.url}
        if LIMIT_REACHED.search(text):
            return {"status": "needs_you", "limit": True,
                    "detail": f"The site says: {first_line(LIMIT_REACHED, text)}"}
        if resubmit and SECURITY_CODE.search(text) and page.locator("input:visible").count():
            code = ask("The site emailed you a security code for this application. "
                       "Enter it here.").strip()
            enter_code(page, code)
            resubmit()
            deadline = time.time() + timeout
            continue
        if challenge_visible(page):
            ask("A human-verification check appeared in the Chrome window. Complete it "
                "there, then reply here with 'done'.")
            deadline = time.time() + timeout
            continue
        if errors:
            messages = [m.strip() for m in page.locator(errors).all_inner_texts() if m.strip()]
            if messages:
                return {"status": "needs_you",
                        "detail": "The form showed errors: " + "; ".join(messages[:5])}
    return {"status": "needs_you",
            "detail": "Clicked submit but saw no confirmation. Check your email or the site."}


def enter_code(page, code: str) -> None:
    boxes = page.locator("input[id^='security-input']:visible")
    if boxes.count() > 1:
        for index, char in enumerate(code[: boxes.count()]):
            boxes.nth(index).fill(char)
    else:
        page.locator("input:visible").last.fill(code)


def challenge_visible(page) -> bool:
    for frame in page.frames:
        if CHALLENGE_FRAME.search(frame.url or ""):
            try:
                if frame.frame_element().is_visible():
                    box = frame.frame_element().bounding_box()
                    if box and box["width"] > 100 and box["height"] > 100:
                        return True
            except Exception:  # noqa: BLE001 - frame detached.
                continue
    return False


def visible_text(page) -> str:
    try:
        return page.evaluate("() => document.body ? document.body.innerText : ''")
    except Exception:  # noqa: BLE001 - navigating.
        return ""


def first_line(pattern, text: str) -> str:
    for line in text.splitlines():
        if pattern.search(line):
            return line.strip()[:200]
    return ""
