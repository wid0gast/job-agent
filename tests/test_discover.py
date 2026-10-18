"""Discovering companies on applicant-tracking sites, reading their boards, and not counting a job twice."""

import json
import shutil

import httpx
import pytest

from job_agent import companies, config, discover, filters, sources
from job_agent.ats import identify
from job_agent.tracker import Tracker, match_key

WORKABLE = {"results": [{"shortcode": "AB12", "title": "Software Engineer Intern", "state": "published",
                         "remote": False, "location": {"city": "New York", "region": "NY", "country": "United States"},
                         "published": "2026-10-01T00:00:00.000Z", "type": "internship"}], "total": 1}
SMARTRECRUITERS = {"totalFound": 1, "content": [
    {"id": "744000111", "name": "Data Engineer", "company": {"name": "Acme Corp"}, "releasedDate": "2026-10-02",
     "location": {"fullLocation": "Austin, TX, United States", "remote": True},
     "typeOfEmployment": {"label": "Full-time"}}]}
BAMBOO = {"result": [{"id": 42, "jobOpeningName": "Backend Engineer", "isRemote": False,
                      "atsLocation": {"city": "Denver", "state": "CO", "country": "United States"},
                      "employmentStatusLabel": "Full-Time"}]}
JOBVITE = ('<table><tr><td class="jv-job-list-name"><a href="/acme/job/oX1y">Platform Engineer</a></td>'
           '<td class="jv-job-list-location"><div class="jv-meta"> Seattle, WA </div></td></tr></table>')
JAZZ = ("<h3 class='list-group-item-heading'> <a href=\"https://acme.applytojob.com/apply/Zz9Yy8Xx7W/QA-Engineer\">"
        " QA Engineer </a> </h3> <ul class='list-inline'> <li><i class='fa fa-map-marker'></i>Remote</li></ul>")
ICIMS = ('<div class="row"><span class="sr-only field-label">Job Locations</span> <span > US-NY-New York</span>'
         '<a href="https://careers-acme.icims.com/jobs/5150/security-engineer/job?in_iframe=1" class="iCIMS_Anchor" '
         'title="5150 - Security Engineer"><h3>Security Engineer</h3></a></div>')


def fake_sites(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "workable.com/api" in url:
        return httpx.Response(200, json=WORKABLE)
    if "smartrecruiters.com" in url:
        return httpx.Response(200, json=SMARTRECRUITERS)
    if "bamboohr.com" in url:
        return httpx.Response(200, json=BAMBOO)
    if "jobvite.com" in url:
        return httpx.Response(200, text=JOBVITE)
    if "applytojob.com" in url:
        return httpx.Response(200, text=JAZZ)
    if "icims.com" in url:
        return httpx.Response(200, text=ICIMS if "pr=0" in url else "")
    if "boards-api.greenhouse.io" in url:
        return httpx.Response(200, json={"jobs": [{"id": 777, "title": "Software Engineer, Infrastructure",
                                                   "absolute_url": "https://job-boards.greenhouse.io/doordashusa/jobs/777",
                                                   "location": {"name": "New York, NY"}, "updated_at": "2026-10-03",
                                                   "content": ""}]})
    return httpx.Response(404)


def client():
    return httpx.Client(transport=httpx.MockTransport(fake_sites))


def test_postings_on_every_system_get_an_identity():
    cases = {
        "https://jobs.smartrecruiters.com/ServiceNow/744000152723759-software-engineer": "smartrecruiters:744000152723759",
        "https://apply.workable.com/mlabs/j/726A5E61FE/": "workable:726A5E61FE",
        "https://helpscout.bamboohr.com/careers/123": "bamboohr:helpscout:123",
        "https://jobs.jobvite.com/enphase-energy/job/oz4NAfwK": "jobvite:oz4NAfwK",
        "https://snapsheet.applytojob.com/apply/JV5PwUIexX/Software-Engineer": "jazzhr:JV5PwUIexX",
        "https://careers-powerschool.icims.com/jobs/53014/senior-engineer/job": "icims:careers-powerschool:53014",
    }
    for url, key in cases.items():
        assert identify(url).key == key, url
    assert identify("https://apply.workable.com/huggingface/").board == "huggingface"  # A board, no job.


def test_each_board_reader():
    with client() as c:
        for ats, board, title, place in [
            ("workable", "acme", "Software Engineer Intern", "New York, NY, United States"),
            ("smartrecruiters", "Acme", "Data Engineer", "Austin, TX, United States; Remote"),
            ("bamboohr", "acme", "Backend Engineer", "Denver, CO, United States"),
            ("jobvite", "acme", "Platform Engineer", "Seattle, WA"),
            ("jazzhr", "acme", "QA Engineer", "Remote"),
            ("icims", "careers-acme", "Security Engineer", "US-NY-New York"),
        ]:
            [job] = sources.BOARD_FEEDS[ats](c, board)
            assert (job["title"], job["location"]) == (title, place), ats
            assert identify(job["url"]).ats == ats and job["active"]
    assert sources.BOARD_FEEDS["workable"](client(), "acme")[0]["job_type"] == "internship"


@pytest.fixture()
def world(tmp_path, monkeypatch):
    company_file = tmp_path / "companies.csv"
    shutil.copy(companies.COMPANIES_FILE, company_file)
    monkeypatch.setattr(companies, "COMPANIES_FILE", company_file)
    monkeypatch.setattr(sources, "http", client)
    profile = tmp_path / "profile"
    shutil.copytree(config.ROOT / "profile.example", profile)
    monkeypatch.setattr(config, "PROFILE_DIR", profile)
    config.save_search({"roles": ["Software Engineer"], "locations": ["New York", "Remote"]})
    return Tracker(tmp_path / "t.db"), company_file


class Searcher:
    """Stands in for Claude's web search: the postings 'found' on job sites."""

    def __init__(self, postings):
        self.postings, self.asked = postings, []

    def web_json(self, system, user, schema, task, domains, max_searches):
        self.asked.append((json.loads(user), task, domains, max_searches))
        return {"postings": self.postings}


def test_discover_adds_boards_and_their_jobs(world):
    tracker, company_file = world
    owner = companies.Companies().record("DoorDash", ats="greenhouse", board="doordashusa")
    companies.Companies().save()
    llm = Searcher([
        {"url": "https://apply.workable.com/acme/j/AB12/", "title": "Software Engineer Intern",
         "company": "Acme", "location": "New York"},
        {"url": "https://job-boards.greenhouse.io/doordashusa/jobs/777", "title": "Software Engineer, Infrastructure",
         "company": "DoorDash USA", "location": "New York"},
        {"url": "https://acme.wd5.myworkdayjobs.com/en-US/Careers/job/NY/Software-Engineer_R123",
         "title": "Software Engineer", "company": "Acme Workday", "location": "New York, NY"},
        {"url": "https://www.indeed.com/viewjob?jk=1", "title": "Software Engineer", "company": "X", "location": ""},
    ])
    counts = discover.run(llm, tracker, config.load_profile(), log=lambda *_: None)
    (request, task, domains, max_searches), = llm.asked
    assert request["roles"] == ["Software Engineer"] and task == "discover" and max_searches == discover.MAX_SEARCHES
    assert "apply.workable.com" in domains and "icims.com" in domains
    registry = companies.Companies(company_file)
    assert registry.by_board("workable", "acme")                      # Read on every refresh from now on.
    jobs = {j["url"]: j for j in tracker.all()}
    assert jobs["https://apply.workable.com/acme/j/AB12/"]["location"] == "New York, NY, United States"  # Board copy.
    assert jobs["https://job-boards.greenhouse.io/doordashusa/jobs/777"]["company_id"] == owner  # Not "doordash-usa".
    assert any("myworkdayjobs" in url for url in jobs)                 # No board feed: added directly.
    assert not any("indeed" in url for url in jobs)                    # Not an ATS site.
    assert counts["new"] == 3


def test_same_job_on_two_systems_is_one_job(tmp_path):
    tracker = Tracker(tmp_path / "t.db")
    tracker.add_lead("https://job-boards.greenhouse.io/acme/jobs/1", "x", "Acme", "Software Engineer Intern - Summer 2027",
                     "NYC", company_id="acme")
    assert tracker.find_duplicate("https://acme.wd1.myworkdayjobs.com/x/job/NYC/SWE-Intern_R1", "Acme Inc.",
                                  "SWE Intern", "acme")
    # Another office on the same system is a different job; so is the full-time role.
    assert not tracker.find_duplicate("https://job-boards.greenhouse.io/acme/jobs/2", "Acme",
                                      "Software Engineer Intern - Summer 2027", "acme")
    assert match_key("acme", "Software Engineer Intern") != match_key("acme", "Software Engineer New Grad")


def test_jobs_you_already_applied_to_are_set_aside(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    shutil.copytree(config.ROOT / "profile.example", profile)
    monkeypatch.setattr(config, "PROFILE_DIR", profile)
    tracker = Tracker(tmp_path / "t.db")
    tracker.add_lead("https://example.com/applied", "imported", "Kensho", "mle", "", company_id="kensho")
    tracker.mark_applied(tracker.all()[0]["id"], "2026-10-14", "you", "From your sheet.")
    for n, title in enumerate(["Machine Learning Engineer Intern", "Data Analyst Intern"]):
        tracker.add_lead(f"https://job-boards.greenhouse.io/kensho/jobs/{n}", "x", "Kensho", title, "Remote",
                         company_id="kensho")
    sources.apply_filters(tracker, config.load_profile())
    by_title = {j["title"]: j for j in tracker.all()}
    assert by_title["Machine Learning Engineer Intern"]["status"] == "filtered_out"
    assert by_title["Machine Learning Engineer Intern"]["reason"].startswith('Looks like your application to "mle"')
    assert by_title["Data Analyst Intern"]["status"] == "found"      # A different role stays.
    from job_agent import actions

    actions.requeue(tracker, by_title["Machine Learning Engineer Intern"]["id"])  # "Not the same job."
    sources.apply_filters(tracker, config.load_profile())
    assert tracker.get(by_title["Machine Learning Engineer Intern"]["id"])["status"] == "found"


def test_remote_abroad_and_past_seasons():
    assert not filters.location_ok(["Remote Spain"], ["Remote"])
    assert filters.location_ok(["Remote in the US"], ["Remote"])
    assert filters.location_ok(["New York / Remote-friendly"], ["Remote"])
    profile = {"preferences": {"locations": ["any"]}}
    assert filters.check({"title": "ML Intern - Summer 2025", "job_type": "internship"}, {}, profile) \
        == "Summer 2025 has passed."
    picky = {"preferences": {"locations": ["any"], "terms": ["Summer 2099"]}}
    assert filters.check({"title": "ML Intern - Fall 2098", "job_type": "internship"}, {}, picky).startswith("Term")


def test_discover_without_a_web_searching_model(world, monkeypatch):
    """The class models can't search the web: the app runs the searches itself, and no model call is made."""
    from job_agent import research

    tracker, company_file = world
    asked = []

    def search(client, query):
        asked.append(query)
        return [{"title": "Job Application for Software Engineer Intern at Acme",
                 "url": "https://apply.workable.com/acme/j/AB12/", "snippet": ""},
                {"title": "Software Engineer - Indeed", "url": "https://www.indeed.com/viewjob?jk=1", "snippet": ""}]

    monkeypatch.setattr(research, "search", search)
    monkeypatch.setattr(discover.time, "sleep", lambda s: None)
    counts = discover.run(None, tracker, config.load_profile(), log=lambda *_: None)
    assert asked and all("site:" in q for q in asked) and len(asked) <= discover.MAX_SEARCHES
    assert '"Software Engineer" New York' in asked[0]
    assert companies.Companies(company_file).by_board("workable", "acme")
    assert counts["new_boards"] == 1 and counts["new"] >= 1
