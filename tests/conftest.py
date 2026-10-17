"""Tests never call a real model: scripted stand-ins replace it where a test needs one."""

import os

os.environ["MODEL_BACKEND"] = "off"  # Set before .env is read; .env never overrides it.
