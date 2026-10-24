"""The master resume and profile are editable any time; jobs come from whichever fields you pick."""

import http.client
import shutil
import threading
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

import pytest

from job_agent import config, dashboard, filters, resumes, sources


@pytest.fixture()
def profile(tmp_path, monkeypatch):
    folder = tmp_path / "profile"
    shutil.copytree(config.ROOT / "profile.example", folder)
    monkeypatch.setattr(config, "PROFILE_DIR", folder)
    return folder


@pytest.fixture()
def app(profile, tmp_path, monkeypatch):
    monkeypatch.setattr(dashboard, "DB_PATH", tmp_path / "t.db")
    app = dashboard.Dashboard(config.Settings.from_env(require_model=False), 0)
    app.llm_missing = False
    monkeypatch.setattr(app, "ensure_worker", lambda: None)
    server = ThreadingHTTPServer(("127.0.0.1", 0), None)
    app.port = server.server_port
    server.RequestHandlerClass = dashboard.handler_for(app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield app
    server.shutdown()


def request(app, method, path, fields=None):
    conn = http.client.HTTPConnection("127.0.0.1", app.port, timeout=10)
    body = urlencode({"token": app.token, **(fields or {})}, doseq=True) if fields is not None else ""
    conn.request(method, path, body, {"Host": f"127.0.0.1:{app.port}", "Origin": f"http://127.0.0.1:{app.port}",
                                      "Content-Type": "application/x-www-form-urlencoded"})
    response = conn.getresponse()
    return response.status, response.getheader("Location", ""), response.read().decode()


def test_master_resume_edits_keep_history(profile):
    first = (profile / "resume.md").read_text()
    backup = resumes.save_master("# Jane Doe\n\n## Experience\n- Edited by hand\n")
    assert backup.read_text() == first and backup.parent.name == "resume-history"
    assert (profile / "resume.md").read_text() == "# Jane Doe\n\n## Experience\n- Edited by hand\n"
    with pytest.raises(ValueError, match="No changes"):
        resumes.save_master("# Jane Doe\n\n## Experience\n- Edited by hand")
    with pytest.raises(ValueError, match="empty"):
        resumes.save_master("   ")


def test_profile_files_are_checked_before_saving(profile):
    before = (profile / "profile.yaml").read_text()
    with pytest.raises(ValueError, match=r"isn't valid YAML \(line \d+\)"):
        config.save_profile_file("profile.yaml", "preferences:\n  roles: [unclosed\n")
    with pytest.raises(ValueError, match="key: value"):
        config.save_profile_file("answers.yaml", "- just\n- a list\n")
    with pytest.raises(ValueError, match="can't be edited"):
        config.save_profile_file("../.env", "X=1")
    assert (profile / "profile.yaml").read_text() == before  # Nothing half-written.
    config.save_profile_file("story-bank.md", "## Shipping under pressure\nWhat happened...")
    assert config.read_optional("story-bank.md").startswith("## Shipping under pressure")


def test_profile_page_and_resume_editing_in_the_dashboard(app, profile):
    status, _, page = request(app, "GET", "/profile")
    assert status == 200 and all(t in page for t in ("Profile settings", "Saved answers", "Story bank", "Writing sample"))
    status, where, _ = request(app, "POST", "/profile/save", {"file": "profile.yaml", "content": "preferences: [\n"})
    assert status == 303 and "valid%20YAML" in where
    status, where, _ = request(app, "POST", "/profile/save",
                               {"file": "writing-sample.md", "content": "I write plainly."})
    assert "Saved%20writing-sample.md" in where and config.read_optional("writing-sample.md") == "I write plainly.\n"
    status, where, _ = request(app, "POST", "/resumes/master", {"markdown": "# Jane Doe\n- New line\n"})
    assert status == 303 and "Updating%20your%20profile" in where
    assert (profile / "resume.md").read_text() == "# Jane Doe\n- New line\n"
    assert [t["kind"] for t in app.tracker.tasks()] == ["sync_profile"]


def test_jobs_come_from_the_fields_you_pick():
    everything = sources.ALL_FIELDS
    assert sources.chosen_fields({"preferences": {}}) == everything  # None picked: every field.
    assert sources.chosen_fields({"preferences": {"fields": ["quant", "bogus"]}}) == ["quant"]
    assert sources.matches_field("Senior Backend Engineer", ["software"])
    assert not sources.matches_field("Account Executive", ["software", "quant"])
    assert sources.matches_field("Account Executive", ["software", "other"])
    assert sources.matches_field("FPGA Design Intern", ["hardware"])
    assert sources.matches_field("Quantitative Trader", ["quant"])
    assert sources.in_fields("Quantitative Finance", ["quant"])


def test_field_and_seniority_filters():
    def reason(job, details, prefs):
        return filters.check({"job_type": "full_time", "location": "NYC", **job}, details,
                             {"preferences": {"locations": ["any"], **prefs}})

    software = {"fields": ["software"]}
    assert reason({"title": "Software Engineer"}, {"category": "Software"}, software) is None
    assert "Not in your fields" in reason({"title": "ML Engineer"}, {"category": "AI/ML/Data"}, software)
    assert "Not in your fields" in reason({"title": "Sales Lead"}, {}, software)  # A board job: by title.
    assert reason({"title": "Staff Engineer"}, {}, {}) .startswith("Senior role")
    assert reason({"title": "Staff Engineer"}, {}, {"include_senior": True}) is None


def test_work_authorization_is_chosen_once_on_the_profile_page(app, profile):
    import yaml

    from job_agent import answers

    facts = yaml.safe_load((profile / "resume-profile.yaml").read_text())
    facts.pop("work_authorization")
    (profile / "resume-profile.yaml").write_text(yaml.safe_dump(facts))
    _, _, page = request(app, "GET", "/profile")
    assert 'action="/profile/authorization"' in page
    status, where, _ = request(app, "POST", "/profile/authorization", {"choice": "f1"})
    assert status == 303 and "F-1" in where
    auth = config.load_profile()["work_authorization"]
    assert answers.sponsorship_answer("Will you now or in the future require visa sponsorship?", auth) == "Yes"
    assert "# What your resume can't say" in (profile / "profile.yaml").read_text(), "your comments are kept"
    _, _, page = request(app, "GET", "/profile")
    assert 'action="/profile/authorization"' not in page and "F-1 student" in page
    _, where, _ = request(app, "POST", "/profile/authorization", {"choice": "citizen"})
    assert "already%20sets" in where  # Once set, it's changed in profile.yaml.


def test_a_form_about_a_job_returns_to_it_when_no_referer_is_sent(app, profile):
    # The class platform's sandboxed frame sends no Referer; Save changes used to land on Today.
    app.tracker.add_lead("https://job-boards.greenhouse.io/acme/jobs/1", "test", "Acme", "ML Intern", "NYC")
    job_id = app.tracker.leads(5)[0]["id"]
    app.tracker.update(job_id, status="skipped")
    status, where, _ = request(app, "POST", "/requeue", {"job_id": str(job_id)})
    assert status == 303 and where.startswith(f"/job/{job_id}")


def test_a_school_the_forms_search_lacks_is_named_in_its_note():
    from job_agent import answers

    field = {"key": "school--0", "label": "School", "kind": "search", "options": [], "required": True,
             "section": "education", "unlisted": {"value": "New York University", "instead": None}}
    drafted, _ = answers.draft([field], {"education": [{"school": "New York University"}]}, {}, {})
    assert "New York University" in drafted[0]["note"] and "Not in your resume" not in drafted[0]["note"]
