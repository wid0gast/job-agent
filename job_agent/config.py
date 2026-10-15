"""Paths and settings. Everything personal lives under profile/ and data/."""

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
# On the class app store the app folder is read-only; hosted.py points this at the session's workspace.
HOME = Path(os.environ.get("JOB_AGENT_HOME") or ROOT)
HOSTED = bool(os.environ.get("JOB_AGENT_HOSTED"))
PROFILE_DIR = HOME / "profile"
PROFILE_EXAMPLE_DIR = ROOT / "profile.example"
DATA_DIR = HOME / "data"
SKILLS_DIR = ROOT / "skills"
SITES_FILE = ROOT / "sites.yaml"


def load_env_file(path: Path = ROOT / ".env") -> None:
    """Read KEY=VALUE lines without overriding variables already set."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    api_key: str
    model: str
    submit_mode: str
    daily_submit_limit: int
    browser_channel: str
    company_cap: int = 3
    company_window_days: int = 30
    fit_threshold: int = 60
    spend_cap_usd: float = 1.0
    data_collection: str = "deny"
    cover_letter: str = "required"   # required | always | never

    @classmethod
    def from_env(cls, require_model: bool = True) -> "Settings":
        load_env_file()
        key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        model = os.environ.get("OPENROUTER_MODEL", "").strip()
        if require_model and (not key or not model):
            raise SystemExit(
                "Set OPENROUTER_API_KEY and OPENROUTER_MODEL in job-agent/.env "
                "(copy .env.example)."
            )
        mode = os.environ.get("SUBMIT_MODE", "review").strip().lower()
        if mode not in ("review", "auto"):
            raise SystemExit("SUBMIT_MODE must be review or auto.")
        return cls(
            api_key=key,
            model=model,
            submit_mode=mode,
            daily_submit_limit=int(os.environ.get("DAILY_SUBMIT_LIMIT", "15")),
            browser_channel=os.environ.get("BROWSER_CHANNEL", "chrome").strip(),
            company_cap=int(os.environ.get("COMPANY_CAP", "3")),
            company_window_days=int(os.environ.get("COMPANY_WINDOW_DAYS", "30")),
            fit_threshold=int(os.environ.get("FIT_THRESHOLD", "60")),
            spend_cap_usd=float(os.environ.get("SPEND_CAP_USD", "1.00")),
            data_collection=os.environ.get("OPENROUTER_DATA_COLLECTION", "deny").strip(),
            cover_letter=os.environ.get("COVER_LETTER", "required").strip(),
        )


def load_sites() -> dict:
    return yaml.safe_load(SITES_FILE.read_text(encoding="utf-8"))


def load_profile() -> dict:
    """Facts from your resume (resume-profile.yaml), profile.yaml on top, then the
    Search page's settings (search.yaml) for preferences."""
    from .profile_sync import merge

    path = PROFILE_DIR / "profile.yaml"
    if not path.exists():
        raise SystemExit("No profile yet. Run `uv run job-agent setup` first.")
    derived = PROFILE_DIR / "resume-profile.yaml"
    base = (yaml.safe_load(derived.read_text(encoding="utf-8")) or {}) if derived.exists() else {}
    profile = merge(base, yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    # The Search page replaces whole preference values, so an emptied list
    # (no location filter) really is empty.
    profile["preferences"] = {**(profile.get("preferences") or {}), **load_search()}
    return profile


SEARCH_KEYS = ("description", "roles", "job_types", "locations", "terms", "max_posting_age_days",
               "exclude_companies", "exclude_title_words", "fit_threshold", "resume_pages")


def load_search() -> dict:
    path = PROFILE_DIR / "search.yaml"
    if not path.exists():
        return {}
    return {k: v for k, v in (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).items()
            if k in SEARCH_KEYS}


def save_search(values: dict) -> None:
    header = ("# Written by the dashboard's Search page. These override the same\n"
              "# preferences in profile.yaml.\n")
    clean = {k: values[k] for k in SEARCH_KEYS if k in values}
    (PROFILE_DIR / "search.yaml").write_text(
        header + yaml.safe_dump(clean, sort_keys=False, allow_unicode=True), encoding="utf-8")


def fit_threshold(profile: dict, settings) -> int:
    """The Search page's threshold, else FIT_THRESHOLD from .env."""
    value = (profile.get("preferences") or {}).get("fit_threshold")
    return int(value) if value not in (None, "") else settings.fit_threshold


def read_master_resume() -> str:
    path = PROFILE_DIR / "resume.md"
    if not path.exists():
        raise SystemExit("Add your master resume at profile/resume.md.")
    return path.read_text(encoding="utf-8")


def load_answers() -> dict:
    path = PROFILE_DIR / "answers.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def read_optional(name: str) -> str | None:
    path = PROFILE_DIR / name
    return path.read_text(encoding="utf-8") if path.exists() else None


# ----- site passwords ---------------------------------------------------------
# Locally they live in the macOS Keychain. A hosted session has no keychain and
# forgets everything when it ends, so they go in a file that the dashboard shows
# and the session download includes.

PASSWORDS_FILE = DATA_DIR / "site-passwords.json"


def get_password(service: str, email: str) -> str | None:
    if not HOSTED:
        import keyring

        return keyring.get_password(service, email)
    import json

    stored = json.loads(PASSWORDS_FILE.read_text()) if PASSWORDS_FILE.exists() else {}
    return stored.get(f"{service}|{email}")


def set_password(service: str, email: str, password: str) -> None:
    if not HOSTED:
        import keyring

        keyring.set_password(service, email, password)
        return
    import json

    stored = json.loads(PASSWORDS_FILE.read_text()) if PASSWORDS_FILE.exists() else {}
    stored[f"{service}|{email}"] = password
    PASSWORDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PASSWORDS_FILE.write_text(json.dumps(stored, indent=1))
