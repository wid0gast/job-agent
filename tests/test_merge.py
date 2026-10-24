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


def test_pdf_extraction_breaks_are_undone_in_code():
    from job_agent.resumes import rejoin

    text = ("T owards Fair T ext Generation · PyT orch, BERT/RoBERT a · Prof. Y vonne Park · Multi-T ask\n"
            "CPT -eligible · first author , complemented · by 61% .\n"
            "https://a@example.com · Q&A summaries · BERT a model · T is")
    assert rejoin(text) == ("Towards Fair Text Generation · PyTorch, BERT/RoBERTa · Prof. Yvonne Park · Multi-Task\n"
                            "CPT-eligible · first author, complemented · by 61%.\n"
                            "a@example.com · Q&A summaries · BERT a model · T is")


def test_a_labeled_part_of_the_contact_line_is_its_own_line_and_a_bare_label_goes():
    old = "# A\na@example.com\nWork Authorization: F-1 student, CPT-eligible.\n## Skills\nGo\n"
    new = ("# A\na@example.com · https://scholar.google.com/citations?user=x | Scholar | "
           "Work Authorization: F-1 student, CPT-eligible\n## Skills\nGo\n")
    header = merged(old, new)["markdown"].split("## Skills")[0]
    assert header.count("Work Authorization") == 1 and "| Scholar" not in header and "· Scholar" not in header
    assert "scholar.google.com" in header


def test_two_wordings_sharing_a_long_stretch_are_caught_without_the_model():
    old = "# A\n" + ROLE + "- Built the summarization pipeline with rollups across 120 regions; used by 4 analytics teams through a shared API.\n"
    new = "# A\n" + ROLE + "- Architected a summarization pipeline for 20K+ reports a quarter, now used by 4 analytics teams through a shared API.\n"
    result = merged(old, new)
    assert "120 regions" in result["markdown"] and "20K+" in result["markdown"] and len(result["possible"]) == 1


def test_older_skill_lines_take_the_newest_style():
    old = "# A\n## Skills\n**Data:** SQL, Excel\n"
    new = "# A\n## Skills\n- **Engineering:** Python, SQL\n"
    skills = merged(old, new)["markdown"].split("## Skills")[1].strip().splitlines()
    assert skills == ["- **Engineering:** Python, SQL", "- **Data:** Excel"]


def test_a_school_written_as_a_plain_line_is_not_added_again():
    old = "# A\n## Education\n### Example University — Springfield, USA\nMaster of Science, Data Science. May 2028\n"
    new = "# A\n## EDUCATION\nExample University, Springfield, USA\nMaster of Science, Data Science. May 2028\n"
    md = merged(old, new)["markdown"]
    assert md.count("Example University") == 1 and md.count("Master of Science") == 1


def test_paper_links_leave_the_contact_line():
    old = "# A\na@example.com\n## Skills\nGo\n"
    new = "# A\na@example.com · https://arxiv.org/pdf/2501.00001 · https://github.com/a\n## Skills\nGo\n"
    result = merged(old, new)
    assert "arxiv" not in result["markdown"].splitlines()[1] and "github.com/a" in result["markdown"]
    assert any("arxiv" in c for c in result["changes"])


def test_wrapped_lines_and_shouted_section_names_are_tidied():
    from job_agent.resumes import rejoin

    assert rejoin("## RESEARCH FOCUS\nNLP researcher, complemented by two\nyears in production.\n## AI PROJECTS") == \
        "## Research Focus\nNLP researcher, complemented by two years in production.\n## AI PROJECTS"
    assert rejoin("BERT and RoBERT a toxicity classifiers") == "BERT and RoBERTa toxicity classifiers"


def test_spaced_out_headings_are_rejoined_into_words():
    from job_agent.resumes import rejoin

    assert rejoin("R E S E A R C H F O C U S\n## S E L E C T E D P U B L I C A T I O N S") == \
        "RESEARCH FOCUS\n## Selected Publications"


def test_two_wordings_sharing_a_number_and_their_subject_are_compared_without_the_model():
    old = "# A\n" + ROLE + "- Owned Recheck, which re-verified answers against their sources, cutting hallucination reports by 61%.\n"
    new = "# A\n" + ROLE + "- Added on-demand checks of each answer claim against its sources; reduced hallucination reports by 61%.\n"
    assert merged(old, new)["markdown"].count("61%") == 1
    other = "# A\n" + ROLE + "- Reduced p95 latency by 40% by caching embeddings.\n"
    again = "# A\n" + ROLE + "- Cut inference costs by 40% by moving to spot instances.\n"
    md = merged(other, again)["markdown"]  # Same number, different work: both stay.
    assert "latency" in md and "inference costs" in md


def test_a_degree_abbreviated_in_one_version_is_the_same_degree():
    old = "# A\n## Education\n### Example University — Springfield, USA\nM.S. Machine Learning, May 2022\n"
    new = "# A\n## Education\n### Master of Science in Machine Learning — Example University\nMay 2022 · Springfield, USA\n"
    assert merged(old, new)["markdown"].count("Example University") == 1


def test_contact_links_the_conversion_skipped_are_added_and_bare_handles_go():
    from job_agent.resumes import add_links

    text = "A\na@example.com | marcuslee | a mlee\n\nLinks in the PDF:\nmailto:a@example.com\nhttps://linkedin.com/in/marcuslee\nhttps://github.com/mlee"
    md = add_links("# A\na@example.com · marcuslee · a mlee\n## Skills\nGo", text)
    header = merged(md, md)["markdown"].splitlines()[1]
    assert header == "a@example.com · https://linkedin.com/in/marcuslee · https://github.com/mlee"
