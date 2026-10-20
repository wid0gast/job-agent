"""Tests never call a real model, and never touch your profile/ or data/."""

import os
import shutil

import pytest

os.environ.pop("OPENROUTER_API_KEY", None)  # Set before .env is read; no test reaches a real model.
os.environ["OPENROUTER_API_KEY"] = ""

from job_agent import config  # noqa: E402


@pytest.fixture(autouse=True)
def own_profile(tmp_path, monkeypatch):
    """A copy of the example profile and an empty data folder for every test (tests may swap their own)."""
    profile = tmp_path / "own-profile"
    shutil.copytree(config.ROOT / "profile.example", profile)
    monkeypatch.setattr(config, "PROFILE_DIR", profile)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "own-data")
    # Paths fixed when modules load, and the repo's company list, go to copies too.
    from job_agent import answers, companies, dashboard, sources, worker

    data, registry = tmp_path / "own-data", tmp_path / "own-companies.csv"
    shutil.copy(config.ROOT / "companies.csv", registry)
    monkeypatch.setattr(companies, "COMPANIES_FILE", registry)
    monkeypatch.setattr(companies, "LEARNED_FILE", data / "learned-rules.yaml")
    monkeypatch.setattr(answers, "APPROVED_BANK", data / "approved-answers.json")
    monkeypatch.setattr(sources, "CACHE", data / "cache")
    for module in (worker, dashboard):
        monkeypatch.setattr(module, "DB_PATH", data / "applications.db")
    monkeypatch.setattr(worker, "BROWSER_PROFILE", data / "browser-profile")
    monkeypatch.setattr(worker, "LOCK_PATH", data / "worker.lock")
    monkeypatch.setattr(worker, "LIVE_SCREENSHOT", data / "applications" / "live-page.png")
    monkeypatch.setattr(dashboard, "LOG_PATH", data / "logs" / "worker.log")
