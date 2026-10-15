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
    tracker.update(first["id"], status="working")
    tracker.update(first["id"], status="submitted")
    tracker.update(second["id"], status="skipped")
    assert tracker.submitted_today() == 1
    assert tracker.leads(10) == []


def test_invented_numbers_are_detected():
    master = "Cut runtime from 3 hours to 45 minutes. Improved F1 by 12%. 2026."
    assert unsupported_numbers("Improved F1 by 12% in 2026.", master) == []
    assert unsupported_numbers("Improved F1 by 30% for 500 users.", master) == ["30%", "500"]
    # A number at the end of a line is the same number.
    assert unsupported_numbers("Graduated 2026\nnext", "In 2026 I graduated.") == []
