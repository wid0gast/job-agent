"""Tests never call a real model, and never touch your profile/ or data/: scripted stand-ins
replace the model where a test needs one, and every test gets its own copy of the example profile."""

import os
import shutil

import pytest

os.environ["MODEL_BACKEND"] = "off"  # Set before .env is read; .env never overrides it.

from job_agent import config  # noqa: E402


@pytest.fixture(autouse=True)
def own_profile(tmp_path, monkeypatch):
    """A copy of the example profile and an empty data folder for every test (tests may swap their own)."""
    profile = tmp_path / "own-profile"
    shutil.copytree(config.ROOT / "profile.example", profile)
    monkeypatch.setattr(config, "PROFILE_DIR", profile)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "own-data")
