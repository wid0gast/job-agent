"""Build the master resume from resume PDFs you upload.

Each PDF is turned into Markdown (the model only restructures; wording stays
as written). Then all of them are merged into one proposed master, oldest to
newest: every entry from any resume is kept, and where versions disagree the
newer one wins. The proposal waits for you; accepting it backs up the old
master first. Words and numbers that appear in none of your resumes are
flagged, because the master is what every truth check compares against.

    profile/resumes/<name>.pdf            what you uploaded
    profile/resumes/parsed/<name>.md      each one as Markdown (+ .json details)
    profile/resume.proposed.md            the merge waiting for you (+ .json)
    profile/resume.md                     the master, once you accept
    profile/resume-history/               earlier masters
"""

import json
import re
import shutil
import unicodedata
from datetime import datetime
from pathlib import Path

from . import config, documents

MAX_PDF_BYTES = 10 * 1024 * 1024
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
DATE = re.compile(r"\b(?:(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+)?((?:19|20)\d{2})\b", re.I)
WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#.\-]*[A-Za-z0-9+#]")
# Words the model may add for structure without adding a fact.
STRUCTURE = {"education", "experience", "projects", "skills", "summary", "publications",
             "awards", "certifications", "leadership", "activities", "interests", "languages",
             "research", "work", "and", "the", "present", "expected", "other", "volunteering",
             "honors", "coursework", "relevant", "technical", "professional", "selected"}

PARSE_PROMPT = """
You convert the text of one resume (extracted from a PDF, so spacing and line
breaks may be broken) into clean Markdown with this structure:

# Full Name
(one line: the resume's own contact details, separated by " · ")
## Section (Education, Experience, Projects, Skills, ...)
### Title — Organization
dates · location
- bullet

If "Links in the PDF" are listed at the end, write the contact line with
full URLs (e.g. https://www.linkedin.com/in/...) in place of their labels,
and keep paper links next to their publications.

Keep every fact and keep the wording exactly as written: fix only spacing,
broken line breaks and hyphenation, including words the extraction split
("RoBERT a" is "RoBERTa", "CPT -eligible" is "CPT-eligible"). Anything else on
a role's line stays in its heading, e.g. an advisor: "### Research Intern —
Example University, with Prof. Maria Lopez". Do not summarize,
rephrase, reorder within a section, or add anything. If a part is unreadable,
leave it out and say so in notes.
""".strip()

PARSE_SCHEMA = {
    "type": "object",
    "properties": {"markdown": {"type": "string"}, "notes": {"type": "string"}},
    "required": ["markdown", "notes"],
    "additionalProperties": False,
}


def folder() -> Path:
    return config.PROFILE_DIR / "resumes"


def parsed_folder() -> Path:
    return folder() / "parsed"


def proposal_paths() -> tuple[Path, Path]:
    return config.PROFILE_DIR / "resume.proposed.md", config.PROFILE_DIR / "resume.proposed.json"


# ----- PDF → text -----------------------------------------------------------

def extract_text(pdf: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(pdf))
    text = rejoin(plain("\n".join(page.extract_text() or "" for page in reader.pages)))
    # Hyperlink targets aren't in the text ("LinkedIn" is just a label), but
    # application forms need the real URLs.
    links = []
    for page in reader.pages:
        for annotation in page.get("/Annots") or []:
            action = annotation.get_object().get("/A") or {}
            uri = str(action.get("/URI") or "")
            if uri.startswith(("http", "mailto:")) and uri not in links:
                links.append(uri)
    if links:
        text += "\n\nLinks in the PDF:\n" + "\n".join(links)
    if len(text) < 80:
        raise ValueError(f"{pdf.name} has almost no text. It may be a scanned image; "
                         "export it from your editor as a text PDF instead.")
    return text


def plain(text: str) -> str:
    """Ligatures (ﬁ, ﬂ) to letters, icon glyphs dropped, spaces tidied."""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Co")  # private-use icons
    return re.sub(r"[ \t]+", " ", text).strip()


# Letters a PDF's kerning splits from the rest of their word ("T owards", "PyT orch", "Y eo").
KERNED = re.compile(r"(?<![A-Za-z&])((?:[A-Z][a-z]+)?[TVWYPF]) ([a-z]{2,})\b")
LONE = {"is", "in", "on", "or", "an", "as", "at", "to", "of", "and", "the", "for", "by", "it", "be", "cells"}


SECTION_WORDS = STRUCTURE | {"focus", "service", "teaching", "volunteer", "additional", "information",
                              "achievements", "involvement", "objective", "profile", "employment", "history",
                              "academic", "industry", "community", "extracurricular", "references", "honours"}


def section_case(name: str) -> str:
    """"SELECTED PUBLICATIONS" as "Selected Publications"."""
    return " ".join(w if w == "&" else w.lower() if i and w.lower() in {"and", "the", "of"} else w.capitalize()
                    for i, w in enumerate(name.split()))


def unspaced(letters: str) -> str:
    """"RESEARCHFOCUS" as "RESEARCH FOCUS": split into section words where possible."""
    lower, best = letters.lower(), {0: []}
    for end in range(1, len(lower) + 1):
        for start in range(end):
            if start in best and lower[start:end] in SECTION_WORDS | {"&"}:
                best[end] = best[start] + [letters[start:end]]
                break
    return " ".join(best[len(lower)]) if len(lower) in best else letters


def rejoin(text: str) -> str:
    """Undo what PDF text extraction breaks, in code (one model kept "T owards" and "PyT orch"
    whatever the prompt said): kerned splits, "RoBERT a", "CPT -eligible", "author ,", an email
    written as a web address."""
    text = re.sub(r"^(#+ +)?((?:[A-Z&] ){3,}[A-Z&])$", lambda m: (m.group(1) or "") + unspaced(m.group(2).replace(" ", "")),
                  text, flags=re.M)  # "R E S E A R C H F O C U S", a heading spaced out for looks.
    text = KERNED.sub(lambda m: m.group(0) if m.group(2) in LONE else m.group(1) + m.group(2), text)
    text = re.sub(r"([a-z][A-Z]{2,}) ([a-z])\b", r"\1\2", text)  # "RoBERT a" (not "BERT a model")
    text = re.sub(r"(?<=[a-z,])\n(?=[a-z])", " ", text)  # A sentence wrapped onto the next line.
    text = re.sub(r"\bcontact line \(([^()\n]*)\)", r"\1", text, flags=re.I)  # A prompt's placeholder, copied.
    text = re.sub(r"^(## +)([A-Z][A-Z &]+)$", lambda m: m.group(1) + section_case(m.group(2))
                  if set(m.group(2).lower().replace("&", " ").split()) <= SECTION_WORDS else m.group(0), text, flags=re.M)
    text = re.sub(r"(?<=[A-Za-z]) -(?=[a-z])", "-", text)
    text = re.sub(r"(?<=[\w)%+]) +(?=[,;.:)](?:\s|$))", "", text, flags=re.M)
    text = re.sub(r"\( +", "(", text)
    return re.sub(r"(?:https?://)?(?:mailto:)([^\s/·|]+@)|https?://(?=[^\s/·|]+@)", r"\1", text)


def save_upload(filename: str, data: bytes) -> Path:
    """Store an uploaded PDF under profile/resumes/ with a safe, unique name."""
    if not data.startswith(b"%PDF") or len(data) > MAX_PDF_BYTES:
        raise ValueError("Upload a PDF under 10 MB.")
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(filename).stem).strip("_")[:60] or "resume"
    folder().mkdir(parents=True, exist_ok=True)
    path, n = folder() / f"{stem}.pdf", 2
    while path.exists():
        path, n = folder() / f"{stem}_{n}.pdf", n + 1
    path.write_bytes(data)
    return path


# ----- checks ---------------------------------------------------------------

def latest_date(text: str, today: datetime | None = None) -> tuple[int, int]:
    """The latest past month-year a resume mentions: how recent its content is.

    Future dates are skipped: "Expected May 2027" appears on every version.
    """
    today = today or datetime.now()
    best = (0, 0)
    for month, year in DATE.findall(text):
        when = (int(year), MONTHS.get(month[:3].lower(), 0) if month else 0)
        if when <= (today.year, today.month):
            best = max(best, when)
    return best


def unsupported_words(markdown: str, sources: list[str]) -> list[str]:
    """Words in `markdown` that appear in none of the sources.

    Matching ignores case and spaces, since PDF extraction often drops spaces
    ("PythonandSQL"). Structure words (section names) are allowed.
    """
    haystack = re.sub(r"\s+", "", plain(" ".join(sources))).lower()
    markdown = plain(markdown)
    flagged = []
    for word in WORD.findall(markdown):
        lower = word.lower()
        if lower in STRUCTURE or lower in flagged or len(lower) < 3:
            continue
        if lower not in haystack:
            flagged.append(lower)
    return flagged


def dropped_words(markdown: str, source: str) -> list[str]:
    """Names, numbers and capitalized words in the PDF's text that the Markdown left out. One
    model dropped every advisor ("with Prof. Maria Lopez") from role headings."""
    body = source.split("\n\nLinks in the PDF:")[0]
    haystack = re.sub(r"[^a-z0-9]", "", plain(markdown).lower())
    missing = []
    for word in re.findall(r"[A-Za-z][A-Za-z0-9+#.\-]*[A-Za-z0-9+#]|\d[\d.,%+]*", body):
        key = re.sub(r"[^a-z0-9]", "", word.lower())
        if len(key) >= 3 and (word[0].isupper() or word[0].isdigit()) and key not in haystack \
                and key not in missing:
            missing.append(key)
    return missing


def restore(markdown: str, source: str, missing: list[str]) -> str:
    """Put back what a conversion left out of a line, from the PDF's own line: one model dropped
    "with Prof. Maria Lopez" from role headings however often it was asked to keep it."""
    from .merge import DATES

    key = lambda w: re.sub(r"[^a-z0-9]", "", w.lower())  # noqa: E731
    dated = lambda t: bool(DATES.fullmatch(t) or re.fullmatch(  # noqa: E731 - "Aug", "–", "Present" end a span.
        r"(?i)(jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|june?|july?|aug(ust)?|sep(t|tember)?|oct(ober)?|nov(ember)?"
        r"|dec(ember)?)\.?|[–—-]|present|current", t))
    lines = markdown.splitlines()
    for raw in source.split("\n\nLinks in the PDF:")[0].splitlines():
        tokens = raw.split()
        if not any(key(t) in missing for t in tokens):
            continue
        best, share = None, 0.0
        for i, line in enumerate(lines):
            mine = words(line)
            if len(mine) >= 2 and len(mine & words(raw)) / len(mine) > share:
                best, share = i, len(mine & words(raw)) / len(mine)
        if best is None or share < 0.6:
            continue
        have = re.sub(r"[^a-z0-9]", "", lines[best].lower())
        absent = [bool(key(t)) and key(t) not in have and not dated(t) for t in tokens]
        for start, token in enumerate(tokens):
            if key(token) not in missing or not absent[start]:
                continue
            end = start
            while start > 0 and absent[start - 1]:
                start -= 1
            while end + 1 < len(tokens) and absent[end + 1]:
                end += 1
            span = " ".join(tokens[start:end + 1]).strip(" ,;")
            before = tokens[start - 1].strip(" ,;") if start else ""
            at = lines[best].find(before) + len(before) if before and before in lines[best] else len(lines[best])
            lines[best] = lines[best][:at].rstrip(" ,;") + (", " if lines[best][:at].strip() else "") + span + \
                (" " + lines[best][at:].lstrip(" ,;") if lines[best][at:].strip(" ,;") else "")
            have = re.sub(r"[^a-z0-9]", "", lines[best].lower())
            absent = [bool(key(t)) and key(t) not in have and not dated(t) for t in tokens]
    return "\n".join(lines)


def problems(markdown: str, sources: list[str]) -> dict:
    found = {"invented_numbers": documents.unsupported_numbers(markdown, "\n".join(sources)),
             "unknown_words": unsupported_words(markdown, sources)}
    if len(sources) == 1:  # One PDF's conversion: nothing of it may go missing either.
        found["left_out"] = dropped_words(markdown, sources[0])
    return found


# ----- PDF → Markdown -------------------------------------------------------

def parse(llm, pdf: Path, log=print) -> dict:
    """Turn one PDF into Markdown, checked against the PDF's own text."""
    text = extract_text(pdf)
    log(f"  read {pdf.name}: {len(text)} characters")
    result = llm.json(PARSE_PROMPT, text[:30000], PARSE_SCHEMA, "resume_markdown")
    result["markdown"] = rejoin(result["markdown"])
    found = problems(result["markdown"], [text])
    for _ in range(2):  # One model dropped every advisor's name twice running, then kept them.
        if not any(found.values()):
            break
        log(f"  retrying: {found}")
        retry = (f"{text[:30000]}\n\n---\nYour previous Markdown didn't match the resume: "
                 + "; ".join(f"{k.replace('_', ' ')}: {', '.join(map(str, v))}" for k, v in found.items() if v)
                 + ". Use only the resume's own words and numbers, and leave none of them out.")
        again = llm.json(PARSE_PROMPT, retry, PARSE_SCHEMA, "resume_markdown")
        again["markdown"] = rejoin(again["markdown"])
        checked = problems(again["markdown"], [text])
        if sum(map(len, checked.values())) <= sum(map(len, found.values())):
            result, found = again, checked
    if found.get("left_out"):  # Whatever's still missing, put back from the PDF's own lines.
        result["markdown"] = restore(result["markdown"], text, found["left_out"])
        found = problems(result["markdown"], [text])
    if not re.search(r"unreadable|illegible|could ?n[o']t|missing|left out|unclear", result.get("notes") or "", re.I):
        result["notes"] = ""  # "Links were converted as requested": nothing for you to act on.
    parsed_folder().mkdir(parents=True, exist_ok=True)
    md_path = parsed_folder() / f"{pdf.stem}.md"
    md_path.write_text(result["markdown"].strip() + "\n", encoding="utf-8")
    year, month = latest_date(text)
    details = {"source": pdf.name, "parsed_at": datetime.now().isoformat(timespec="seconds"),
               "latest_date": f"{year}-{month:02d}" if year else None,
               "file_modified": datetime.fromtimestamp(pdf.stat().st_mtime).isoformat(timespec="seconds"),
               "notes": result["notes"], "checks": found}
    (parsed_folder() / f"{pdf.stem}.json").write_text(json.dumps(details, indent=1), encoding="utf-8")
    (parsed_folder() / f"{pdf.stem}.txt").write_text(text, encoding="utf-8")
    return details


def parsed() -> list[dict]:
    """Parsed resumes, oldest content first (ties: the shorter cut first, so the fuller is the base)."""
    items = []
    for meta in parsed_folder().glob("*.json"):
        details = json.loads(meta.read_text(encoding="utf-8"))
        details["markdown"] = meta.with_suffix(".md").read_text(encoding="utf-8")
        text = meta.with_suffix(".txt")
        details["text"] = text.read_text(encoding="utf-8") if text.exists() else details["markdown"]
        details["stem"] = meta.stem
        items.append(details)
    # Same content date (a 1-page and a 2-page cut of one resume): the fuller one is the base,
    # or the shorter cut's condensed lines "cover" the fuller ones and the details are lost.
    return sorted(items, key=lambda d: (d["latest_date"] or "", len(d["markdown"]), d["file_modified"]))


def remove(stem: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", stem):
        raise ValueError("Unknown resume.")
    for suffix in (".md", ".json", ".txt"):
        (parsed_folder() / f"{stem}{suffix}").unlink(missing_ok=True)
    (folder() / f"{stem}.pdf").unlink(missing_ok=True)


# ----- merge → proposal → master --------------------------------------------

def same_text(a: str, b: str) -> bool:
    squash = lambda t: re.sub(r"[^a-z0-9]+", "", t.lower())  # noqa: E731
    return squash(a) == squash(b)


def current_master() -> str | None:
    """The master resume, unless it's still the example from setup."""
    path = config.PROFILE_DIR / "resume.md"
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    example = config.PROFILE_EXAMPLE_DIR / "resume.md"
    if example.exists() and text.strip() == example.read_text(encoding="utf-8").strip():
        return None
    return text


# Structure for the merge (merge.py) and typesetting (latex.py): sections, roles, lines.

BULLET = re.compile(r"^\s*(?:[-*•▪◦]|\d+\.)\s+")


def words(line: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", plain(line).lower())) - {"and", "the", "of", "a", "in", "to", "for", "with", "on", "at"}


def dates_only(line: str) -> bool:
    """A line that's dates, maybe with a place ("January 2025 – May 2025 · New York")."""
    from .merge import DATES

    return bool(DATES.search(line)) and len(DATES.sub("", line).strip(" ·|,–—-")) < 30 and not BULLET.match(line)


def structure(markdown: str) -> dict:
    """{"title", "header": [lines], "sections": [{"name", "entries": [{"heading", "meta", "items"}]}]}

    A role is a "### " heading, or a plain line directly followed by bullets
    ("Software Engineer, Example Corp June 2025 – Aug 2025" then "- ...").
    """
    doc = {"title": "", "header": [], "sections": []}
    section = entry = None
    lines = [l.rstrip() for l in markdown.splitlines() if l.strip()]
    for index, line in enumerate(lines):
        following = lines[index + 1] if index + 1 < len(lines) else ""
        after = lines[index + 2] if index + 2 < len(lines) else ""
        if dates_only(following) and BULLET.match(after) and not dates_only(line):
            following = after  # "Role, Org" then "Jan 2025 – May 2025" then bullets: still a role.
        if line.startswith("# ") and not doc["title"]:
            doc["title"] = line
        elif line.startswith("## "):
            section = {"name": line[3:].strip(), "entries": []}
            doc["sections"].append(section)
            entry = None
        elif section is None:
            doc["header"].append(line)
        elif line.startswith("### ") or (not BULLET.match(line) and BULLET.match(following)
                                         and not (entry and entry["heading"] and not entry["items"])):
            plain_heading = not line.startswith("### ")
            entry = {"heading": line[4:].strip() if not plain_heading else line,
                     "plain": plain_heading, "meta": [], "items": []}
            section["entries"].append(entry)
        else:
            if entry is None:
                entry = {"heading": None, "meta": [], "items": []}
                section["entries"].append(entry)
            if entry["heading"] and not entry["items"] and not BULLET.match(line):
                entry["meta"].append(line)  # dates · location
            else:
                entry["items"].append(line)
    return doc


def render(doc: dict) -> str:
    out = [doc["title"], *doc["header"]]
    for section in doc["sections"]:
        out += ["", f"## {section['name']}"]
        for entry in section["entries"]:
            if entry["heading"]:
                out += [entry["heading"]] if entry.get("plain") else ["", f"### {entry['heading']}"]
            out += entry["meta"] + entry["items"]
    return "\n".join(out).strip() + "\n"


def merge_versions(versions: list[tuple[str, str]], llm=None, log=print) -> dict:
    """Merge (label, markdown) versions, oldest first (merge.py; rules in docs/merge-guide.md)."""
    from .merge import merge

    return merge(versions, llm, log)


def propose(include_master: bool = True, log=print, llm=None) -> dict:
    """Merge every parsed resume (and the current master) into a proposal for you."""
    versions = parsed()
    if not versions:
        raise ValueError("Upload at least one resume PDF first.")
    sources = []
    master = current_master() if include_master else None
    if master and any(same_text(master, v["markdown"]) for v in versions):
        master = None  # It's one of your uploads already (e.g. accepted from it).
    if master:
        # Your master may hold edits made by hand; it counts as the oldest version.
        sources.append(("your current master resume", master, master))
    sources += [(f"{v['source']} (content through {v['latest_date'] or 'unknown'})",
                 rejoin(v["markdown"]), rejoin(v["text"])) for v in versions]  # Also ones read before rejoin().
    log(f"  merging {len(sources)} version(s)")
    result = merge_versions([(label, md) for label, md, _ in sources], llm, log)
    raw = [text for _, _, text in sources] + [md for _, md, _ in sources]
    write_proposal(result["markdown"], {
        "created": datetime.now().isoformat(timespec="seconds"),
        "sources": [label for label, _, _ in sources],
        "changes": result["changes"], "kept": result["kept"], "combined": result.get("combined", []),
        "possible": result.get("possible", []),
        "checks": problems(result["markdown"], raw), "raw_sources": raw})
    return proposal()


def write_proposal(markdown: str, details: dict) -> None:
    md_path, meta_path = proposal_paths()
    md_path.write_text(markdown.strip() + "\n", encoding="utf-8")
    meta_path.write_text(json.dumps(details, indent=1), encoding="utf-8")


def edit_proposal(markdown: str) -> dict:
    """Your edits to the proposal; the checks run again on the edited text."""
    details = proposal()
    if details is None:
        raise ValueError("There's no proposed resume to edit.")
    details.pop("markdown")
    details["checks"] = problems(markdown, details.get("raw_sources", []))
    details["edited"] = True
    write_proposal(markdown, details)
    return proposal()


def proposal() -> dict | None:
    md_path, meta_path = proposal_paths()
    if not md_path.exists() or not meta_path.exists():
        return None
    details = json.loads(meta_path.read_text(encoding="utf-8"))
    details["markdown"] = md_path.read_text(encoding="utf-8")
    return details


def accept() -> Path | None:
    """Make the proposal the master. Returns where the old master was saved."""
    md_path, meta_path = proposal_paths()
    if not md_path.exists():
        raise ValueError("There's no proposed resume to accept.")
    master = config.PROFILE_DIR / "resume.md"
    backup = None
    if master.exists():
        history = config.PROFILE_DIR / "resume-history"
        history.mkdir(exist_ok=True)
        backup = history / f"resume-{datetime.now():%Y%m%d-%H%M%S}.md"
        shutil.copy(master, backup)
    shutil.move(str(md_path), master)
    meta_path.unlink(missing_ok=True)
    return backup


def save_master(markdown: str) -> Path | None:
    """Your own edit of the master resume. Returns where the previous master was saved."""
    text = markdown.replace("\r\n", "\n").strip()
    if not text:
        raise ValueError("The resume is empty; nothing was saved.")
    master = config.PROFILE_DIR / "resume.md"
    backup = None
    if master.exists():
        if master.read_text(encoding="utf-8").strip() == text:
            raise ValueError("No changes to save.")
        history = config.PROFILE_DIR / "resume-history"
        history.mkdir(exist_ok=True)
        backup = history / f"resume-{datetime.now():%Y%m%d-%H%M%S}.md"
        shutil.copy(master, backup)
    master.parent.mkdir(parents=True, exist_ok=True)
    master.write_text(text + "\n", encoding="utf-8")
    return backup


def discard() -> None:
    for path in proposal_paths():
        path.unlink(missing_ok=True)
