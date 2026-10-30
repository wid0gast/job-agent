from job_agent.documents import unsupported_numbers
from job_agent.tracker import Tracker, job_key


def test_job_key_ignores_tracking_parameters():
    assert job_key("https://www.linkedin.com/jobs/view/3912345678/?trk=abc") == "linkedin:3912345678"
    assert (
        job_key("https://www.linkedin.com/jobs/search/?currentJobId=3912345678&keywords=x")
        == "linkedin:3912345678"
    )
    # Greenhouse job IDs are global, so the board page and a company site agree.
    assert job_key("https://boards.greenhouse.io/acme/jobs/123?utm_source=x") == "greenhouse:123"
    assert job_key("https://acme.com/careers?gh_jid=123&utm=1") == "greenhouse:123"
    assert job_key("https://example.com/careers?jobid=55&utm=1") == "example.com/careers?jobid=55"


def test_duplicates_by_url_and_by_company_title(tmp_path):
    tracker = Tracker(tmp_path / "t.db")
    added, _ = tracker.add_lead("https://www.linkedin.com/jobs/view/1/", "linkedin",
                                "Acme, Inc.", "Software Engineer Intern", "NYC")
    assert added
    again, dup = tracker.add_lead("https://linkedin.com/jobs/view/1?trk=z", "linkedin",
                                  "Acme", "SWE", "NYC")
    assert not again and dup["company"] == "Acme, Inc."
    # Same role found on the company's own ATS is still a duplicate.
    cross, _ = tracker.add_lead("https://jobs.lever.co/acme/abc", "lever",
                                "acme inc", "Software Engineer Intern", "NYC")
    assert not cross


def test_submitted_today_counts_only_submissions(tmp_path):
    tracker = Tracker(tmp_path / "t.db")
    tracker.add_lead("https://a.com/jobs/1", "x", "A", "One", "")
    tracker.add_lead("https://a.com/jobs/2", "x", "A", "Two", "")
    first, second = tracker.leads(10)
    for status in ("preparing", "prepared", "approved", "submitting", "submitted"):
        tracker.update(first["id"], status=status)
    tracker.update(second["id"], status="skipped")
    assert tracker.submitted_today() == 1
    assert tracker.leads(10) == []


def test_invented_numbers_are_detected():
    master = "Cut runtime from 3 hours to 45 minutes. Improved F1 by 12%. 2026."
    assert unsupported_numbers("Improved F1 by 12% in 2026.", master) == []
    assert unsupported_numbers("Improved F1 by 30% for 500 users.", master) == ["30%", "500"]
    # A number at the end of a line is the same number.
    assert unsupported_numbers("Graduated 2026\nnext", "In 2026 I graduated.") == []


def test_one_posting_on_two_career_sites_is_one_job(tmp_path):
    """Workday lists a requisition on each career site (R108261, R108261-1): that's one job. The same
    title under another requisition (another office) is still its own posting, grouped as one role."""
    from job_agent import matching, sources
    from job_agent.tracker import Tracker

    tracker = Tracker(tmp_path / "t.db")
    base = "https://tencent.wd1.myworkdayjobs.com/{}/job/London/GenAI-Research-Intern_{}"
    assert tracker.add_lead(base.format("Interns", "R108261"), "s", "Tencent", "GenAI Research Intern", "London")[0]
    added, existing = tracker.add_lead(base.format("Careers", "R108261-1"), "s", "Tencent", "GenAI Research Intern",
                                       "London")
    assert not added and existing["job_key"].endswith(":R108261")
    assert tracker.add_lead(base.format("Careers", "R109999"), "s", "Tencent", "GenAI Research Intern", "Shenzhen")[0]

    # Copies stored before this rule are set aside, with the reason.
    tracker.db.execute("INSERT INTO jobs (job_key, url, company, title, location, status, created_at, updated_at,"
                       " match_key) SELECT 'workday:tencent/Old:R108261-2', url || '-2', company, title, location,"
                       " 'found', created_at, updated_at, match_key FROM jobs WHERE id = 1")
    assert sources.set_aside_reposts(tracker) == 1
    copy = tracker.by_key("workday:tencent/Old:R108261-2")
    assert copy["status"] == "skipped" and "Same posting as job #1" in copy["reason"]
    [(first, others)] = matching.grouped(tracker.with_status("found"))
    assert [o["location"] for o in others] == ["Shenzhen"] or first["location"] == "Shenzhen"


def test_details_written_side_by_side_keep_each_other(tmp_path):
    """Ranking, a refresh and preparing each set their own part of a job's details, from copies
    read at different times: none undoes another's."""
    import json

    tracker = Tracker(tmp_path / "t.db")
    tracker.add_lead("https://a.com/jobs/1", "x", "A", "One", "", details={"locations": ["NYC"]})
    job = tracker.leads(1)[0]
    tracker.merge_details(job["id"], match={"score": 80})
    tracker.merge_details(job["id"], keyword={"score": 55})  # Written from a copy read before the match.
    details = json.loads(tracker.get(job["id"])["details"])
    assert details == {"locations": ["NYC"], "match": {"score": 80}, "keyword": {"score": 55}}
