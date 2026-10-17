"""Paths and settings. Everything personal lives under profile/ and data/."""

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
HOME = Path(os.environ.get("JOB_AGENT_HOME") or ROOT)
PROFILE_DIR = HOME / "profile"
PROFILE_EXAMPLE_DIR = ROOT / "profile.example"
DATA_DIR = HOME / "data"


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


BACKENDS = ("auto", "claude-code", "anthropic", "off")


@dataclass(frozen=True)
class Settings:
    backend: str                     # auto | claude-code | anthropic | off (see llm.client)
    model: str                       # Empty: the backend's default
    submit_mode: str
    daily_submit_limit: int
    browser_channel: str
    company_cap: int = 3
    company_window_days: int = 30
    fit_threshold: int = 60
    spend_cap_usd: float = 1.0       # Anthropic API backend only; Claude Code uses your plan
    cover_letter: str = "required"   # required | always | never
    research: str = "auto"           # auto (for letters and "why us" answers) | off

    @classmethod
    def from_env(cls, require_model: bool = True) -> "Settings":
        load_env_file()
        backend = os.environ.get("MODEL_BACKEND", "auto").strip().lower()
        if backend not in BACKENDS:
            raise SystemExit(f"MODEL_BACKEND must be one of {', '.join(BACKENDS)}.")
        mode = os.environ.get("SUBMIT_MODE", "review").strip().lower()
        if mode not in ("review", "auto"):
            raise SystemExit("SUBMIT_MODE must be review or auto.")
        return cls(
            backend=backend,
            model=os.environ.get("CLAUDE_MODEL", "").strip(),
            submit_mode=mode,
            daily_submit_limit=int(os.environ.get("DAILY_SUBMIT_LIMIT", "15")),
            browser_channel=os.environ.get("BROWSER_CHANNEL", "chrome").strip(),
            company_cap=int(os.environ.get("COMPANY_CAP", "3")),
            company_window_days=int(os.environ.get("COMPANY_WINDOW_DAYS", "30")),
            fit_threshold=int(os.environ.get("FIT_THRESHOLD", "60")),
            spend_cap_usd=float(os.environ.get("SPEND_CAP_USD", "1.00")),
            cover_letter=os.environ.get("COVER_LETTER", "required").strip(),
            research=os.environ.get("RESEARCH", "auto").strip().lower(),
        )


# A new profile starts with these templates. Your resume comes from the Resumes page;
# profile.example/resume.md (Jane Doe) is only an example.
STARTER_FILES = ("profile.yaml", "story-bank.md")


def ensure_profile() -> bool:
    """Create profile/ from the templates on first run; True if it was created."""
    if (PROFILE_DIR / "profile.yaml").exists():
        return False
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    for name in STARTER_FILES:
        if not (PROFILE_DIR / name).exists():
            (PROFILE_DIR / name).write_text((PROFILE_EXAMPLE_DIR / name).read_text(encoding="utf-8"), encoding="utf-8")
    return True


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
# Passwords for accounts the agent creates on job sites live in the macOS Keychain.


def get_password(service: str, email: str) -> str | None:
    import keyring

    return keyring.get_password(service, email)


def set_password(service: str, email: str, password: str) -> None:
    import keyring

    keyring.set_password(service, email, password)
