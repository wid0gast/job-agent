"""Resume PDFs → Markdown → merged master, with a scripted model and real PDFs."""

import json
import re
from datetime import datetime

import pytest
from playwright.sync_api import sync_playwright

from job_agent import config, documents, resumes
from job_agent.browser import Headless

OLD = """# Jane Doe

New York, NY · jane.old@example.com

## Experience

### Research Assistant — NYU Data Lab
January 2025 – May 2025
- Fine-tuned small language models for document classification, improving F1 by 9%.

## Skills

Python, SQL
"""
NEW = """# Jane Doe

New York, NY · jane.doe@example.com

## Experience

### Software Engineering Intern — Example Corp
June 2026 – August 2026
- Built an internal search service in Python and PostgreSQL used by 40 engineers.

### Research Assistant — NYU Data Lab
January 2025 – May 2025
- Fine-tuned small language models for document classification, improving F1 by 12%.

## Skills

Python, SQL, PostgreSQL
"""


class ScriptedModel:
    """Parsing returns the resume each PDF was made from."""

    def json(self, system, user, schema, name):
        assert name == "resume_markdown"
        return {"markdown": NEW if "Example Corp" in user else OLD, "notes": ""}


@pytest.fixture()
def profile(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROFILE_DIR", tmp_path / "profile")
    (tmp_path / "profile").mkdir()
    with sync_playwright() as p:
        printer = Headless(p, "chromium")
        pdfs = {}
        for name, md in (("old", OLD), ("new", NEW)):
            path = tmp_path / f"{name}.pdf"
            printer.render_pdf(documents.to_html(md, name), path)
            pdfs[name] = path
        printer.close()
    return pdfs


def test_parse_merge_accept(profile):
    model = ScriptedModel()
    # Upload the newer one first: order comes from content dates, not upload order.
    for name in ("new", "old"):
        pdf = resumes.save_upload(f"{name}.pdf", profile[name].read_bytes())
        details = resumes.parse(model, pdf, log=lambda *_: None)
        assert not any(details["checks"].values()) and "left_out" in details["checks"]
    assert [v["source"] for v in resumes.parsed()] == ["old.pdf", "new.pdf"]

    proposal = resumes.propose(log=lambda *_: None)
    merged = proposal["markdown"]
    assert not any(proposal["checks"].values())
    assert "12%" in merged and "9%" not in merged          # same bullet: the newer wording wins
    assert "jane.doe@example.com" in merged and "jane.old" not in merged
    assert proposal["kept"] == []                           # the old version adds nothing

    master = config.PROFILE_DIR / "resume.md"
    master.write_text("# Jane Doe\n\nhand-written version\n")
    backup = resumes.accept()
    assert "hand-written" in backup.read_text() and "Example Corp" in master.read_text()
    assert resumes.proposal() is None


def test_merge_keeps_older_facts_in_place():
    older = OLD.replace("improving F1 by 9%.", "improving F1 by 9%.\n- Taught a weekly NLP reading group.")
    older += "\n## Awards\n\n- Dean's list 2024\n"
    newer = NEW.replace("### Research Assistant — NYU Data Lab", "Research Assistant, NYU Data Lab")
    result = resumes.merge_versions([("old", older), ("new", newer)])
    lines = result["markdown"].splitlines()
    nyu = lines.index("Research Assistant, NYU Data Lab")       # a plain-line role is still a role
    assert "- Taught a weekly NLP reading group." in lines[nyu:nyu + 5]
    assert "## Awards" in lines and "- Dean's list 2024" in lines
    assert {k["line"] for k in result["kept"]} == {"- Taught a weekly NLP reading group.",
                                                   "- Dean's list 2024"}


def test_edited_proposal_is_checked_again(profile):
    for name in ("old", "new"):
        resumes.parse(ScriptedModel(), resumes.save_upload(f"{name}.pdf", profile[name].read_bytes()),
                      log=lambda *_: None)
    proposal = resumes.propose(log=lambda *_: None)
    edited = resumes.edit_proposal(proposal["markdown"] + "\n- Managed 400 engineers with Kubernetes.\n")
    assert "400" in edited["checks"]["invented_numbers"]
    assert "kubernetes" in edited["checks"]["unknown_words"]


def test_uploads_and_dates():
    with pytest.raises(ValueError):
        resumes.save_upload("x.pdf", b"not a pdf")
    today = datetime(2026, 10, 14)
    assert resumes.latest_date("Expected May 2027 · June 2026 – August 2026", today) == (2026, 8)
    assert resumes.latest_date("2024 – 2025", today) == (2025, 0)
    assert resumes.unsupported_words("Python and SQL", ["PythonandSQL"]) == []
    assert resumes.plain("ﬁne-tuned classiﬁers\ue9d4") == "fine-tuned classifiers"
    assert resumes.unsupported_words("financial", ["ﬁnancial"]) == []
    with pytest.raises(ValueError):
        resumes.remove("../../etc/passwd")


def test_parse_retries_when_model_adds_text(profile):
    class Inventive:
        calls = 0

        def json(self, system, user, schema, name):
            Inventive.calls += 1
            extra = "- Led a team of 12 people.\n" if Inventive.calls == 1 else ""
            return {"markdown": OLD + extra, "notes": ""}

    pdf = resumes.save_upload("old.pdf", profile["old"].read_bytes())
    details = resumes.parse(Inventive(), pdf, log=lambda *_: None)
    assert Inventive.calls == 2 and details["checks"]["invented_numbers"] == []
    assert json.loads((resumes.parsed_folder() / "old.json").read_text())["latest_date"] == "2025-05"
    assert not re.search("team of 12", (resumes.parsed_folder() / "old.md").read_text())


def test_merging_keeps_a_summary_and_never_doubles_a_line():
    # From two real resumes: a 4-word role heading "covered" the professional summary, a degree
    # under its school heading was added again, and one line of roles came back beside its bullets.
    from job_agent import resumes

    older = ("# A B\n## Professional Summary\nAI Engineer with 2+ years developing production LLM systems "
             "spanning agentic RAG and retrieval pipelines.\n## Education\nMaster of Science, Data Science. May 2028\n"
             "## Awards & Leadership\n- Captain (Chess Club), Editor (Student Newspaper).\n")
    newer = ("# A B\n## Experience\n### AI Research Engineer — Example Corp\nJuly 2024 – June 2026\n- Built search.\n"
             "## Education\n### New York University — New York, USA\nMaster of Science, Data Science. May 2028\n"
             "## Activities & Leadership\n- Captain, The Chess Club\n- Editor, Student Newspaper\n")
    md = resumes.merge_versions([("old", older), ("new", newer)])["markdown"]
    assert "AI Engineer with 2+ years" in md
    assert md.count("Master of Science, Data Science") == 1 and "Awards & Leadership" not in md


def test_a_conversion_that_leaves_out_a_name_is_caught():
    from job_agent import resumes

    text = "Research Intern, Example University with Prof. Maria Lopez Aug 2023 – Jul 2024"
    md = "### Research Intern — Example University\nAug 2023 – Jul 2024"
    assert resumes.problems(md, [text])["left_out"] == ["prof", "maria", "lopez"]
    assert not resumes.problems(md + ", with Prof. Maria Lopez", [text])["left_out"]


def test_two_cuts_of_one_resume_merge_into_the_fuller_and_keep_every_role():
    # A 1-page and a 2-page cut with the same dates: the condensed one became the base and its
    # short lines "covered" the fuller ones; and "Machine Learning Intern — Northwind" matched
    # "Machine Learning Research Intern — Contoso Research Lab" and vanished into it.
    from job_agent import resumes

    short = ("# A B\n## Experience\n### Machine Learning Research Intern — Contoso Research Lab\nMay 2023 – Aug 2023\n"
             "- Built a legal NLP pipeline.\n")
    full = ("# A B\n## Experience\n### Machine Learning Research Intern — Contoso Research Lab\nMay 2023 – Aug 2023\n"
            "- Built a legal NLP pipeline for court judgments with graph neural networks.\n"
            "### Machine Learning Intern — Northwind Analytics\nMay 2022 – Aug 2022\n- Shipped churn models.\n")
    items = [{"latest_date": "2026-06", "markdown": md, "file_modified": "2026-10-01", "stem": stem}
             for stem, md in (("full", full), ("short", short))]  # Listed in the wrong order on purpose.
    ordered = sorted(items, key=lambda d: (d["latest_date"] or "", len(d["markdown"]), d["file_modified"]))
    md = resumes.merge_versions([(d["stem"], d["markdown"]) for d in ordered])["markdown"]
    assert "court judgments" in md and "Northwind Analytics" in md and "Shipped churn models." in md


def test_a_name_the_conversion_left_out_is_put_back_from_the_pdf_line():
    from job_agent.resumes import restore

    text = "Research Intern, Example University with Prof. Maria Lopez Aug 2023 – Jul 2024\n• Built a parser."
    md = "## Experience\n### Research Intern — Example University\nAug 2023 – Jul 2024\n- Built a parser."
    out = restore(md, text, ["maria", "lopez"])
    assert "### Research Intern — Example University, with Prof. Maria Lopez\n" in out + "\n"
    assert out.count("Aug 2023") == 1


def test_a_whole_line_the_conversion_left_out_is_put_back_beside_its_neighbour():
    from job_agent.resumes import restore

    text = "JANE DOE\njane@example.com | site.dev\nWork Authorization: F-1 student, CPT-eligible.\nEDUCATION\nExample University"
    md = "# JANE DOE\njane@example.com · https://site.dev\n\n## Education\nExample University"
    out = restore(md, text, ["authorization", "cpteligible"])
    assert out.splitlines()[2] == "Work Authorization: F-1 student, CPT-eligible."
