"""How to apply (strategy.py) and the hiring manager's read (first_read.py)."""

from datetime import UTC, datetime, timedelta

from job_agent import first_read, strategy

NOW = datetime(2026, 10, 22, 12, tzinfo=UTC)


def ago(days: int) -> str:
    return (NOW - timedelta(days=days)).isoformat()


def job(**extra) -> dict:
    return {"id": 1, "status": "found", "title": "Data Analyst", "location": "New York, NY",
            "job_type": "full_time", "posted_at": ago(3), "fit_score": None, **extra}


def route(j, **kw):
    return strategy.advise(j, listings=kw.pop("listings", []), now=NOW, **kw)


def test_a_fresh_posting_with_nothing_in_the_way_is_apply():
    advice = route(job())
    assert advice["route"] == "apply" and "new" in advice["headline"]
    assert any(r["good"] and "3 days ago" in r["text"] for r in advice["reasons"])


def test_a_role_posted_again_in_the_same_place_is_think_twice_but_a_new_office_is_not():
    earlier = {"location": "New York, NY", "posted_at": ago(80), "status": "closed"}
    assert route(job(), listings=[earlier])["route"] == "think_twice"
    elsewhere = {**earlier, "location": "Austin, TX"}
    assert route(job(), listings=[elsewhere])["route"] == "apply"


def test_internships_stay_open_longer_before_they_look_stale():
    assert route(job(posted_at=ago(60)))["route"] == "think_twice"
    assert route(job(posted_at=ago(60), job_type="internship"))["route"] == "apply"
    assert route(job(posted_at=ago(95), job_type="internship"))["route"] == "think_twice"


def test_people_found_come_first_and_waiting_on_them_is_said():
    person = {"name": "Priya Raman", "kind": "background", "title": "Data Scientist"}
    packet = {"people": {"people": [person]}}
    assert route(job(status="prepared"), packet=packet)["route"] == "reach_out"
    person["sent_at"] = ago(1)
    advice = route(job(status="prepared"), packet=packet)
    assert advice["route"] == "reach_out" and "give it until" in advice["headline"]
    person["sent_at"] = ago(5)  # No reply in time: apply (the follow-up is on Today).
    assert route(job(status="prepared"), packet=packet)["route"] == "apply"
    person["replied_at"] = ago(2)
    assert "replied" in route(job(status="prepared"), packet=packet)["headline"]


def test_a_stale_posting_with_someone_to_ask_is_reach_out():
    packet = {"people": {"people": [{"name": "Sam Lee", "kind": "peer", "title": "Analyst"}]}}
    advice = route(job(status="prepared", posted_at=ago(70)), packet=packet)
    assert advice["route"] == "reach_out" and "still open" in advice["headline"]


def test_your_own_applications_and_the_company_limit_come_first():
    applied = {"title": "Data Analyst II", "submitted_at": ago(10)}
    assert route(job(), applied=applied)["route"] == "think_twice"
    assert route(job(), cap="Company cap reached: 3 of 3 in 30 days.")["headline"].startswith("Company cap")
    assert route(job(status="submitted")) is None


def test_the_tracker_lists_a_roles_other_rows(tmp_path):
    from job_agent.tracker import Tracker

    t = Tracker(tmp_path / "jobs.db")
    t.add_lead("https://boards.greenhouse.io/acme/jobs/1", "greenhouse", "Acme", "Data Analyst", "NYC")
    t.add_lead("https://boards.greenhouse.io/acme/jobs/2", "greenhouse", "Acme", "Data Analyst", "Austin")
    t.add_lead("https://boards.greenhouse.io/acme/jobs/3", "greenhouse", "Acme", "Designer", "NYC")
    first = t.get(1)
    assert [row["id"] for row in t.listings(first)] == [2]


def packet(tmp_path, essay="I cleaned 2 million rows of loan data."):
    letter = tmp_path / "letter.md"
    letter.write_text("Dear team,\n\nI built the loan model.", encoding="utf-8")
    return {"job": {"company": "Acme", "title": "Analyst"}, "description": "SQL.",
            "resume_doc": {"header_note": "", "sections": [{"title": "Experience", "kind": "entries", "text": "",
                           "items": [], "skills": [], "entries": [{"title": "Intern", "organization": "Credit Union",
                           "dates": "2025", "location": "", "bullets": ["Joined member and loan tables in SQL."]}]}]},
            "documents": {"cover_letter": {"markdown": str(letter), "pdf": ""}},
            "answers": [{"key": "why", "label": "Why us?", "kind": "textarea", "value": essay, "required": True}]}


class Reader:
    def __init__(self, concerns):
        self.concerns, self.sent = concerns, None

    def json(self, system, user, schema, name):
        self.sent = user
        return {"pile": "interview", "remembered": "The loan model.", "missing": "", "concerns": self.concerns}


def test_concerns_must_quote_the_application(tmp_path):
    class Ctx:
        llm = Reader([{"where": "resume", "quote": "Joined member and  loan tables", "problem": "a", "fix": "b"},
                      {"where": "answer", "quote": "“I cleaned 2 million rows”", "problem": "a", "fix": "b"},
                      {"where": "cover_letter", "quote": "I led a team of 40", "problem": "a", "fix": "b"}])

    read = first_read.read(Ctx(), packet(tmp_path))
    assert [c["where"] for c in read["concerns"]] == ["resume", "answer"]
    assert "I built the loan model." in Ctx.llm.sent  # The letter, the resume and the essays are all read.


def test_a_read_goes_out_of_date_when_what_the_employer_reads_changes(tmp_path):
    before = first_read.basis(packet(tmp_path))
    assert first_read.basis(packet(tmp_path)) == before
    assert first_read.basis(packet(tmp_path, essay="Something else.")) != before
