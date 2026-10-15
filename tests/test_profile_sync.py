"""Profile facts come from the master resume; nothing the resume doesn't say survives."""

from job_agent import profile_sync

RESUME = """# ALEX EXAMPLE
me@example.com · https://www.linkedin.com/in/me/ · https://github.com/me · https://me.dev
Work Authorization: F-1 student (CPT).
## Education
New York University, New York, USA
Master of Science, Statistics. May 2027
"""


class Model:
    def json(self, system, user, schema, name):
        return {"name": "Alex Example", "email": "me@example.com", "phone": "+1 212 555 0199",
                "location": "", "work_authorization_text":
                "F-1 student (CPT).",
                "education": [{"school": "New York University", "location": "New York, USA",
                               "degree": "Master of Science", "major": "Statistics", "start": "",
                               "graduation": "May 2027", "gpa": "4.0"}]}


def test_derive_keeps_only_what_the_resume_says():
    profile, notes = profile_sync.derive(Model(), RESUME)
    contact = profile["contact"]
    assert contact["name"] == "Alex Example" and contact["linkedin"] == "https://www.linkedin.com/in/me/"
    assert contact["github"] == "https://github.com/me" and contact["website"] == "https://me.dev"
    assert "phone" not in contact and any("phone" in n for n in notes)       # invented: dropped
    assert "gpa" not in profile["education"][0]                              # invented: dropped
    assert contact["location"] == "New York, USA" and contact["country"] == "United States"
    auth = profile["work_authorization"]
    assert (auth["authorized_in_us"], auth["requires_sponsorship_now"],
            auth["requires_sponsorship_future"]) == (True, False, True)


def test_profile_yaml_overrides_resume_facts():
    merged = profile_sync.merge({"contact": {"name": "A", "phone": ""}, "education": [1]},
                                {"contact": {"phone": "+1 1"}, "preferences": {"x": 1}})
    assert merged == {"contact": {"name": "A", "phone": "+1 1"}, "education": [1],
                      "preferences": {"x": 1}}


def test_degree_mapping_prefers_the_right_degree():
    from job_agent.answers import degree_option

    options = ["Associate's Degree", "Bachelor's Degree", "Doctor of Philosophy (Ph.D.)",
               "Master of Business Administration (M.B.A.)", "Master's Degree"]
    assert degree_option("Master of Science", options) == "Master's Degree"
    assert degree_option("Bachelor of Engineering", options) == "Bachelor's Degree"
    assert degree_option("MBA", options) == "Master of Business Administration (M.B.A.)"
    assert profile_sync.squash("Springfield, Illinois, USA") in profile_sync.squash("Springfield Illinois, USA")


def test_sponsorship_questions_answer_what_they_ask():
    from job_agent.answers import sponsorship_answer

    f1 = {"requires_sponsorship_now": False, "requires_sponsorship_future": True}
    assert sponsorship_answer("will you require immigration sponsorship to begin working for imc?", f1) == "No"
    assert sponsorship_answer("will you require immigration sponsorship in the future to continue working?", f1) == "Yes"
    assert sponsorship_answer("will you now or in the future require visa sponsorship?", f1) == "Yes"
    assert sponsorship_answer("do you require sponsorship?", f1) == "Yes"


def test_only_visa_sponsorship_questions_get_sponsorship_answers():
    from job_agent import answers

    profile = {"work_authorization": {"requires_sponsorship_now": False, "requires_sponsorship_future": True}}

    def answer(label):
        got = answers.from_profile({"key": "q", "label": label, "kind": "text", "options": [],
                                    "section": "main"}, profile, {})
        return got[0] if got else None

    assert answer("Do you currently receive any active funding (e.g., grants, sponsorships)?") is None
    assert answer("Will you now or in the future require visa sponsorship?") == "Yes"
    assert answer("Do you require sponsorship, or will you require sponsorship in the future?") == "Yes"
    assert answer("Will you require immigration sponsorship to begin working?") == "No"
