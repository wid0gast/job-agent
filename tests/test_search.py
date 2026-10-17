"""The Search page's settings, ranking by match score, and the threshold."""

import http.client
import json
import re
import shutil
import threading
from dataclasses import replace
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
    assert prefs["terms"] == ["Summer 2027"]                # untouched keys come from profile.yaml
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
    app = dashboard.Dashboard(replace(config.Settings.from_env(require_model=False), submit_mode="review"), 0)
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
    app = dashboard.Dashboard(replace(config.Settings.from_env(require_model=False), submit_mode="review"), 0)
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
