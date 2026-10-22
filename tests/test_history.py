"""Your application record: importing a job sheet, outcomes, dates, caps, export."""

import csv
import shutil
from dataclasses import dataclass

import pytest

from job_agent import companies, config, history
from job_agent.tracker import Tracker

SHEET = """Job Link,Company,Role,Jane,Jane Outreach,Sam,Notes
https://job-boards.greenhouse.io/acme/jobs/111?gh_src=x,Acme,ML Intern,Applied,,Applied,
https://jobs.lever.co/acme/aaaaaaaa-1111-4222-8333-444444444444,Acme,Data Intern,OA,Applied,,Code verification
https://jobs.ashbyhq.com/acme/bbbbbbbb-1111-4222-8333-444444444444,Acme,AI Intern,Rejected,,,
https://example.com/careers/42,Initech,Research Intern,,,Applied,
https://example.com/careers/43,Hooli,Data Scientist,Interview!,,,No visa sponsorship
"""


@dataclass
class Caps:
    company_cap: int = 3
    company_window_days: int = 30


@pytest.fixture()
def tracker(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    shutil.copytree(config.ROOT / "profile.example", profile)
    monkeypatch.setattr(config, "PROFILE_DIR", profile)
    monkeypatch.setattr(companies, "COMPANIES_FILE", tmp_path / "companies.csv")
    return Tracker(tmp_path / "t.db")


def write(tmp_path, text):
    path = tmp_path / "sheet.csv"
    path.write_text(text)
    return path


def test_import_outcomes_matching_and_caps(tracker, tmp_path):
    # A job already in the lists (found by refresh) gets marked applied, not duplicated.
    tracker.add_lead("https://job-boards.greenhouse.io/acme/jobs/111", "simplify-intern", "Acme",
                     "Machine Learning Intern", "NYC", company_id="acme")
    report = history.import_sheet(write(tmp_path, SHEET), tracker)
    assert report["status_column"] == "Jane"               # named after you, not Sam
    assert report["added"] == 4 and report["unknown_dates"] == 4
    assert report["skipped"] == ["Initech: no status for you"]
    apps = {j["title"]: j for j in tracker.applications()}
    assert len(tracker.all()) == 4                          # the found job was reused
    assert apps["Machine Learning Intern"]["applied_via"] == "you"
    assert apps["Data Intern"]["outcome"] == "oa" and apps["AI Intern"]["outcome"] == "rejected"
    assert apps["Data Scientist"]["outcome"] == "interview"
    notes = " ".join(e["note"] for e in tracker.events(apps["Data Intern"]["id"]))
    assert "Code verification" in notes and "Jane Outreach: Applied" in notes

    # Undated imports count toward the company cap (the safe side) until you date them.
    assert "cap reached" in companies.cap_problem(tracker, "acme", Caps(), rules={})
    for job in apps.values():
        if job["company"] == "Acme":
            tracker.set_applied_date(job["id"], "2026-01-15T12:00:00+00:00")
    assert companies.cap_problem(tracker, "acme", Caps(), rules={}) is None

    # Importing again changes nothing; a newer status in the sheet updates the outcome.
    again = history.import_sheet(write(tmp_path, SHEET), tracker)
    assert (again["added"], again["updated"]) == (0, 0)
    newer = history.import_sheet(write(tmp_path, SHEET.replace("Acme,ML Intern,Applied", "Acme,ML Intern,Offer")), tracker)
    assert newer["updated"] == 1
    assert {j["title"]: j for j in tracker.applications()}["Machine Learning Intern"]["outcome"] == "offer"

    stats = history.summary(tracker)
    assert stats["total"] == 4 and stats["counts"]["offer"] == 1 and stats["response_rate"] == 100

    out = tmp_path / "export.csv"
    assert history.export_sheet(out, tracker) == 4
    rows = list(csv.DictReader(out.open()))
    hooli = next(r for r in rows if r["Company"] == "Hooli")
    assert hooli["Outcome"] == "interview" and hooli["Date known"] == "no"
    assert "No visa sponsorship" in hooli["History"]


def test_agent_submissions_and_manual_marks_are_recorded(tracker):
    tracker.add_lead("https://a.com/jobs?jobid=1", "x", "A", "One", "")
    tracker.add_lead("https://a.com/jobs?jobid=2", "x", "A", "Two", "")
    one, two = tracker.leads(10)
    for status in ("preparing", "prepared", "approved", "submitting", "submitted"):
        tracker.update(one["id"], status=status)
    job = tracker.get(one["id"])
    assert (job["outcome"], job["applied_via"], job["applied_date_known"]) == ("applied", "agent", 1)
    assert [e["kind"] for e in tracker.events(one["id"])] == ["applied"]

    tracker.mark_applied(two["id"], None, "you")
    with pytest.raises(ValueError):
        tracker.mark_applied(two["id"], None, "you")       # already applied
    tracker.set_outcome(two["id"], "interview", note="Phone screen Tuesday")
    with pytest.raises(ValueError):
        tracker.set_outcome(two["id"], "hired")
    assert [e["kind"] for e in tracker.events(two["id"])] == ["applied", "interview"]


def test_status_column_must_be_clear(tracker, tmp_path):
    sheet = SHEET.replace("Jane,", "Alex,").replace("Jane Outreach", "Alex Outreach")
    with pytest.raises(ValueError, match="status-column"):
        history.import_sheet(write(tmp_path, sheet), tracker)  # profile name is Jane: ambiguous
    assert history.import_sheet(write(tmp_path, sheet), tracker, status="Alex")["added"] == 4


def test_quiet_applications_become_no_response_once(tmp_path):
    from datetime import UTC, datetime, timedelta

    from job_agent import history
    from job_agent.tracker import Tracker

    tracker = Tracker(tmp_path / "t.db")
    tracker.add_lead("https://boards.greenhouse.io/acme/jobs/1", "test", "Acme", "Data Intern", "NYC")
    job = tracker.leads(5)[0]["id"]
    old = (datetime.now(UTC) - timedelta(days=40)).isoformat(timespec="seconds")
    tracker.mark_applied(job, old, "you")
    assert history.mark_quiet(tracker) == 1 and tracker.get(job)["outcome"] == "no_response"
    tracker.set_outcome(job, "applied", note="Still waiting.")  # You set it back:
    assert history.mark_quiet(tracker) == 0 and tracker.get(job)["outcome"] == "applied"
    rows = history.breakdown(tracker)
    assert {"group": "Contacted someone", "value": "no", "n": 1, "heard": 0, "positive": 0} in rows
