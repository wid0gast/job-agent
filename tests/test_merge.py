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


def test_a_venue_on_its_own_line_joins_its_bullet_and_a_links_section_joins_the_contact_line():
    from job_agent.resumes import rejoin

    assert rejoin("- Built corpora.\nNLPW @ ACL 2025\n- Built a parser.\n### Stripe 2022") == \
        "- Built corpora. (NLPW @ ACL 2025)\n- Built a parser.\n### Stripe\n2022"  # A role stays a role.
    md = merged("# A\na@example.com\n## Skills\nGo\n", "# A\n## Skills\nGo\n## Links\na@example.com https://github.com/a\n")["markdown"]
    assert md.splitlines()[1] == "a@example.com · https://github.com/a" and "## Links" not in md


def test_the_same_pdf_uploaded_twice_is_one_version(tmp_path, monkeypatch):
    from job_agent import config, resumes

    monkeypatch.setattr(config, "PROFILE_DIR", tmp_path)
    first = resumes.save_upload("cv.pdf", b"%PDF-1.4 one")
    assert resumes.save_upload("cv.pdf", b"%PDF-1.4 one") == first
    assert resumes.save_upload("cv.pdf", b"%PDF-1.4 two") != first


def test_conversion_formatting_is_made_consistent():
    from job_agent.resumes import rejoin

    assert rejoin("### Engineer — Lumen July 2024 – June 2026\n*   A paper. ACL, 2023. [Link]\n**Skills:** Go") == \
        "### Engineer — Lumen\nJuly 2024 – June 2026\n- A paper. ACL, 2023.\n**Skills:** Go"
    md = merged("# A\na@example.com\n## Skills\nGo\n",
                "# A\na@example.com\n## Awards\n- Dean's list 2024\n**Links:**\n- https://github.com/a\n- https://arxiv.org/pdf/1\n")["markdown"]
    assert "https://github.com/a" in md.splitlines()[1] and "Links" not in md and "arxiv" not in md.split("## Awards")[1]


MASTER = """# A
a@example.com
## Professional Summary
Engineer with 3 years in ranking systems. Currently studying at Example University.
## Experience
### Engineer — Lumen, with Prof. Lee
Jan 2024 – Dec 2025
- Built the ranking service for 2M users.
## Education
### Master of Science — Example University
May 2028
## Skills
- **Languages:** Python, Go
"""


def test_a_tailored_resume_with_shifted_sections_is_put_back_in_line():
    from job_agent import documents

    shifted = {"header_note": "", "sections": [
        {"title": "Professional Summary (tailored) - Engineer with 3 years in ranking systems, focusing on fraud detection.",
         "kind": "entries", "text": "", "items": [], "skills": [],
         "entries": [{"title": "Engineer", "organization": "Lumen (Engineer)", "dates": "Jan 2024 – Dec 2025",
                      "location": "London, UK", "bullets": ["Built the ranking service for 2M users."]}]},
        {"title": "Skills", "kind": "education", "text": "", "items": [], "skills": [],
         "entries": [{"title": "Master of Science", "organization": "Example University", "dates": "May 2028",
                      "location": "", "bullets": []}]},
        {"title": "", "kind": "skills", "text": "", "entries": [], "items": [],
         "skills": [{"label": "Languages", "items": "Python, Go"}]}]}
    doc, fixes = documents.realign(shifted, MASTER)
    assert [s["title"] for s in doc["sections"]] == ["Professional Summary", "Experience", "Education", "Skills"]
    assert "fraud" not in doc["sections"][0]["text"] and "ranking systems" in doc["sections"][0]["text"]
    role = doc["sections"][1]["entries"][0]
    assert role["organization"] == "Lumen, with Prof. Lee" and role["location"] == ""
    faithful = {"header_note": "", "sections": [{"title": "Experience", "kind": "entries", "text": "", "items": [], "skills": [],
                "entries": [{"title": "Engineer", "organization": "Lumen", "dates": "Jan 2024 – Dec 2025", "location": "",
                             "bullets": ["Built the ranking service for 2M users."]}]}]}
    assert documents.realign(faithful, MASTER)[1] == []


def test_an_unusable_tailoring_reply_falls_back_to_the_master_resume():
    from types import SimpleNamespace

    from job_agent import prepare

    class Broken:
        def json(self, *args):
            raise ValueError("Model did not return valid JSON")

    doc = prepare.tailor(SimpleNamespace(llm=Broken()), {"company": "Acme", "title": "Engineer"}, "", {}, MASTER, 1)
    assert doc["untailored"] and [s["title"] for s in doc["sections"]][:2] == ["Professional Summary", "Experience"]


def test_a_resume_chosen_by_id_is_built_from_the_masters_own_lines():
    from types import SimpleNamespace

    from job_agent import prepare

    master = MASTER + "## Research Focus\nRetrieval for high-stakes domains.\n## Publications\n- A. Paper one. ACL, 2024.\n- A. Paper two. EMNLP, 2025.\n"
    doc, text = prepare.numbered(master)
    assert "B1 Built the ranking service for 2M users." in text and "K1 Languages: Python, Go" in text

    class Chooser:
        def json(self, system, user, schema, name):
            assert "B1 Built" in json.loads(user)["resume (numbered)"]
            return {"sections": [{"id": "S5", "entries": [], "items": [], "skills": []},       # Research Focus
                                 {"id": "S1", "entries": [], "items": [], "skills": []},       # a second summary: one only
                                 {"id": "S6", "entries": [], "items": ["I2", "I2", "I9"], "skills": []},
                                 {"id": "S2", "entries": [{"id": "E1", "bullets": ["B1", "B7"]}], "items": [], "skills": []},
                                 {"id": "S2", "entries": [], "items": [], "skills": []},       # repeated: ignored
                                 {"id": "S99", "entries": [], "items": [], "skills": []}],     # unknown: ignored
                    "matched": ["Ranking"], "emphasized": [], "gaps": []}

    out = prepare.select(SimpleNamespace(llm=Chooser()), {"company": "Acme", "title": "Engineer"}, "", {}, master, 1)
    titles = [s["title"] for s in out["sections"]]
    assert titles[:4] == ["Research Focus", "Education", "Publications", "Experience"]  # Still studying: Education next.
    assert out["sections"][2]["items"] == ["A. Paper two. EMNLP, 2025."]
    assert out["sections"][3]["entries"][0]["bullets"] == ["Built the ranking service for 2M users."]
    assert out["sections"][3]["entries"][0]["organization"] == "Lumen, with Prof. Lee"


def test_lines_chosen_by_their_text_and_under_the_wrong_section_go_home():
    from types import SimpleNamespace

    from job_agent import prepare

    master = MASTER + "## Publications\n- A. Paper one. ACL, 2024.\n- A. Paper two. EMNLP, 2025.\n"

    class Texty:  # As DeepSeek answered: the lines' text, and everything under the summary's id.
        def json(self, system, user, schema, name):
            return {"sections": [{"id": "S1", "entries": [{"id": "E1", "bullets": ["Built the ranking service for 2M users."]}],
                                  "items": ["A. Paper two. EMNLP, 2025."], "skills": ["Python"]}],
                    "matched": [], "emphasized": [], "gaps": []}

    out = prepare.select(SimpleNamespace(llm=Texty()), {"company": "Acme", "title": "Engineer"}, "", {}, master, 1)
    by_title = {s["title"]: s for s in out["sections"]}
    assert by_title["Experience"]["entries"][0]["bullets"] == ["Built the ranking service for 2M users."]
    assert by_title["Publications"]["items"] == ["A. Paper two. EMNLP, 2025."]
    assert by_title["Skills"]["skills"] == [{"label": "Languages", "items": "Python, Go"}]
    assert "Education" in by_title and "Professional Summary" in by_title


def test_tailored_sections_put_the_summary_first_and_minor_sections_last():
    from job_agent import prepare

    sec = lambda title, kind="entries", dates="": {"title": title, "kind": kind, "text": "x" if kind == "paragraph" else "",  # noqa: E731
                                                   "entries": [{"title": "", "dates": dates}] if dates else [], "items": [], "skills": []}
    got = prepare.ordered([sec("Teaching Experience"), sec("Industry Experience"), sec("Skills", "skills"),
                           sec("Education", dates="May 2099"), sec("Research Focus", "paragraph"), sec("Research Experience")])
    assert [s["title"] for s in got] == ["Research Focus", "Education", "Industry Experience", "Research Experience",
                                         "Teaching Experience", "Skills"]


def test_contact_details_come_in_one_order():
    old = "# A\nsite.dev · https://github.com/a · a@example.com\n## Skills\nGo\n"
    new = "# A\nhttps://github.com/a · a@example.com · site.dev\n## Skills\nGo\n"
    assert merged(old, new)["markdown"].splitlines()[1] == "a@example.com · https://github.com/a · site.dev"


def test_one_line_saying_what_a_role_lists_as_bullets_is_not_added_again():
    old = "# A\n## Activities\nClub: Secretary, Chess Club (2022); Editor, Student Paper (2023).\n"
    new = "# A\n## Activities\n### Club\n- Secretary, Chess Club (2022).\n- Editor, Student Paper (2023).\n"
    md = merged(old, new)["markdown"]
    assert md.count("Chess Club") == 1 and md.count("Student Paper") == 1


def test_an_activities_heading_is_not_taken_for_a_role_that_mentions_the_same_place():
    old = "# A\n## Activities\n### BITS Pilani\n- Secretary, Chess Club (2022).\n"
    new = "# A\n## Teaching Experience\n### Teaching Assistant — Graph Mining | BITS Pilani\nAug 2023 – Dec 2023\n- Ran labs.\n## Activities\n- BITS Pilani: Secretary, Chess Club (2022).\n"
    md = merged(old, new)["markdown"]
    assert md.count("Chess Club") == 1 and "Ran labs" in md.split("## Activities")[0]


def test_an_older_contact_item_the_resume_already_says_is_not_added():
    old = "# A\na@example.com · New York, USA\n## Skills\nGo\n"
    new = "# A\na@example.com\n## Education\n### MS — NYU\nNew York, USA\n## Skills\nGo\n"
    assert merged(old, new)["markdown"].splitlines()[1] == "a@example.com"
