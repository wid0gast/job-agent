"""The Search page's settings, ranking by match score, and the threshold."""

import http.client
import json
import re
import shutil
import threading
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

import pytest

from job_agent import config, dashboard, matching
from job_agent.tracker import Tracker


class Ranker:
    """Scores jobs by whether 'Research' is in the title."""

    model = "scripted"

    def __init__(self):
        self.calls = 0

    def json(self, system, user, schema, name):
        assert name == "match_scores"
        self.calls += 1
        jobs = json.loads(user)["jobs"]
        assert len(jobs) <= matching.BATCH
        return {"scores": [{"id": j["id"], "score": 90 if "Research" in j["title"] else 40,
                            "why": "research role" if "Research" in j["title"] else "off target"}
                           for j in jobs]}


@pytest.fixture()
def tracker(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    shutil.copytree(config.ROOT / "profile.example", profile)
    monkeypatch.setattr(config, "PROFILE_DIR", profile)
    tracker = Tracker(tmp_path / "t.db")
    for n, title in enumerate(["Research Intern", "Marketing Analyst", "AI Research Engineer"]):
        tracker.add_lead(f"https://job-boards.greenhouse.io/acme/jobs/{n}", "x", "Acme", title, "NYC",
                         job_type="internship", details={"locations": ["NYC"], "terms": ["Summer 2027"]})
    return tracker


def test_search_settings_override_profile(tracker):
    config.save_search({"description": "ML research internships", "locations": [], "fit_threshold": 75})
    prefs = config.load_profile()["preferences"]
    assert prefs["description"] == "ML research internships"
    assert prefs["locations"] == []                         # an emptied filter stays empty
    assert prefs["max_posting_age_days"] == 45               # untouched keys come from profile.yaml
    settings = config.Settings.from_env(require_model=False)
    assert config.fit_threshold(config.load_profile(), settings) == 75


def test_match_scores_rank_jobs_and_go_stale(tracker):
    config.save_search({"description": "ML research internships"})
    model = Ranker()
    result = matching.score(model, tracker, log=lambda *_: None)
    assert result == {"scored": 3, "remaining": 0} and model.calls == 1
    ranked = sorted(tracker.with_status("found"), key=lambda j: -matching.ranking_score(j)[0])
    assert [j["title"] for j in ranked][-1] == "Marketing Analyst"
    assert matching.ranking_score(ranked[0]) == (90, "match")
    # A fit score (from prepare) takes over from the match score.
    tracker.update(ranked[-1]["id"], fit_score=95)
    assert matching.ranking_score(tracker.get(ranked[-1]["id"])) == (95, "fit")
    # Changing what you're looking for makes every match score stale.
    config.save_search({"description": "Data analyst roles"})
    assert len(matching.stale(tracker, config.load_profile(), config.read_master_resume())) == 3


def test_search_page_saves_and_jobs_page_ranks(tracker, tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "DB_PATH", tmp_path / "t.db")
    config.save_search({"description": "ML research internships"})
    matching.score(Ranker(), tracker, log=lambda *_: None)
    app = dashboard.Dashboard(config.Settings.from_env(require_model=False), 0)
    server = ThreadingHTTPServer(("127.0.0.1", 0), None)
    app.port = server.server_port
    server.RequestHandlerClass = dashboard.handler_for(app)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def request(method, path, body=""):
        conn = http.client.HTTPConnection("127.0.0.1", app.port, timeout=10)
        conn.request(method, path, body, {"Host": f"127.0.0.1:{app.port}",
                                          "Origin": f"http://127.0.0.1:{app.port}",
                                          "Content-Type": "application/x-www-form-urlencoded"})
        response = conn.getresponse()
        return response.status, response.read().decode()

    try:
        page = request("GET", "/jobs")[1]
        order = [page.index(t) for t in ("Research Intern", "AI Research Engineer", "Marketing Analyst")]
        assert order[2] > max(order[:2])                    # the off-target job ranks last
        assert re.search(r'class="below"[^>]*>.*?Marketing Analyst', page, re.S)  # under the threshold
        body = urlencode({"token": app.token, "description": "Research roles", "roles": "Research Intern",
                          "job_types": "internship", "locations": "any", "terms": "",
                          "fit_threshold": "80", "max_posting_age_days": ""})
        status, _ = request("POST", "/search", body)
        assert status == 303
        prefs = config.load_profile()["preferences"]
        assert prefs["fit_threshold"] == 80 and prefs["locations"] == ["any"] and prefs["terms"] == []
        assert "Research roles" in request("GET", "/search")[1]
    finally:
        server.shutdown()


def test_saving_search_with_no_jobs_starts_a_refresh(tmp_path, monkeypatch):
    """A fresh session (every hosted visit) has no jobs yet: Save fetches them instead of reporting 0."""
    profile = tmp_path / "profile"
    shutil.copytree(config.ROOT / "profile.example", profile)
    monkeypatch.setattr(config, "PROFILE_DIR", profile)
    monkeypatch.setattr(dashboard, "DB_PATH", tmp_path / "empty.db")
    app = dashboard.Dashboard(config.Settings.from_env(require_model=False), 0)
    monkeypatch.setattr(app, "ensure_worker", lambda: None)
    server = ThreadingHTTPServer(("127.0.0.1", 0), None)
    app.port = server.server_port
    server.RequestHandlerClass = dashboard.handler_for(app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", app.port, timeout=10)
        body = urlencode({"token": app.token, "description": "ML internships", "job_types": "internship",
                          "locations": "any", "fit_threshold": "70"})
        conn.request("POST", "/search", body, {"Host": f"127.0.0.1:{app.port}",
                                               "Origin": f"http://127.0.0.1:{app.port}",
                                               "Content-Type": "application/x-www-form-urlencoded"})
        response = conn.getresponse()
        assert response.status == 303 and response.getheader("Location", "").split("?")[0].endswith("/")
        assert [t["kind"] for t in app.tracker.tasks()] == ["refresh"]
    finally:
        server.shutdown()


def test_keyword_rank_is_free_and_shortlists_for_the_model(tracker, monkeypatch):
    """Every job gets a keyword score without the model; the model ranks only the best of them."""
    config.save_search({"description": "Machine learning research internships", "roles": ["Research Intern"]})
    assert matching.keyword_rank(tracker, config.load_profile()) == 3
    by_title = {j["title"]: matching.ranking_score(j) for j in tracker.with_status("found")}
    assert by_title["Research Intern"][1] == "keyword"
    assert by_title["Research Intern"][0] > by_title["AI Research Engineer"][0] > by_title["Marketing Analyst"][0]
    assert matching.keyword_rank(tracker, config.load_profile()) == 0  # Unchanged search: nothing redone.
    monkeypatch.setattr(matching, "SHORTLIST", 2)
    model = Ranker()
    result = matching.score(model, tracker, log=lambda *_: None)
    assert result == {"scored": 2, "remaining": 1} and model.calls == 1
    kinds = {j["title"]: matching.ranking_score(j)[1] for j in tracker.with_status("found")}
    assert kinds == {"Research Intern": "match", "AI Research Engineer": "match", "Marketing Analyst": "keyword"}
    # A model score always ranks above a keyword-only one.
    ranked = sorted(tracker.with_status("found"), key=matching.rank_key)
    assert ranked[-1]["title"] == "Marketing Analyst"


def test_scoring_has_its_own_lane(tracker):
    """Ranking runs beside other tasks: the lane takes only scoring, the main loop everything else."""
    from job_agent import worker

    config.save_search({"description": "ML research internships"})
    prepare = tracker.add_task("prepare", 1)
    scoring = tracker.add_task("score_matches")
    ctx = type("Ctx", (), {"tracker": tracker, "llm": Ranker()})()
    stop = threading.Event()
    side = threading.Thread(target=worker.lane, args=(ctx, lambda *_: None, stop), daemon=True)
    side.start()
    try:
        for _ in range(50):
            if tracker.task(scoring)["status"] == "done":
                break
            threading.Event().wait(0.1)
    finally:
        stop.set()
        side.join(5)
    assert tracker.task(scoring)["status"] == "done"
    assert tracker.task(prepare)["status"] == "queued"  # Left for the main loop.
    assert tracker.claim_task(exclude=worker.OWN_LANE)["id"] == prepare


def test_refresh_runs_beside_a_prepare(tracker, monkeypatch):
    """A refresh reads every company board (over 15 minutes on the class platform); a prepare
    queued behind it used to wait all that time. The lists lane takes it instead."""
    from job_agent import worker

    monkeypatch.setitem(worker.HANDLERS, "refresh", lambda task, ctx, log: "lists read")
    refresh, prepare = tracker.add_task("refresh"), tracker.add_task("prepare", 1)
    ctx = type("Ctx", (), {"tracker": tracker, "llm": None})()
    stop = threading.Event()
    side = threading.Thread(target=worker.lane, args=(ctx, lambda *_: None, stop, worker.LANES[1]), daemon=True)
    side.start()
    try:
        for _ in range(50):
            if tracker.task(refresh)["status"] == "done":
                break
            threading.Event().wait(0.1)
    finally:
        stop.set()
        side.join(5)
    assert tracker.task(refresh)["result"] == "lists read"
    assert tracker.claim_task(exclude=worker.OWN_LANE)["id"] == prepare, "the main loop takes the prepare"


def test_refresh_keeps_only_jobs_that_fit_and_ranks_them_batch_by_batch(tmp_path, monkeypatch):
    """Jobs outside the search are dropped before they're stored (tens of thousands filled the
    class platform's workspace), and each batch is filtered and ranked as it lands."""
    from contextlib import nullcontext

    import yaml

    from job_agent import companies, config, sources
    from job_agent.tracker import Tracker

    (config.PROFILE_DIR / "search.yaml").write_text(yaml.safe_dump(
        {"roles": ["Data Scientist"], "locations": ["any"], "job_types": ["internship", "full_time"],
         "exclude_companies": ["Initech"], "max_posting_age_days": None}))
    registry = tmp_path / "companies.csv"
    registry.write_text("company_id,name,aliases,website,ats,board,field_tags,source,added\n"
                        "acme,Acme,,,greenhouse,acme,,list,2026-01-01\n"
                        "globex,Globex,,,greenhouse,globex,,list,2026-01-01\n")
    monkeypatch.setattr(companies, "COMPANIES_FILE", registry)

    def posting(company, title, n):
        return {"site": "test", "url": f"https://job-boards.greenhouse.io/{company.lower()}/jobs/{n}",
                "company": company, "title": title, "location": "New York, NY", "posted_at": "2026-10-01",
                "active": True, "job_type": "internship", "details": {"locations": ["New York, NY"]}}

    listed = [posting("Acme", "Data Scientist Intern", 1), posting("Acme", "Sales Associate Intern", 2),
              posting("Initech", "Data Scientist Intern", 3)]
    boards = {"acme": [posting("Acme", "Senior Data Scientist", 4)],
              "globex": [posting("Globex", "Data Scientist, Growth", 5), posting("Globex", "Recruiter", 6)],
              "initech": []}  # Registered from the lists, as every listed company's board is.
    monkeypatch.setattr(sources, "http", lambda: nullcontext(None))
    monkeypatch.setattr(sources, "simplify_jobs", lambda client, fields: [dict(j) for j in listed])
    monkeypatch.setattr(sources, "BOARD_FEEDS", {"greenhouse": lambda client, board: [dict(j) for j in boards[board]]})
    monkeypatch.setattr(sources, "BOARD_BATCH", 1)

    tracker = Tracker(tmp_path / "t.db")
    updates = []
    counts = sources.refresh(tracker, config.load_profile(), boards=True, log=lambda *_: None,
                             progress=updates.append)
    titles = sorted((j["company"], j["title"]) for j in tracker.all())
    assert titles == [("Acme", "Data Scientist Intern"), ("Acme", "Senior Data Scientist"),
                      ("Globex", "Data Scientist, Growth")]  # Sales, Recruiter and Initech were dropped.
    assert counts["skipped"] == 3
    assert [u.split(":")[0] for u in updates] == ["Job lists read", "1 of 3 company boards read",
                                                  "2 of 3 company boards read", "3 of 3 company boards read"]
    found = tracker.with_status("found")
    assert all("keyword" in json.loads(j["details"]) for j in found)  # Ranked as they landed.


def test_the_wrong_kind_or_level_caps_a_match_score_in_code(tracker):
    class Glowing(Ranker):  # Says what's wrong, scores it 90 anyway.
        def json(self, system, user, schema, name):
            return {"scores": [{"id": j["id"], "score": 90, "why": "x", "kind_fits": "Analyst" not in j["title"],
                                "level_fits": "Engineer" not in j["title"]} for j in json.loads(user)["jobs"]]}

    config.save_search({"description": "ML research internships"})
    matching.score(Glowing(), tracker, log=lambda *_: None)
    scores = {j["title"]: json.loads(j["details"])["match"]["score"] for j in tracker.all()}
    assert scores == {"Research Intern": 90, "Marketing Analyst": 35, "AI Research Engineer": 45}


def test_a_full_time_job_is_capped_when_you_ask_only_for_internships():
    full = {"job_type": "full_time"}
    assert matching.wrong_kind("ML research internships for Summer 2027", full)
    assert not matching.wrong_kind("Internships or full-time new grad roles in ML", full)
    assert not matching.wrong_kind("ML research internships", {"job_type": "internship"})
    assert matching.wrong_kind("Full-time ML engineer roles", {"job_type": "internship"})


def test_fit_text_about_you_is_said_to_you():
    from job_agent.documents import to_you

    assert to_you({"summary": "He has an MS. The candidate lacks a 2025 degree. Prof. Lee and her lab."}) == \
        {"summary": "You have an MS. You lack a 2025 degree. Prof. Lee and her lab."}


def test_a_top_pick_that_falls_short_is_replaced_by_the_next(tracker, monkeypatch):
    from types import SimpleNamespace

    from job_agent import actions, worker

    ran = []

    def prepare(job_id, ctx, log, note=""):
        ran.append((job_id, note))
        ctx.tracker.move(job_id, "found", "filtered_out", reason="Fit 45 is below your threshold of 60.")
        return "filtered_out"

    monkeypatch.setattr(worker, "step", lambda module, name: prepare)
    monkeypatch.setattr(actions, "top_picks", lambda tracker, settings, count: ([{"id": 2, "title": "Analyst", "company": "Acme"}], []))
    ctx = SimpleNamespace(tracker=tracker, settings=None)
    said = worker._prepare({"job_id": 1, "arg": f"{worker.TOP_PICK}1"}, ctx, print)
    assert ran == [(1, "")] and "Trying the next match instead: Analyst at Acme" in said
    queued = tracker.db.execute("SELECT job_id, arg FROM tasks WHERE kind = 'prepare'").fetchall()
    assert [tuple(r) for r in queued] == [(2, f"{worker.TOP_PICK}0")]
    assert "next match" not in worker._prepare({"job_id": 2, "arg": f"{worker.TOP_PICK}0"}, ctx, print)
