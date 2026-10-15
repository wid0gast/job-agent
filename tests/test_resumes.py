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
        assert details["checks"] == {"invented_numbers": [], "unknown_words": []}
    assert [v["source"] for v in resumes.parsed()] == ["old.pdf", "new.pdf"]

    proposal = resumes.propose(log=lambda *_: None)
    merged = proposal["markdown"]
    assert proposal["checks"] == {"invented_numbers": [], "unknown_words": []}
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
