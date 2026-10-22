"""Resumes and cover letters typeset with LaTeX in your own resume style.

The style is a .tex preamble with these macros (your one-page and two-page
templates): \\section, \\expSubheading, \\resumeSubheading,
\\resumeSubHeadingListStart/End, \\resumeItemListStart/End. Only the preamble is
used; the body is written here from a structured resume:

    {"header_note": "...", "sections": [{"title", "kind", "text", "entries",
     "items", "skills"}]}

kind is paragraph | entries | education | list | skills, and sections can come
in any order. Text may use **bold**, *italic* and [label](url).
Compiled with Tectonic (brew install tectonic).
"""

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from . import config

TEMPLATES = Path(__file__).resolve().parent / "templates"
SPECIAL = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
           "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}
INLINE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)|\*\*(.+?)\*\*|(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)")


class Unavailable(RuntimeError):
    """No LaTeX engine installed."""


def available() -> bool:
    return shutil.which("tectonic") is not None


def preamble(pages: int = 1) -> str:
    name = "two-page.tex" if pages == 2 else "one-page.tex"
    for folder in (config.PROFILE_DIR / "resume-templates", TEMPLATES):
        path = folder / name
        if path.exists():
            text = path.read_text(encoding="utf-8")
            return text.split("\\begin{document}")[0].rstrip()
    raise FileNotFoundError(f"No resume template {name}.")


# Characters models produce that the resume font can't print (they'd vanish).
LOOKALIKES = {"\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2212": "-", "\u00ad": "-",
              "\u00a0": " ", "\u202f": " ", "\u2009": " ", "\u200b": "", "\u2060": ""}
SYMBOLS = {"–": "--", "—": "---", "•": r"\textbullet{}", "·": r"\textperiodcentered{}", "…": r"\ldots{}", "→": r"$\rightarrow$",
           "≥": r"$\geq$", "≤": r"$\leq$", "×": r"$\times$", "~": r"\textasciitilde{}"}


def escape(text: str) -> str:
    text = "".join(LOOKALIKES.get(c, c) for c in text)
    # f{}i: no "ﬁ"/"ﬂ"/"ﬀ" ligature glyphs, which resume parsers read as one odd character
    # ("classiﬁcation" no longer matches a search for "classification").
    return re.sub(r"f(?=[fil])", "f{}", "".join(SYMBOLS.get(c) or SPECIAL.get(c, c) for c in text))


def inline(text: str) -> str:
    """Markdown-style emphasis and links to LaTeX; everything else escaped."""
    out, last = [], 0
    for match in INLINE.finditer(text or ""):
        out.append(escape(text[last:match.start()]))
        label, url, bold, italic = match.groups()
        if url:
            out.append(rf"\href{{{url.replace('%', chr(92) + '%').replace('#', chr(92) + '#')}}}{{{inline(label)}}}")
        elif bold:
            out.append(rf"\textbf{{{inline(bold)}}}")
        else:
            out.append(rf"\textit{{{inline(italic)}}}")
        last = match.end()
    out.append(escape(text[last:] if text else ""))
    return "".join(out)


def header(contact: dict, note: str = "") -> str:
    parts = []
    if contact.get("email"):
        parts.append(rf"\jaQuiet{{\faEnvelope}}\ \href{{mailto:{contact['email']}}}{{{escape(contact['email'])}}}")
    if contact.get("phone"):
        parts.append(rf"\jaQuiet{{\faPhone}}\ {escape(contact['phone'])}")
    if contact.get("linkedin"):
        handle = [p for p in urlsplit(contact["linkedin"]).path.split("/") if p][-1:] or ["LinkedIn"]
        parts.append(rf"\jaQuiet{{\faLinkedin}}\ \href{{{contact['linkedin']}}}{{{escape(handle[0])}}}")
    if contact.get("scholar"):
        parts.append(rf"\jaQuiet{{\aiGoogleScholar}}\ \href{{{contact['scholar']}}}{{Scholar}}")
    if contact.get("github"):
        handle = [p for p in urlsplit(contact["github"]).path.split("/") if p][:1] or ["GitHub"]
        parts.append(rf"\jaQuiet{{\faGithub}}\ \href{{{contact['github']}}}{{{escape(handle[0])}}}")
    if contact.get("website"):
        site = urlsplit(contact["website"]).netloc or contact["website"]
        parts.append(rf"\jaQuiet{{\faGlobe}}\ \href{{{contact['website']}}}{{{escape(site.removeprefix('www.'))}}}")
    lines = [r"\begin{center}", r"\thispagestyle{empty}",
             rf"{{\Huge\bfseries\MakeUppercase{{{escape(contact.get('name', ''))}}}}}\\[5pt]",
             r"\small " + " ~|~\n".join(parts) + (r"\\[4pt]" if note else "")]
    if note:
        lines.append(rf"\small\textit{{{inline(note)}}}")
    lines.append(r"\end{center}")
    return "\n".join(lines)


def bold_name(text: str, name: str) -> str:
    """Your name in a publication's author list, bolded as in your template."""
    surname = (name.split() or [""])[-1]
    if not surname or "**" in text:
        return text
    pattern = re.compile(rf"\b((?:[A-Z]\.\s*)+{re.escape(surname)}|{re.escape(name)})\b", re.I)
    return pattern.sub(lambda m: f"**{m.group(1)}**", text, count=1)


MAX_SKILL_LINES = 5


def unique_skills(skills: list[dict]) -> list[dict]:
    """Drop skills already listed on an earlier line, and lines left nearly empty."""
    seen, out = set(), []
    for line in skills:
        items = [i.strip() for i in re.split(r",(?![^()]*\))", line["items"]) if i.strip()]
        fresh = [i for i in items if i.lower().rstrip(".") not in seen]
        seen |= {i.lower().rstrip(".") for i in items}
        if len(fresh) >= 2 or (fresh and len(items) == 1):
            out.append({"label": line["label"], "items": ", ".join(fresh).rstrip(".") + "."})
    return out[:MAX_SKILL_LINES]


def section(sec: dict, name: str = "") -> str:
    title, kind = escape(sec["title"]).replace("f{}", "f"), sec["kind"]  # Uppercased: no ligatures.
    out = [rf"\section{{{title}}}"]
    if kind == "paragraph":
        out.append(r"\small " + inline(sec.get("text", "")))
    elif kind == "entries":
        out.append(r"\resumeSubHeadingListStart")
        for i, entry in enumerate(sec.get("entries", [])):
            lead = r"\vspace{4pt}" if i else ""
            if entry.get("organization"):
                out.append(rf"{lead}\expSubheading{{{inline(entry['title'])}}}{{}}"
                           rf"{{{inline(entry['organization'])}}}{{{inline(entry.get('dates', ''))}}}")
            else:  # A project: no ", <organization>" after the title.
                out.append(rf"{lead}\item \textbf{{{inline(entry['title'])}}} \hfill \textit{{{inline(entry.get('dates', ''))}}}")
            if entry.get("bullets"):
                out.append(r"\resumeItemListStart")
                out += [rf"\item\small{{{inline(b)}}}" for b in entry["bullets"]]
                out.append(r"\resumeItemListEnd")
        out.append(r"\resumeSubHeadingListEnd")
    elif kind == "education":
        out.append(r"\resumeSubHeadingListStart")
        for entry in sec.get("entries", []):
            out.append(rf"\resumeSubheading{{{inline(entry['organization'] or entry['title'])}}}"
                       rf"{{{inline(entry.get('location', ''))}}}"
                       rf"{{{inline(entry['title'] if entry.get('organization') else '')}}}"
                       rf"{{{inline(entry.get('dates', ''))}}}")
            if entry.get("bullets"):  # GPA, honors, thesis, coursework
                out += [r"\resumeItemListStart", *[rf"\item\small{{{inline(b)}}}" for b in entry["bullets"]],
                        r"\resumeItemListEnd"]
        out.append(r"\resumeSubHeadingListEnd")
    elif kind == "list":
        style = "[leftmargin=1em, labelsep=0.4em]" if "publication" in sec["title"].lower() else "[leftmargin=*]"
        out.append(rf"\begin{{itemize}}{style}")
        publications = "publication" in sec["title"].lower()
        out += [rf"\item\small{{{inline(bold_name(item, name) if publications else item)}}}"
                for item in sec.get("items", [])]
        out.append(r"\end{itemize}")
    elif kind == "skills":
        lines = [rf"\textbf{{{escape(s['label'])}:}} {inline(s['items'])}"
                 for s in unique_skills(sec.get("skills", []))]
        out.append("\\\\\n".join(lines))
    return "\n".join(out)


# Added after any template's preamble so the PDF's text layer reads cleanly in resume
# parsers (tested with pdftotext, pdfminer and pypdf): letter-spaced headings keep their
# look with tighter spacing (wide gaps extract as "E D U C A T I O N"), and icons are
# marked as having no text where the reader supports it.
CLEAN_TEXT = r"""
\usepackage{accsupp}
\makeatletter\@ifpackageloaded{soul}{\sodef\so{}{.08em}{.5em plus .1em minus .05em}{.3em}}{}\makeatother
\newcommand{\jaQuiet}[1]{\BeginAccSupp{method=pdfstringdef,ActualText={}}#1\EndAccSupp{}}
"""


def resume_tex(doc: dict, contact: dict, pages: int = 1) -> str:
    body = "\n\n".join(section(s, contact.get("name", "")) for s in doc["sections"] if s_has_content(s))
    return (preamble(pages) + CLEAN_TEXT + "\n\\begin{document}\n\n" + header(contact, doc.get("header_note", ""))
            + "\n\n" + body + "\n\n\\end{document}\n")


def letter_tex(text: str, contact: dict) -> str:
    """A cover letter in the same style: your header, then the letter's paragraphs."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    body = "\n\n".join(r"\noindent " + inline(" ".join(p.splitlines())) for p in paragraphs)
    return (preamble(1) + CLEAN_TEXT + "\n\\begin{document}\n\n" + header(contact) + "\n\n\\vspace{10pt}\n"
            r"\setlength{\parskip}{8pt}" + "\n" + body + "\n\n\\end{document}\n")


def s_has_content(sec: dict) -> bool:
    return bool(sec.get("text") or sec.get("entries") or sec.get("items") or sec.get("skills"))


def compile_pdf(tex: str, output: Path) -> int:
    """Typeset to `output`; returns the page count."""
    if not available():
        raise Unavailable("Install a LaTeX engine for resumes in your style: brew install tectonic")
    with tempfile.TemporaryDirectory() as work:
        source = Path(work) / "resume.tex"
        source.write_text(tex, encoding="utf-8")
        command, env = ["tectonic", "-X", "compile", "--outdir", work, str(source)], None
        if config.HOSTED:  # Packages come through the app's local mirror (see netrelay).
            from . import netrelay

            command += ["--bundle", netrelay.bundle_url()]
            env = {k: v for k, v in os.environ.items() if k.lower() not in ("http_proxy", "https_proxy")}
        run = subprocess.run(command, capture_output=True, text=True, timeout=300, env=env)
        if run.returncode != 0:
            errors = [l for l in run.stderr.splitlines() if l.startswith("error")]
            raise RuntimeError("LaTeX failed: " + " | ".join(errors[:3] or run.stderr.splitlines()[-6:]))
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(Path(work) / "resume.pdf", output)
    from pypdf import PdfReader

    return len(PdfReader(str(output)).pages)


def plain(doc: dict) -> str:
    """The resume as Markdown (for checks, search and the dashboard)."""
    out = []
    if doc.get("header_note"):
        out.append(doc["header_note"])
    for sec in doc["sections"]:
        out.append(f"\n## {sec['title']}")
        if sec.get("text"):
            out.append(sec["text"])
        for entry in sec.get("entries", []):
            out.append(f"### {entry['title']}" + (f" — {entry['organization']}" if entry.get("organization") else ""))
            meta = " · ".join(x for x in (entry.get("dates"), entry.get("location")) if x)
            if meta:
                out.append(meta)
            out += [f"- {b}" for b in entry.get("bullets", [])]
        out += [f"- {i}" for i in sec.get("items", [])]
        out += [f"**{s['label']}:** {s['items']}" for s in sec.get("skills", [])]
    return "\n".join(out).strip() + "\n"


def from_markdown(markdown: str) -> dict:
    """Your master resume (Markdown) as structured sections, unchanged in content."""
    from .resumes import BULLET, structure

    doc = structure(markdown)
    note = next((line for line in doc["header"] if re.search(r"authori[sz]ation|visa|citizen", line, re.I)), "")
    sections = []
    for sec in doc["sections"]:
        entries = [e for e in sec["entries"] if e["heading"]]
        loose = [i for e in sec["entries"] if not e["heading"] for i in e["items"]]
        if "education" in sec["name"].lower() and not entries and loose:
            sections.append({"title": sec["name"].title(), "kind": "education", "text": "",
                             "entries": education_entries(loose), "items": [], "skills": []})
            continue
        if entries:
            parsed = []
            for e in entries:
                title, org, dates = split_heading(e["heading"])
                meta = " · ".join(e["meta"]).split(" · ")  # "dates · location"
                parsed.append({"title": title, "organization": org,
                               "dates": dates or meta[0], "location": meta[1] if len(meta) > 1 else "",
                               "bullets": [BULLET.sub("", b) for b in e["items"]]})
            sections.append({"title": sec["name"].title(), "kind": "entries", "text": "", "entries": parsed,
                             "items": [BULLET.sub("", i) for i in loose], "skills": []})
        elif loose and all(BULLET.match(i) for i in loose):
            sections.append({"title": sec["name"].title(), "kind": "list", "text": "", "entries": [],
                             "items": [BULLET.sub("", i) for i in loose], "skills": []})
        elif loose and all(re.match(r"^\**[^:]{2,40}:\**", i) for i in loose):
            skills = [{"label": i.split(":", 1)[0].strip("* "), "items": i.split(":", 1)[1].strip(" *")} for i in loose]
            sections.append({"title": sec["name"].title(), "kind": "skills", "text": "", "entries": [],
                             "items": [], "skills": skills})
        else:
            sections.append({"title": sec["name"].title(), "kind": "paragraph", "text": " ".join(loose),
                             "entries": [], "items": [], "skills": []})
    return {"header_note": note, "sections": sections, "matched": [], "emphasized": [], "gaps": []}


MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
DATES = re.compile(rf"\s*((?:{MONTH}\s+)?(?:19|20)\d{{2}}\s*(?:[–—-]+\s*(?:(?:{MONTH}\s+)?(?:19|20)\d{{2}}|Present|present|Now))?)\s*$")


def split_heading(heading: str) -> tuple[str, str, str]:
    """"AI Engineer, Acme July 2024 – June 2026" → (title, organization, dates)."""
    dates = ""
    match = DATES.search(heading)
    if match:
        dates, heading = match.group(1).strip(), heading[:match.start()].strip()
    for sep in (" — ", " – ", " | ", ", "):
        if sep in heading:
            title, org = heading.split(sep, 1)
            return title.strip(), org.strip(), dates
    return heading.strip(), "", dates


def education_entries(lines: list[str]) -> list[dict]:
    """Pairs of lines: "School, City, Country" then "Degree. Date"."""
    out = []
    for i in range(0, len(lines) - 1, 2):
        school, degree = lines[i].strip(), lines[i + 1].strip()
        parts = [p.strip() for p in school.split(",")]
        location = ", ".join(parts[-2:]) if len(parts) > 2 else (parts[1] if len(parts) == 2 else "")
        name = ", ".join(parts[:-2]) if len(parts) > 2 else parts[0]
        match = DATES.search(degree)
        dates = match.group(1).strip() if match else ""
        degree = degree[:match.start()].strip() if match else degree
        out.append({"title": degree, "organization": name, "dates": dates, "location": location,
                    "bullets": []})
    if len(lines) % 2:
        out.append({"title": lines[-1].strip(), "organization": "", "dates": "", "location": "", "bullets": []})
    return out


def page_fill(pdf: Path) -> float:
    """How far down the last page the text reaches (0-1); a full page is about 0.95."""
    from pypdf import PdfReader

    page = PdfReader(str(pdf)).pages[-1]
    height = float(page.mediabox.height)
    lowest = []

    def visit(text, cm, tm, font, size):
        if text.strip():
            lowest.append(cm[5] + tm[5] * cm[3] if cm[3] else tm[5])

    page.extract_text(visitor_text=visit)
    return round((height - min(lowest)) / height, 3) if lowest else 0.0


def trim_once(doc: dict) -> str | None:
    """Remove the least relevant line (items are ordered most relevant first).
    Returns what was removed, or None when nothing sensible is left to cut."""
    sections = doc["sections"]

    def longest_entry(keep):
        found = [(len(e["bullets"]), i, j) for i, s_ in enumerate(sections) if s_["kind"] == "entries"
                 for j, e in enumerate(s_["entries"]) if len(e["bullets"]) > keep]
        return max(found) if found else None

    def longest_list(keep):
        found = [(len(s_["items"]), i) for i, s_ in enumerate(sections)
                 if s_["kind"] == "list" and len(s_["items"]) > keep]
        return max(found) if found else None

    def skills_beyond(keep):
        return next((s_ for s_ in reversed(sections) if s_["kind"] == "skills" and len(s_["skills"]) > keep), None)

    steps = [
        lambda: (lambda hit: hit and sections[hit[1]]["entries"][hit[2]]["bullets"].pop())(longest_entry(2)),
        lambda: (lambda sec: sec and sec["skills"].pop()["label"])(skills_beyond(3)),
        lambda: (lambda hit: hit and sections[hit[1]]["items"].pop())(longest_list(2)),
        lambda: (lambda hit: hit and sections[hit[1]]["entries"][hit[2]]["bullets"].pop())(longest_entry(1)),
        lambda: (lambda hit: hit and sections[hit[1]]["items"].pop())(longest_list(1)),
        lambda: (lambda sec: sec and sec["skills"].pop()["label"])(skills_beyond(2)),
    ]
    for step in steps:
        cut = step()
        if cut:
            return cut
    groups = [(len(s_["entries"]), i) for i, s_ in enumerate(sections) if s_["kind"] == "entries" and len(s_["entries"]) > 1]
    if groups:
        _, i = max(groups)
        return sections[i]["entries"].pop()["title"]
    return None


def fit(doc: dict, contact: dict, pages: int, output: Path, limit: int = 40) -> tuple[dict, int, list[str]]:
    """Typeset, then cut the least relevant lines until it fits the page limit."""
    import copy

    doc = copy.deepcopy(doc)
    for sec in doc["sections"]:  # Fit what will actually print.
        if sec["kind"] == "skills":
            sec["skills"] = unique_skills(sec["skills"])
    removed = []
    count = compile_pdf(resume_tex(doc, contact, pages), output)
    while count > pages and len(removed) < limit:
        cut = trim_once(doc)
        if cut is None:
            break
        removed.append(cut if isinstance(cut, str) else str(cut))
        count = compile_pdf(resume_tex(doc, contact, pages), output)
    return doc, count, removed


def ensure_education(doc: dict, master: str) -> dict:
    """A tailored resume always keeps your education: if it was left out, put the
    master's Education section back (before Skills, as in your one-page style)."""
    present = [sec for sec in doc["sections"]
               if sec["kind"] == "education" or "education" in sec["title"].lower()]
    if any(s_has_content(sec) for sec in present):
        return doc
    doc = {**doc, "sections": [sec for sec in doc["sections"] if sec not in present]}  # empty ones
    education = next((sec for sec in from_markdown(master)["sections"] if sec["kind"] == "education"), None)
    if education is None:
        return doc
    sections = list(doc["sections"])
    skills = next((i for i, sec in enumerate(sections) if sec["kind"] == "skills"), len(sections))
    sections.insert(skills, education)
    return {**doc, "sections": sections}
