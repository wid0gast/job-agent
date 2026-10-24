"""Resumes typeset in the LaTeX style: escaping, sections, fallback, page count."""

import pytest

from job_agent import latex

MASTER = """# Jane Doe
New York, NY · jane@example.com
Work Authorization: F-1 student (CPT).

## Experience
Software Engineer, Example Corp June 2025 – Aug 2025
- Built a **search** service used by 40 engineers & cut costs 12%.

## Education
New York University, New York, USA
Master of Science, Statistics. May 2027

## Skills
**Languages:** Python, SQL, C++
"""


def test_inline_escapes_and_formats():
    out = latex.inline("**97% precision** & [paper](https://x.org/a#b) for C# $5 a_b")
    assert out == r"\textbf{97\% precision} \& \href{https://x.org/a\#b}{paper} for C\# \$5 a\_b"


def test_master_markdown_becomes_sections_in_order():
    doc = latex.from_markdown(MASTER)
    assert [s["kind"] for s in doc["sections"]] == ["entries", "education", "skills"]
    role = doc["sections"][0]["entries"][0]
    assert (role["title"], role["organization"], role["dates"]) == ("Software Engineer", "Example Corp",
                                                                    "June 2025 – Aug 2025")
    school = doc["sections"][1]["entries"][0]
    assert (school["organization"], school["location"], school["dates"]) == (
        "New York University", "New York, USA", "May 2027")
    assert doc["header_note"].startswith("Work Authorization")
    tex = latex.resume_tex(doc, {"name": "Jane Doe", "email": "jane@example.com",
                                 "github": "https://github.com/jane"}, 1)
    assert r"\expSubheading{Software Engineer}{}{Example Corp}{June 2025 -- Aug 2025}" in tex
    assert r"\resumeSubheading{New York University}{New York, USA}" in tex
    assert r"\textbf{Languages:} Python, SQL, C++" in tex
    assert r"\jaQuiet{\faGithub}\ \href{https://github.com/jane}{jane}" in tex


@pytest.mark.skipif(not latex.available(), reason="no LaTeX engine")
def test_compiles_to_one_page(tmp_path):
    doc = latex.from_markdown(MASTER)
    pages = latex.compile_pdf(latex.resume_tex(doc, {"name": "Jane Doe", "email": "j@x.org"}, 1),
                              tmp_path / "r.pdf")
    assert pages == 1 and (tmp_path / "r.pdf").stat().st_size > 5000


def test_lookalike_characters_survive():
    assert latex.escape("peer\u2011reviewed fine\u2010tuned 3\u00a0weeks") == "peer-reviewed f{}ine-tuned 3 weeks"  # f{}: no "ﬁ" ligature
    assert latex.escape("a • b … c") == r"a \textbullet{} b \ldots{} c"


def test_skills_listed_once_and_name_bold_in_publications():
    lines = latex.unique_skills([{"label": "ML", "items": "PyTorch, LoRA, RAG"},
                                 {"label": "Tools", "items": "PyTorch, Docker, AWS (S3, Bedrock)"},
                                 {"label": "Again", "items": "pytorch, rag"}])
    assert [l["label"] for l in lines] == ["ML", "Tools"]
    assert lines[1]["items"] == "Docker, AWS (S3, Bedrock)."
    assert latex.bold_name("S. Example et al. Title. ACL, 2026.", "Jane Example").startswith(
        "**S. Example**")
    assert latex.bold_name("V. Kumari, S. Gupta, S. Example, et al.", "Jane Example") == \
        "V. Kumari, S. Gupta, **S. Example**, et al."


def test_education_is_put_back_when_left_out():
    doc = latex.from_markdown(MASTER)
    without = {**doc, "sections": [s for s in doc["sections"] if s["kind"] != "education"]}
    fixed = latex.ensure_education(without, MASTER)
    assert [s["kind"] for s in fixed["sections"]] == ["entries", "education", "skills"]
    assert latex.ensure_education(doc, MASTER) == doc


def test_empty_education_is_replaced_and_middle_dot_prints():
    doc = latex.from_markdown(MASTER)
    empty = {**doc, "sections": [s if s["kind"] != "education" else {**s, "entries": []} for s in doc["sections"]]}
    fixed = latex.ensure_education(empty, MASTER)
    assert next(s for s in fixed["sections"] if s["kind"] == "education")["entries"]
    assert latex.escape("2025 · London") == r"2025 \textperiodcentered{} London"


@pytest.mark.skipif(not latex.available(), reason="no LaTeX engine")
def test_a_section_filled_in_the_wrong_field_still_typesets(tmp_path):
    # A model filled the wrong field for a section's kind: an empty list failed the whole resume
    # ("perhaps a missing \item").
    doc = latex.from_markdown(MASTER)
    doc["sections"] += [
        {"title": "Publications", "kind": "list", "items": [], "entries": [], "skills": [], "text": "",
         "extra": None},
        {"title": "Awards", "kind": "entries", "entries": [], "items": ["Best Paper, 2025"], "skills": []},
        {"title": "Empty", "kind": "list", "items": [], "entries": [], "skills": []}]
    tex = latex.resume_tex(doc, {"name": "Jane Doe", "email": "j@x.org"}, 1)
    assert "Best Paper, 2025" in tex and r"\section{Empty}" not in tex
    assert latex.compile_pdf(tex, tmp_path / "r.pdf") >= 1
