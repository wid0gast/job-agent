"""Tests never call a real model, and never touch your profile/, data/ or the repo's company list:
scripted stand-ins replace the model where a test needs one, and every test gets its own copies."""

import os
import shutil

import pytest

os.environ["MODEL_BACKEND"] = "off"  # Set before .env is read; .env never overrides it.

from job_agent import answers, companies, config, dashboard, sources, worker  # noqa: E402


@pytest.fixture(autouse=True)
def own_profile(tmp_path, monkeypatch):
    """A copy of the example profile, an empty data folder and a copy of companies.csv for every
    test (tests may swap their own). Paths fixed when modules load are redirected too."""
    profile, data = tmp_path / "own-profile", tmp_path / "own-data"
    shutil.copytree(config.ROOT / "profile.example", profile)
    registry = tmp_path / "own-companies.csv"
    shutil.copy(config.ROOT / "companies.csv", registry)
    monkeypatch.setattr(config, "PROFILE_DIR", profile)
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(companies, "COMPANIES_FILE", registry)
    monkeypatch.setattr(companies, "LEARNED_FILE", data / "learned-rules.yaml")
    monkeypatch.setattr(answers, "APPROVED_BANK", data / "approved-answers.json")
    monkeypatch.setattr(sources, "CACHE", data / "cache")
    for module in (worker, dashboard):
        monkeypatch.setattr(module, "DB_PATH", data / "applications.db")
    monkeypatch.setattr(worker, "BROWSER_PROFILE", data / "browser-profile")
    monkeypatch.setattr(worker, "LOCK_PATH", data / "worker.lock")
    monkeypatch.setattr(dashboard, "LOG_PATH", data / "logs" / "worker.log")
