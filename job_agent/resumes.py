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
contact line (location · email · phone · links, separated by " · ")
## Section (Education, Experience, Projects, Skills, ...)
### Title — Organization
dates · location
- bullet

If "Links in the PDF" are listed at the end, write the contact line with
full URLs (e.g. https://www.linkedin.com/in/...) in place of their labels,
and keep paper links next to their publications.

Keep every fact and keep the wording exactly as written: fix only spacing,
broken line breaks and hyphenation. Do not summarize, rephrase, reorder
within a section, or add anything. If a part is unreadable, leave it out and
say so in notes.
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
    text = plain("\n".join(page.extract_text() or "" for page in reader.pages))
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


def problems(markdown: str, sources: list[str]) -> dict:
    return {"invented_numbers": documents.unsupported_numbers(markdown, "\n".join(sources)),
            "unknown_words": unsupported_words(markdown, sources)}


# ----- PDF → Markdown -------------------------------------------------------

def parse(llm, pdf: Path, log=print) -> dict:
    """Turn one PDF into Markdown, checked against the PDF's own text."""
    text = extract_text(pdf)
    log(f"  read {pdf.name}: {len(text)} characters")
    result = llm.json(PARSE_PROMPT, text[:30000], PARSE_SCHEMA, "resume_markdown")
    found = problems(result["markdown"], [text])
    if found["invented_numbers"] or found["unknown_words"]:
        log(f"  retrying: {found}")
        retry = (f"{text[:30000]}\n\n---\nYour previous Markdown contained text that isn't in "
                 f"the resume: {found}. Use only the resume's own words and numbers.")
        result = llm.json(PARSE_PROMPT, retry, PARSE_SCHEMA, "resume_markdown")
        found = problems(result["markdown"], [text])
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
    """Parsed resumes, oldest content first (ties: older file first)."""
    items = []
    for meta in parsed_folder().glob("*.json"):
        details = json.loads(meta.read_text(encoding="utf-8"))
        details["markdown"] = meta.with_suffix(".md").read_text(encoding="utf-8")
        text = meta.with_suffix(".txt")
        details["text"] = text.read_text(encoding="utf-8") if text.exists() else details["markdown"]
        details["stem"] = meta.stem
        items.append(details)
    return sorted(items, key=lambda d: (d["latest_date"] or "", d["file_modified"]))


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


# The merge is code, not a model: it only copies your own lines, so it can't
# invent anything, and it costs no model calls. The newest version is the base;
# an older line is added only when nothing in the base already covers it.

BULLET = re.compile(r"^\s*(?:[-*•▪◦]|\d+\.)\s+")
COVERED = 0.5  # Share of the shorter line's words found in another line.


def words(line: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", plain(line).lower())) - {"and", "the", "of", "a", "in", "to", "for", "with", "on", "at"}


def overlap(a: str, b: str) -> float:
    wa, wb = words(a), words(b)
    if not wa or not wb:
        return 0.0
    if min(len(wa), len(wb)) < 3:  # Short lines: covered only if every word is there.
        small, large = sorted((wa, wb), key=len)
        return 1.0 if small <= large else 0.0
    return len(wa & wb) / min(len(wa), len(wb))


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


def merge_versions(versions: list[tuple[str, str]]) -> dict:
    """Merge (label, markdown) versions, oldest first. Newest wins; older lines fill gaps."""
    newest_label, newest = versions[-1]
    base = structure(newest)
    changes, kept = [f"Base: {newest_label} (newest)."], []

    def base_lines():
        for section in base["sections"]:
            for entry in section["entries"]:
                yield from entry["items"]
                if entry["heading"]:
                    yield entry["heading"]

    def covered(line: str) -> bool:
        return any(overlap(line, other) >= COVERED for other in base_lines())

    def find_section(name: str):
        return next((s for s in base["sections"] if words(s["name"]) == words(name)), None)

    def find_entry(heading: str):
        return next((e for s in base["sections"] for e in s["entries"]
                     if e["heading"] and overlap(heading, e["heading"]) >= COVERED), None)

    for label, markdown in reversed(versions[:-1]):  # next-newest first
        for section in structure(markdown)["sections"]:
            for entry in section["entries"]:
                missing = [item for item in entry["items"] if not covered(item)]
                match = find_entry(entry["heading"]) if entry["heading"] else None
                if entry["heading"] and match is None and (missing or not entry["items"]) \
                        and not covered(entry["heading"]):
                    target = find_section(section["name"])
                    if target is None:
                        target = {"name": section["name"], "entries": []}
                        base["sections"].append(target)
                        changes.append(f"Added section “{section['name']}” from {label}.")
                    target["entries"].append({**entry, "items": missing or entry["items"]})
                    changes.append(f"Added “{entry['heading']}” from {label}.")
                    kept += [(label, entry["heading"])]
                    continue
                if not missing:
                    continue
                if match is not None:
                    match["items"] += missing
                else:
                    target = find_section(section["name"])
                    if target is None:
                        target = {"name": section["name"], "entries": []}
                        base["sections"].append(target)
                        changes.append(f"Added section “{section['name']}” from {label}.")
                    loose = next((e for e in target["entries"] if not e["heading"]), None)
                    if loose is None:
                        loose = {"heading": None, "meta": [], "items": []}
                        target["entries"].append(loose)
                    loose["items"] += missing
                kept += [(label, item) for item in missing]
    if len(changes) == 1 and len(versions) > 1:
        changes.append("Everything in the older versions is already in the newest one.")
    return {"markdown": render(base), "changes": changes,
            "kept": [{"from": label, "line": line} for label, line in kept]}


def propose(include_master: bool = True, log=print) -> dict:
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
                 v["markdown"], v["text"]) for v in versions]
    log(f"  merging {len(sources)} version(s)")
    result = merge_versions([(label, md) for label, md, _ in sources])
    raw = [text for _, _, text in sources] + [md for _, md, _ in sources]
    write_proposal(result["markdown"], {
        "created": datetime.now().isoformat(timespec="seconds"),
        "sources": [label for label, _, _ in sources],
        "changes": result["changes"], "kept": result["kept"],
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


def discard() -> None:
    for path in proposal_paths():
        path.unlink(missing_ok=True)
