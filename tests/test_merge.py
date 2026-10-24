"""The resume merge's rules (docs/merge-guide.md), each found by the merge eval
(docs/merge-eval.md). A scripted model stands in for the one that judges repeats."""

import json

from job_agent import merge


class Judge:
    """Groups lines that contain the same marker word, as a model would group two wordings."""

    def __init__(self, *markers):
        self.markers, self.calls = markers, 0

    def json(self, system, user, schema, name="answer"):
        self.calls += 1
        groups = []
        for unit in json.loads(user)["units"]:
            for marker in self.markers:
                ids = [l["id"] for l in unit["lines"] if marker.lower() in l["text"].lower()]
                if len(ids) > 1:
                    groups.append(ids)
        return {"groups": groups}


def merged(old: str, new: str, judge=None) -> dict:
    return merge.merge([("old", old), ("new", new)], judge, log=lambda *a: None)


ROLE = "## Experience\n### Engineer — Lumen\nJan 2024 – Present\n"


def test_two_wordings_of_one_result_keep_the_fuller():
    old = "# A\n" + ROLE + "- Built the evaluation harness the team used before each release.\n"
    new = "# A\n" + ROLE + "- Created an offline evaluation harness with 6,000 labeled queries that gates every release.\n"
    result = merged(old, new, Judge("harness"))
    assert result["markdown"].count("harness") == 1 and "6,000 labeled queries" in result["markdown"]
    assert result["combined"][0]["left_out"] == ["- Built the evaluation harness the team used before each release."]


def test_a_wording_with_a_fact_the_other_lacks_is_kept_and_shown():
    old = "# A\n" + ROLE + "- Built the summarization pipeline for 7 teams, with daily rollups across 120 regions.\n"
    new = "# A\n" + ROLE + "- Architected a summarization pipeline for 20K+ reports a quarter, used by 7 teams.\n"
    result = merged(old, new, Judge("summarization"))
    assert "120 regions" in result["markdown"] and "20K+" in result["markdown"]
    assert len(result["possible"]) == 1


def test_look_alike_lines_about_different_work_stay_apart():
    old = "# A\n" + ROLE + "- Taught algebra to 30 students across 5 sections.\n- Taught geometry to 30 students across 2 sections.\n"
    new = "# A\n" + ROLE + "- Taught algebra to 30 students across 5 sections.\n"
    for judge in (None, Judge("taught")):  # Even if the model joins them, a different number keeps both.
        md = merged(old, new, judge)["markdown"]
        assert md.count("Taught algebra") == 1 and md.count("Taught geometry") == 1


def test_an_updated_number_replaces_the_old_one():
    old = "# A\n" + ROLE + "- Fine-tuned small models for document classification, improving F1 by 9%.\n"
    new = "# A\n" + ROLE + "- Fine-tuned small models for document classification, improving F1 by 12%.\n"
    md = merged(old, new)["markdown"]
    assert "12%" in md and "9%" not in md


def test_a_promotion_is_its_own_role_and_newest_dates_win():
    old = "# A\n## Experience\n### Software Engineer — Brightwave\nJul 2022 – Present\n- Built billing in Go.\n"
    new = ("# A\n## Experience\n### Software Engineer II — Brightwave\nJan 2024 – Mar 2025\n- Designed the invoicing API.\n"
           "### Software Engineer — Brightwave\nJul 2022 – Dec 2023\n- Built billing in Go.\n")
    md = merged(old, new)["markdown"]
    assert "Jul 2022 – Dec 2023" in md and "Jul 2022 – Present" not in md
    assert md.count("Built billing in Go.") == 1 and "Software Engineer II" in md


def test_contact_details_from_every_version_newest_phone_wins():
    old = "# A\na@example.com · (415) 555-0100 · github.com/a\n## Skills\nGo\n"
    new = "# A\na@example.com · (415) 555-0199 · linkedin.com/in/a\n## Skills\nGo\n"
    header = merged(old, new)["markdown"].splitlines()[1]
    assert "555-0199" in header and "555-0100" not in header and "github.com/a" in header and "linkedin" in header


def test_skills_once_each_and_none_lost():
    old = "# A\n## Skills\n- **Engineering:** Python, SQL, Docker, Git\n- **ML:** PyTorch, LangGraph\n"
    new = "# A\n## Skills\n- **Systems:** Python, SQL, Docker, orchestration (LangGraph, LangChain)\n- **ML:** PyTorch\n"
    skills = merged(old, new)["markdown"].split("## Skills")[1]
    assert skills.count("Python") == 1 and skills.count("LangGraph") == 1 and "Git" in skills


def test_summaries_come_first_and_both_framings_stay():
    old = "# A\n## Professional Summary\nEngineer with 3 years in ranking.\n" + ROLE + "- Built search.\n"
    new = "# A\n" + ROLE + "- Built search.\n## Research Focus\nRetrieval for high-stakes domains.\n"
    md = merged(old, new, Judge("ranking", "retrieval"))["markdown"]
    sections = [l for l in md.splitlines() if l.startswith("## ")]
    assert sections[0] in ("## Research Focus", "## Professional Summary") and len(sections) == 3


def test_a_line_from_an_unshared_section_finds_its_home():
    old = "# A\n## Awards & Leadership\n- Captain (Chess Club), Editor (Student Newspaper).\n"
    new = ("# A\n## Activities & Leadership\n- Captain, The Chess Club\n- Editor, Student Newspaper\n"
           "## Awards & Honors\n- Dean's list 2024\n")
    md = merged(old, new)["markdown"]
    assert "Awards & Leadership" not in md and md.count("Chess Club") == 1


def test_a_hand_added_line_in_an_older_version_survives():
    old = "# A\n" + ROLE + "- Led the design system migration across 3 teams.\n- Ran usability sessions.\n"
    new = "# A\n" + ROLE + "- Ran usability sessions every quarter.\n"
    md = merged(old, new, Judge("usability"))["markdown"]
    assert "design system migration" in md and md.count("usability sessions") == 1


def test_the_model_only_groups_and_bad_ids_are_ignored():
    class Wild(Judge):
        def json(self, system, user, schema, name="answer"):
            return {"groups": [["u0.0", "u9.9"], ["nonsense", "u0.1"], ["u0.0", "u1.0"]]}  # Unknown or across units.

    old = "# A\n" + ROLE + "- Built search.\n## Projects\n### Tool\n- Wrote a CLI.\n"
    new = "# A\n" + ROLE + "- Built search for 2M users.\n## Projects\n### Tool\n- Wrote a CLI tool.\n"
    md = merged(old, new, Wild())["markdown"]
    assert "Built search" in md and "Wrote a CLI" in md
