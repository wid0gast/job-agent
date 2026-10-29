"""Job identity, status moves, caps and hard filters: no network, no browser."""

from dataclasses import dataclass

import pytest

from job_agent import companies, config, filters
from job_agent.ats import identify
from job_agent.llm import check, parse_json
from job_agent.tracker import Tracker


def test_identify_application_systems():
    assert identify("https://job-boards.greenhouse.io/anthropic/jobs/5183051008").key == "greenhouse:5183051008"
    assert identify("https://job-boards.eu.greenhouse.io/agency/jobs/4738026101").board == "agency"
    embed = identify("https://boards.greenhouse.io/embed/job_app?token=7905463")
    assert (embed.board, embed.job_id) == (None, "7905463")
    assert identify("https://stripe.com/jobs/search?gh_jid=7737124").key == "greenhouse:7737124"
    lever = identify("https://jobs.lever.co/acme/acca98ab-c206-4f71-b7a5-6977e4828586/apply")
    assert lever.key == "lever:acca98ab-c206-4f71-b7a5-6977e4828586" and lever.board == "acme"
    workday = identify("https://amainc.wd12.myworkdayjobs.com/ama_careers/job/NASA-LaRC/Intern_R-100786")
    assert workday.key == "workday:amainc/ama_careers:R-100786"
    assert identify("https://careers-x.icims.com/jobs/1/job").ats == "icims"


def test_same_title_different_job_ids_are_different_jobs(tmp_path):
    tracker = Tracker(tmp_path / "t.db")
    assert tracker.add_lead("https://job-boards.greenhouse.io/acme/jobs/1", "s", "Acme", "ML Intern", "NYC")[0]
    assert tracker.add_lead("https://job-boards.greenhouse.io/acme/jobs/2", "s", "Acme", "ML Intern", "SF")[0]
    # The same job on the company's own site is a duplicate.
    assert not tracker.add_lead("https://acme.com/careers?gh_jid=1", "s", "Acme", "ML Intern", "NYC")[0]


def test_status_moves_are_enforced(tmp_path):
    tracker = Tracker(tmp_path / "t.db")
    tracker.add_lead("https://a.com/jobs?jobid=1", "x", "A", "One", "")
    job = tracker.leads(1)[0]
    with pytest.raises(ValueError):
        tracker.update(job["id"], status="submitted")  # found can't jump to submitted
    tracker.move(job["id"], "found", "preparing")
    with pytest.raises(ValueError):
        tracker.move(job["id"], "found", "preparing")  # someone else already moved it
    tracker.move(job["id"], "preparing", "prepared")
    tracker.move(job["id"], "prepared", "approved")
    tracker.move(job["id"], "approved", "submitting")
    # A crash mid-submit is never retried automatically.
    assert tracker.recover() and tracker.get(job["id"])["status"] == "needs_you"


@dataclass
class Caps:
    company_cap: int = 2
    company_window_days: int = 30


def test_company_cap_counts_approved_and_submitted(tmp_path):
    tracker = Tracker(tmp_path / "t.db")
    ids = []
    for n in range(3):
        tracker.add_lead(f"https://a.com/jobs?jobid={n}", "x", "TikTok", f"Role {n}", "",
                         company_id="bytedance")
        ids.append(tracker.leads(10)[-1]["id"])
    for job_id in ids:
        tracker.move(job_id, "found", "preparing")
        tracker.move(job_id, "preparing", "prepared")
    assert companies.cap_problem(tracker, "bytedance", Caps(), ids[0], rules={}) is None
    tracker.move(ids[0], "prepared", "approved")            # reserved, not yet sent
    tracker.move(ids[1], "prepared", "approved")
    assert "cap reached" in companies.cap_problem(tracker, "bytedance", Caps(), ids[2], rules={})
    rules = {"bytedance": {"paused": True, "note": "limit"}}
    assert "Paused" in companies.cap_problem(tracker, "bytedance", Caps(5), ids[2], rules=rules)


def test_aliases_share_one_company_id(tmp_path):
    registry = companies.Companies(tmp_path / "missing.csv")
    registry.record("ByteDance", source="test")
    registry.rows["bytedance"]["aliases"] = "TikTok"
    registry._index(registry.rows["bytedance"])
    assert registry.resolve("TikTok Inc.") == "bytedance"
    assert registry.resolve("Acme, Inc.") == "acme"


PROFILE = {
    "education": [{"degree": "B.S. Computer Science"}],
    "work_authorization": {"requires_sponsorship_now": True},
    "preferences": {"locations": ["New York, NY", "Remote"], "terms": ["Summer 2027"],
                    "job_types": ["internship"], "exclude_companies": ["Initech"]},
}


def test_hard_filters():
    job = {"company": "Acme", "title": "ML Intern", "job_type": "internship", "location": ""}
    ok = {"locations": ["NYC"], "terms": ["Summer 2027"], "degrees": ["Bachelor's", "Master's"]}
    assert filters.check(job, ok, PROFILE) is None
    assert "Location" in filters.check(job, {**ok, "locations": ["Austin, TX"]}, PROFILE)
    assert filters.check(job, {**ok, "locations": ["Remote in USA"]}, PROFILE) is None
    assert filters.check(job, {**ok, "locations": ["Remote"]}, PROFILE) is None
    assert "Location" in filters.check(job, {**ok, "locations": ["Remote in Canada"]}, PROFILE)
    assert filters.check(job, {**ok, "locations": ["Remote in Canada", "NYC"]}, PROFILE) is None
    assert "Term" in filters.check(job, {**ok, "terms": ["Fall 2026"]}, PROFILE)
    assert "Degree" in filters.check(job, {**ok, "degrees": ["PhD"]}, PROFILE)
    assert "Excluded" in filters.check({**job, "company": "Initech"}, ok, PROFILE)
    assert "type" in filters.check({**job, "job_type": "full_time"}, ok, PROFILE)
    assert filters.sponsorship_problem("We are unable to sponsor visas for this role.", PROFILE)
    assert filters.sponsorship_problem("We welcome all applicants.", PROFILE) is None


def test_model_json_is_parsed_and_checked():
    schema = {"type": "object", "required": ["score", "met"],
              "properties": {"score": {"type": "integer"}, "met": {"enum": ["yes", "no"]}}}
    value = parse_json('Sure!\n```json\n{"score": 80, "met": "yes"}\n```')
    check(value, schema)
    with pytest.raises(ValueError):
        check({"score": "high", "met": "yes"}, schema)
    with pytest.raises(ValueError):
        check({"score": 1, "met": "maybe"}, schema)


def test_company_names_match_regardless_of_spacing(tmp_path):
    registry = companies.Companies(tmp_path / "c.csv")
    registry.record("Two Sigma")
    registry.record("Point72")
    registry.record("American Express")
    registry.add_alias("american-express", "Amex")
    assert registry.resolve("TwoSigma") == "two-sigma" and registry.resolve("Point 72") == "point72"
    assert registry.resolve("amex") == "american-express"


def test_greenhouse_board_guesses_from_company_and_site():
    from job_agent.greenhouse import board_guesses

    guesses = board_guesses({"company": "Pinterest", "url": "https://www.pinterestcareers.com/jobs/?gh_jid=1"})
    assert guesses[0] == "pinterest" and "pinterestcareers" in guesses


def test_a_graduation_month_answers_a_list_of_terms():
    # A model picked "None of the above/Beyond 2027" for a May 2027 graduate.
    from job_agent import answers

    field = {"key": "q", "label": "What is your graduation date? (anticipated or completed)", "kind": "select",
             "options": ["Fall 2026", "Spring 2027", "Summer 2027", "None of the above/Beyond 2027"],
             "section": "", "required": True}
    profile = {"education": [{"graduation": "May 2027"}], "contact": {}, "work_authorization": {}}
    assert answers.from_profile(field, profile, {}) == ("Spring 2027", "profile")


def test_itar_is_a_word_not_part_of_military():
    # Amazon's "Military careers" link set an Applied Science internship aside as "itar".
    profile = {"work_authorization": {"requires_sponsorship_future": True}}
    assert filters.sponsorship_problem("Find Careers Teams Military careers Benefits. Veterans and military "
                                       "spouses are encouraged to apply.", profile) is None
    said = filters.sponsorship_problem("This role is subject to ITAR, so applicants must be U.S. persons.", profile)
    assert "subject to ITAR" in said and not said.endswith(".”.")


def test_jobs_found_before_your_resume_is_accepted_are_filtered_once_your_education_is_known(monkeypatch):
    """Get started finds jobs while your resume is still being read. The degree filter needs your
    education, so the jobs are filtered again when your profile is updated from the resume."""
    from types import SimpleNamespace

    from job_agent import profile_sync, sources, worker
    from job_agent.tracker import Tracker

    (config.PROFILE_DIR / "resume-profile.yaml").unlink()  # No resume accepted yet: education unknown.
    tracker = Tracker(config.DATA_DIR / "t.db")
    tracker.add_lead("https://a.com/jobs/1", "x", "A", "ML Intern", "NYC", details={"degrees": ["Master's"]})
    sources.apply_filters(tracker, config.load_profile())
    assert tracker.leads(5)[0]["status"] == "found"

    def accepted(llm, log):  # Your resume says you have a bachelor's.
        (config.PROFILE_DIR / "resume-profile.yaml").write_text("education:\n- degree: B.S. Computer Science\n")
        return []

    monkeypatch.setattr(profile_sync, "sync", accepted)
    said = worker._sync_profile({}, SimpleNamespace(llm=None, tracker=tracker), print)
    assert tracker.get(1)["status"] == "filtered_out" and "1 job(s) set aside" in said
