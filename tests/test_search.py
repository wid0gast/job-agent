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
