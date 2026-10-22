"""Tailored resumes and cover letters, written as Markdown and printed to PDF."""

import re
from pathlib import Path

CSS = """
body { font: 10.5pt/1.38 Georgia, 'Times New Roman', serif; color: #111; margin: 0; }
h1 { font-size: 20pt; margin: 0 0 2pt; letter-spacing: .5pt; }
h2 { font-size: 11pt; text-transform: uppercase; letter-spacing: 1pt;
     border-bottom: 1px solid #999; margin: 12pt 0 4pt; padding-bottom: 2pt; }
h3 { font-size: 10.5pt; margin: 7pt 0 1pt; }
p { margin: 2pt 0 5pt; }
ul { margin: 2pt 0 4pt 14pt; padding: 0; }
li { margin: 1pt 0; }
a { color: inherit; text-decoration: none; }
"""

# Numbers that look like facts: years, percentages, money, counts with units.
FACT = re.compile(r"\$?\d[\d,.]*\s?(%|k\b|m\b|x\b|\+)?", re.I)


def unsupported_numbers(tailored: str, master: str) -> list[str]:
    """Numbers in the tailored text that the master resume never states.

    Tailoring may reorder, cut and rephrase. It may not invent metrics or
    dates, which is the most common way a rewritten resume becomes untrue.
    """
    known = {_norm(m.group()) for m in FACT.finditer(master)}
    found = []
    for match in FACT.finditer(tailored):
        value = _norm(match.group())
        if len(value.strip("$%kmx+")) < 2:
            continue  # Single digits are list markers and headings, not claims.
        if value not in known and match.group().strip() not in found:
            found.append(match.group().strip())
    return found


def rewritten(doc: dict, master: str, floor: float = 0.75) -> list[tuple[str, str]]:
    """Bullets whose words are mostly not in any line of the master resume: (bullet, closest line).

    Tailoring should keep your sentences: reordering, cutting and using the posting's name
    for a thing are fine. A bullet built from new words is where an AI tone or a quietly
    raised claim creeps in, so these are shown for review."""
    plain = lambda t: set(re.sub(r"\*\*|[^a-z0-9% ]+", " ", t.lower()).split()) - STOP  # noqa: E731
    lines = [(plain(l), l.strip("-*• ").strip()) for l in master.splitlines() if len(l.split()) >= 4]
    found = []
    for section in doc.get("sections", []):
        for entry in section.get("entries", []):
            for bullet in entry.get("bullets", []):
                words = plain(bullet)
                if len(words) < 4 or not lines:
                    continue
                share, best = max((len(words & line) / len(words), text) for line, text in lines)
                if share < floor:
                    found.append((bullet, best))
    return found


STOP = {"a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "with", "by", "at", "from", "as", "that",
        "it", "its", "is", "was", "were", "be", "this", "so", "into", "using", "used"}


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text.lower().replace(",", "")).rstrip(".")


def to_html(body_markdown: str, title: str) -> str:
    import markdown

    body = markdown.markdown(body_markdown, extensions=["sane_lists"])
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><title>{title}</title>"
        f"<style>{CSS}</style></head><body>{body}</body></html>"
    )


def save(printer, job_dir: Path, stem: str, body_markdown: str, title: str) -> dict:
    """Write Markdown and PDF. `printer` is a Browser or Headless."""
    job_dir.mkdir(parents=True, exist_ok=True)
    md_path = job_dir / f"{stem}.md"
    pdf_path = job_dir / f"{stem}.pdf"
    md_path.write_text(body_markdown, encoding="utf-8")
    pages = printer.render_pdf(to_html(body_markdown, title), pdf_path)
    return {"markdown": str(md_path), "pdf": str(pdf_path), "pages": pages}


def save_resume(printer, folder: Path, stem: str, doc: dict, contact: dict, pages: int,
                title: str) -> dict:
    """A structured resume typeset in your LaTeX style (or, without LaTeX, as HTML).
    Returns the files, page count and, under "doc", the resume as printed."""
    from . import latex

    folder.mkdir(parents=True, exist_ok=True)
    markdown = f"# {contact.get('name', '')}\n{contact_line(contact)}\n\n" + latex.plain(doc)
    md_path, pdf_path = folder / f"{stem}.md", folder / f"{stem}.pdf"
    md_path.write_text(markdown, encoding="utf-8")
    if not latex.available():
        return {**save(printer, folder, stem, markdown, title), "style": "plain (no LaTeX)", "doc": doc}
    doc, count, removed = latex.fit(doc, contact, pages, pdf_path)
    markdown = f"# {contact.get('name', '')}\n{contact_line(contact)}\n\n" + latex.plain(doc)
    md_path.write_text(markdown, encoding="utf-8")
    (folder / f"{stem}.tex").write_text(latex.resume_tex(doc, contact, pages), encoding="utf-8")
    return {"markdown": str(md_path), "pdf": str(pdf_path), "tex": str(folder / f"{stem}.tex"),
            "pages": count, "fill": latex.page_fill(pdf_path), "style": "latex",
            "trimmed": removed, "doc": doc}  # doc: what was printed, after fitting (not kept in the packet's documents).


def save_letter(printer, folder: Path, stem: str, text: str, contact: dict, title: str) -> dict:
    from . import latex

    if not latex.available():
        return save(printer, folder, stem, text, title)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{stem}.md").write_text(text, encoding="utf-8")
    tex = latex.letter_tex(text, contact)
    (folder / f"{stem}.tex").write_text(tex, encoding="utf-8")
    count = latex.compile_pdf(tex, folder / f"{stem}.pdf")
    return {"markdown": str(folder / f"{stem}.md"), "pdf": str(folder / f"{stem}.pdf"), "pages": count}


def contact_line(contact: dict) -> str:
    return " · ".join(contact[k] for k in ("location", "email", "phone", "linkedin", "github", "website")
                      if contact.get(k))
