"""Checks in code for what the writing guide (docs/writing-guide.md) asks of generated text."""

from job_agent import answers, documents, latex, research, style


def test_style_flags_what_reads_as_machine_written():
    draft = ("Dear Hiring Manager,\n\nI am writing to apply for the role. I am passionate about data — "
             "it's not just a job, but a calling. Additionally, I leveraged SQL, highlighting my skills.")
    found = " | ".join(style.problems(draft, letter=True))
    for expected in ("stock opening", "passionate", "leveraged", "em dash", "not just", "Additionally",
                     "-ing"):
        assert expected in found, expected


def test_style_passes_plain_specific_writing():
    letter = ("Dear Data Team team,\n\nLast summer I worked out why members of a credit union missed their "
              "first payment. Autopay at signup stood out: members who enrolled were 3x less likely to miss.")
    assert style.problems(letter, letter=True) == []


def test_length_limits_come_from_the_question():
    assert answers.length_limit({"label": "Why us?", "description": "great answers are often 200-400 words"}) == \
        {"range": (200, 400)}
    assert answers.length_limit({"label": "Why this role? (100 words or less)"}) == {"max_words": 100}
    assert answers.length_limit({"label": "About you", "description": "Maximum 500 characters"}) == {"max_chars": 500}
    field = {"label": "Why this role? (100 words or less)"}
    assert "at most 100" in answers.target_words(field)
    assert answers.too_long(field, "word " * 120) and not answers.too_long(field, "word " * 90)


def test_ai_policy_is_found_in_the_form_or_posting():
    fields = [{"label": "Do you acknowledge that these answers are your own?", "description": ""}]
    assert answers.ai_policy("We build models.", fields)
    assert answers.ai_policy("Please do not use AI assistants during the application process.", [])
    assert answers.ai_policy("We build AI assistants for banks.", [{"label": "Why us?", "description": ""}]) is None


def test_a_stated_date_survives_the_words_around_it():
    profile = {"preferences": {"start_date": "June 2027"}}
    assert answers.stated("June 2027, after I finish my Ph.D.", profile, "")
    assert not answers.stated("May 2026", profile, "")


def test_company_questions_get_research():
    job = {"company": "Bracebridge Capital"}
    assert research.wanted({"label": "Why Bracebridge?", "kind": "textarea"}, job)
    assert research.wanted({"label": "Which Fin value resonates most with you?", "kind": "textarea"}, job)
    assert not research.wanted({"label": "Describe a project you worked on with others.", "kind": "textarea"}, job)


def test_rewritten_bullets_are_flagged_and_cuts_are_not():
    master = "- Cleaned and joined member, loan and payment tables (about 2 million rows) in SQL.\n"
    doc = {"sections": [{"entries": [{"bullets": [
        "Joined member, loan and payment tables (about 2 million rows) in SQL.",
        "Acquired and transformed enterprise data assets to drive strategic decisions."]}]}]}
    flagged = [bullet for bullet, _ in documents.rewritten(doc, master)]
    assert flagged == ["Acquired and transformed enterprise data assets to drive strategic decisions."]


def test_projects_without_an_organization_have_no_trailing_comma():
    sec = {"title": "Projects", "kind": "entries", "entries": [
        {"title": "Budget App", "organization": "", "dates": "2025", "location": "", "bullets": ["Built it."]}]}
    tex = latex.section(sec)
    assert r"\textbf{Budget App}" in tex and "Budget App}, " not in tex and r"\expSubheading" not in tex


def test_education_keeps_gpa_and_coursework_lines():
    sec = {"title": "Education", "kind": "education", "entries": [
        {"title": "B.A. Statistics", "organization": "Penn", "dates": "May 2028", "location": "",
         "bullets": ["GPA 3.82", "Relevant coursework: Regression Analysis"]}]}
    tex = latex.section(sec)
    assert "GPA 3.82" in tex and "Regression Analysis" in tex


def test_no_ligatures_in_body_text_but_headings_stay_whole():
    assert latex.escape("fine-tuned workflow") == "f{}ine-tuned workf{}low"
    assert r"\section{Certifications}" in latex.section(
        {"title": "Certifications", "kind": "list", "items": ["PL-300"]})
