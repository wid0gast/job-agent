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
