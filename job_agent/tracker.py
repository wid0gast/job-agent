"""SQLite record of every job the agent has seen, so it never applies twice.

Jobs move through explicit states, and every status change is a conditional
UPDATE, so the dashboard and the worker (separate processes) can't race each
other into an invalid state.
"""

import functools
import json
import re
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .ats import identify

# found → filtered_out | preparing → prepared → approved → submitting → submitted.
# "working" is the older single-agent run that searches, fills and submits in one go.
TRANSITIONS = {
    "found": {"filtered_out", "preparing", "working", "skipped", "closed"},
    "filtered_out": {"found", "closed"},
    "preparing": {"prepared", "filtered_out", "needs_you", "failed", "found", "closed"},
    "prepared": {"approved", "rejected", "preparing", "closed"},
    "approved": {"submitting", "prepared", "rejected", "needs_you", "closed"},
    # Once the click may have happened, only a confirmed result or you can resolve it.
    "submitting": {"submitted", "needs_you"},
    "needs_you": {"found", "preparing", "prepared", "rejected", "skipped", "submitted", "failed"},
    "working": {"submitted", "skipped", "failed", "needs_you"},
    "failed": {"found"},
    "rejected": {"found"},
    "skipped": {"found"},
    "closed": {"found"},
    "submitted": set(),
}
STATUSES = tuple(TRANSITIONS)
RENAMED = {"lead": "found", "in_progress": "working", "needs_user": "needs_you"}
# Approved but not yet sent: these already hold a slot against the caps.
RESERVED = ("approved", "submitting")
# Query parameters that identify a posting on common boards and ATSs.
ID_PARAMS = ("currentjobid", "gh_jid", "jobid", "job_id", "jk", "id", "postingid")
COLUMNS = {
    "company_id": "TEXT", "ats": "TEXT", "ats_board": "TEXT", "ats_job_id": "TEXT",
    "job_type": "TEXT", "posted_at": "TEXT", "fit_score": "INTEGER",
    "packet_dir": "TEXT", "approved_at": "TEXT", "details": "TEXT", "match_key": "TEXT",
    # The employer's side, once applied: see OUTCOMES.
    "outcome": "TEXT", "outcome_at": "TEXT", "applied_via": "TEXT", "applied_date_known": "INTEGER",
}
# What happened after you applied. "applied" means no answer yet.
OUTCOMES = ("applied", "oa", "interview", "offer", "rejected", "withdrawn", "no_response")
# Statuses a job can leave when you record that you applied outside the agent.
NOT_APPLIED = ("found", "filtered_out", "closed", "prepared", "approved", "needs_you", "rejected",
               "skipped", "failed")


def job_key(url: str) -> str:
    """A stable identity for a posting URL, ignoring tracking parameters."""
    posting = identify(url)
    if posting.key:
        return posting.key
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    path = parts.path.rstrip("/")
    if "linkedin.com" in host:
        match = re.search(r"/jobs/view/(?:[^/]*?-)?(\d+)", path)
        query_id = parse_qs(parts.query).get("currentJobId", [None])[0]
        if match or query_id:
            return f"linkedin:{match.group(1) if match else query_id}"
    query = {k.lower(): v[0] for k, v in parse_qs(parts.query).items()}
    ids = [f"{k}={query[k]}" for k in ID_PARAMS if k in query]
    return f"{host}{path}" + (f"?{'&'.join(ids)}" if ids else "")


def normalize(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


# Shorthand people and postings use for the same role.
ABBREVIATIONS = {
    "mle": "machine learning engineer", "ml": "machine learning", "ds": "data science",
    "swe": "software engineer", "sde": "software development engineer", "sre": "site reliability engineer",
    "pm": "product manager", "apm": "associate product manager", "qa": "quality assurance",
    "scientist": "science", "scientists": "science", "engineering": "engineer", "engineers": "engineer",
    "developer": "development", "sr": "senior", "jr": "junior",
    # An internship is a different job from the full-time role with the same title.
    "interns": "intern", "internship": "intern", "internships": "intern", "coop": "intern",
}
# Words that don't change which role it is: internship wording, seasons, years, degree tags.
NOISE = set("""summer fall spring winter autumn ms msc ma mba phd bs ba bsc masters master bachelor
bachelors undergraduate undergrad graduate grad new student students program the a an of and for in at to""".split())
MATCH_VERSION = "v2"


def title_tokens(title: str | None) -> list[str]:
    """A role's words with shorthand expanded and season, year and degree tags dropped."""
    words = []
    for word in re.sub(r"\bco op\b", "coop", normalize(title)).split():
        words += ABBREVIATIONS.get(word, word).split()
    return [w for w in words if w not in NOISE and not re.fullmatch(r"(19|20)\d\d|\d{1,2}", w)]


def match_key(company: str | None, title: str | None) -> str | None:
    """Same company (by ID where known) and the same role, however each board words it."""
    tokens = title_tokens(title)
    return f"{MATCH_VERSION}:{normalize(company)}|{' '.join(tokens)}" if company and tokens else None


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def utc_days_ago(days: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")


def local_midnight_utc() -> str:
    """Start of today in your time zone, as a UTC timestamp comparable with stored ones."""
    midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(UTC).isoformat(timespec="seconds")


def locked(method):
    """The hosted app and dashboard read the tracker from server threads."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)

    return wrapper


class Tracker:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
              id INTEGER PRIMARY KEY,
              job_key TEXT UNIQUE NOT NULL,
              url TEXT NOT NULL,
              site TEXT,
              company TEXT,
              title TEXT,
              location TEXT,
              status TEXT NOT NULL,
              reason TEXT,
              notes TEXT,
              resume_path TEXT,
              cover_letter_path TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              submitted_at TEXT
            );
            CREATE TABLE IF NOT EXISTS accounts (
              domain TEXT PRIMARY KEY,
              email TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY,
              job_id INTEGER NOT NULL,
              at TEXT NOT NULL,                -- when it happened (your date, or now)
              kind TEXT NOT NULL,              -- applied, an outcome, or note
              note TEXT,
              recorded_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
              id INTEGER PRIMARY KEY,
              kind TEXT NOT NULL,
              job_id INTEGER,
              status TEXT NOT NULL,          -- queued, running, waiting, done, failed
              result TEXT,
              question TEXT,                 -- set while the worker waits on you
              answer TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            """
        )
        if "arg" not in {row["name"] for row in self.db.execute("PRAGMA table_info(tasks)")}:
            self.db.execute("ALTER TABLE tasks ADD COLUMN arg TEXT")
        existing = {row["name"] for row in self.db.execute("PRAGMA table_info(jobs)")}
        for name, kind in COLUMNS.items():
            if name not in existing:
                self.db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {kind}")
        self.db.execute("CREATE INDEX IF NOT EXISTS jobs_match ON jobs (match_key)")
        self.db.execute("CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status)")
        for row in self.db.execute(  # Keys from an older matching rule are redone.
                "SELECT id, company, company_id, title FROM jobs WHERE match_key IS NULL "
                "OR match_key NOT LIKE ?", (f"{MATCH_VERSION}:%",)).fetchall():
            self.db.execute("UPDATE jobs SET match_key = ? WHERE id = ?",
                            (match_key(row["company_id"] or row["company"], row["title"]), row["id"]))
        for old, new in RENAMED.items():
            self.db.execute("UPDATE jobs SET status = ? WHERE status = ?", (new, old))
        self.db.commit()

    # ----- jobs --------------------------------------------------------------

    @locked
    def find_duplicate(self, url: str, company: str | None, title: str | None, company_id: str | None = None):
        """Same posting (system job ID or URL), or the same company and role on another board.

        Two postings on the same system with different job IDs are different
        jobs even when the titles match (one role in several offices).
        """
        key = job_key(url)
        row = self.db.execute("SELECT * FROM jobs WHERE job_key = ?", (key,)).fetchone()
        if row:
            return dict(row)
        same = match_key(company_id or company, title)
        if not same:
            return None
        system = key.split(":", 1)[0] if identify(url).key else None
        for row in self.db.execute(
            "SELECT * FROM jobs WHERE match_key = ? AND status NOT IN ('skipped', 'closed')",
            (same,),
        ):
            if system and row["job_key"].startswith(f"{system}:"):
                continue
            return dict(row)
        return None

    @locked
    def add_lead(self, url, site, company, title, location, **extra) -> tuple[bool, dict | None]:
        duplicate = self.find_duplicate(url, company, title)
        if duplicate:
            return False, duplicate
        posting = identify(url)
        details = extra.pop("details", None)
        fields = {
            "job_key": job_key(url), "url": url, "site": site, "company": company,
            "title": title, "location": location, "status": "found",
            "ats": posting.ats, "ats_board": posting.board, "ats_job_id": posting.job_id,
            "details": json.dumps(details) if details is not None else None,
            "match_key": match_key(extra.get("company_id") or company, title),
            "created_at": now(), "updated_at": now(), **extra,
        }
        self.db.execute(
            f"INSERT INTO jobs ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})",
            tuple(fields.values()),
        )
        self.db.commit()
        return True, None

    @locked
    def likely_applied(self, job: dict) -> dict | None:
        """An application you already sent at the same company for what looks like this role:
        one title's words contain the other's ("MLE" and "Machine Learning Engineer Intern")."""
        mine = set(title_tokens(job.get("title")))
        if len(mine) < 2:
            return None
        company = normalize(job.get("company_id") or job.get("company"))
        for row in self.db.execute(
                "SELECT * FROM jobs WHERE id != ? AND (status = 'submitted' OR applied_via IS NOT NULL)",
                (job["id"],)):
            if normalize(row["company_id"] or row["company"]) != company:
                continue
            theirs = set(title_tokens(row["title"]))
            if len(theirs) >= 2 and (theirs <= mine or mine <= theirs):
                return dict(row)
        return None

    @locked
    def get(self, job_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    @locked
    def by_key(self, key: str) -> dict | None:
        row = self.db.execute("SELECT * FROM jobs WHERE job_key = ?", (key,)).fetchone()
        return dict(row) if row else None

    @locked
    def leads(self, limit: int, since: str = "") -> list[dict]:
        """Jobs not yet worked on, optionally only those found at or after `since`."""
        rows = self.db.execute(
            "SELECT * FROM jobs WHERE status = 'found' AND created_at >= ?"
            " ORDER BY id LIMIT ?",
            (since, limit),
        )
        return [dict(row) for row in rows]

    @locked
    def with_status(self, *statuses: str, order: str = "id") -> list[dict]:
        marks = ", ".join("?" * len(statuses))
        rows = self.db.execute(
            f"SELECT * FROM jobs WHERE status IN ({marks}) ORDER BY {order}", statuses
        )
        return [dict(row) for row in rows]

    @locked
    def update(self, job_id: int, **fields) -> None:
        """Change fields. A status change must be an allowed move from the current one."""
        status = fields.pop("status", None)
        if status is not None:
            current = self.get(job_id)
            if current is None:
                raise ValueError(f"No job {job_id}.")
            self.move(job_id, current["status"], status, **fields)
            return
        fields["updated_at"] = now()
        if "details" in fields and not isinstance(fields["details"], str):
            fields["details"] = json.dumps(fields["details"])
        columns = ", ".join(f"{name} = ?" for name in fields)
        self.db.execute(
            f"UPDATE jobs SET {columns} WHERE id = ?", (*fields.values(), job_id)
        )
        self.db.commit()

    @locked
    def move(self, job_id: int, expected: str | tuple, to: str, **fields) -> None:
        """Atomically change status, only if the job is still in `expected`."""
        if to not in TRANSITIONS:
            raise ValueError(f"Unknown status {to!r}.")
        expected = (expected,) if isinstance(expected, str) else tuple(expected)
        allowed = [s for s in expected if to in TRANSITIONS.get(s, ())]
        if not allowed:
            raise ValueError(f"Can't move a job from {'/'.join(expected)} to {to}.")
        fields = {"status": to, "updated_at": now(), **fields}
        if to == "submitted":
            fields["submitted_at"] = fields["updated_at"]
            fields.setdefault("applied_via", "agent")
            fields.setdefault("applied_date_known", 1)
            fields.setdefault("outcome", "applied")
            fields.setdefault("outcome_at", fields["updated_at"])
        if to == "approved":
            fields["approved_at"] = fields["updated_at"]
        if "details" in fields and not isinstance(fields["details"], str):
            fields["details"] = json.dumps(fields["details"])
        columns = ", ".join(f"{name} = ?" for name in fields)
        marks = ", ".join("?" * len(allowed))
        cursor = self.db.execute(
            f"UPDATE jobs SET {columns} WHERE id = ? AND status IN ({marks})",
            (*fields.values(), job_id, *allowed),
        )
        self.db.commit()
        if cursor.rowcount == 1 and to == "submitted":
            self.event(job_id, "applied", fields["submitted_at"], f"Applied ({fields['applied_via']}).")
        if cursor.rowcount != 1:
            current = self.get(job_id)
            raise ValueError(
                f"Job {job_id} is {current['status'] if current else 'missing'}, "
                f"not {'/'.join(allowed)}; it changed elsewhere."
            )

    # ----- applications and outcomes ----------------------------------------

    @locked
    def mark_applied(self, job_id: int, at: str | None, via: str, note: str = "") -> None:
        """Record that you applied (outside the agent, or before it existed).

        Without a date, today is used and marked unknown, so the application
        still counts toward company caps (the safe side); set the real date later.
        """
        stamp = at or now()
        cursor = self.db.execute(
            f"UPDATE jobs SET status = 'submitted', submitted_at = ?, applied_via = ?,"
            f" applied_date_known = ?, outcome = COALESCE(outcome, 'applied'), outcome_at = ?,"
            f" updated_at = ? WHERE id = ? AND status IN ({', '.join('?' * len(NOT_APPLIED))})",
            (stamp, via, 1 if at else 0, stamp, now(), job_id, *NOT_APPLIED))
        self.db.commit()
        if cursor.rowcount != 1:
            raise ValueError(f"Job {job_id} is already applied or being submitted.")
        self.event(job_id, "applied", stamp, note or f"Applied ({via}).")

    @locked
    def set_outcome(self, job_id: int, outcome: str, at: str | None = None, note: str = "") -> None:
        if outcome not in OUTCOMES:
            raise ValueError(f"Outcome must be one of {', '.join(OUTCOMES)}.")
        job = self.get(job_id)
        if job is None or job["status"] != "submitted":
            raise ValueError("Outcomes are for jobs you've applied to.")
        stamp = at or now()
        self.db.execute("UPDATE jobs SET outcome = ?, outcome_at = ?, updated_at = ? WHERE id = ?",
                        (outcome, stamp, now(), job_id))
        self.db.commit()
        self.event(job_id, outcome, stamp, note)

    @locked
    def set_applied_date(self, job_id: int, at: str) -> None:
        self.db.execute("UPDATE jobs SET submitted_at = ?, applied_date_known = 1, updated_at = ?"
                        " WHERE id = ? AND status = 'submitted'", (at, now(), job_id))
        self.db.commit()
        self.event(job_id, "note", now(), f"Applied date set to {at[:10]}.")

    @locked
    def event(self, job_id: int, kind: str, at: str, note: str = "") -> None:
        self.db.execute("INSERT INTO events (job_id, at, kind, note, recorded_at) VALUES (?, ?, ?, ?, ?)",
                        (job_id, at, kind, note, now()))
        self.db.commit()

    @locked
    def events(self, job_id: int) -> list[dict]:
        rows = self.db.execute("SELECT * FROM events WHERE job_id = ? ORDER BY at, id", (job_id,))
        return [dict(r) for r in rows]

    @locked
    def contacted_today(self) -> int:
        """Emails to people at companies (first ones and follow-ups) since local midnight."""
        return self.db.execute(
            "SELECT COUNT(*) FROM events WHERE kind IN ('contacted', 'followed_up') AND at >= ?",
            (local_midnight_utc(),)).fetchone()[0]

    @locked
    def contacted_jobs(self) -> list[int]:
        rows = self.db.execute("SELECT DISTINCT job_id FROM events WHERE kind = 'contacted' ORDER BY job_id")
        return [r["job_id"] for r in rows]

    @locked
    def applications(self) -> list[dict]:
        rows = self.db.execute("SELECT * FROM jobs WHERE status = 'submitted' ORDER BY submitted_at DESC")
        return [dict(r) for r in rows]

    # ----- caps --------------------------------------------------------------

    @locked
    def submitted_today(self) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM jobs WHERE status = 'submitted' AND submitted_at >= ?",
            (local_midnight_utc(),),
        ).fetchone()[0]

    @locked
    def reserved_today(self) -> int:
        """Submitted today plus approved ones waiting to go out."""
        marks = ", ".join("?" * len(RESERVED))
        waiting = self.db.execute(
            f"SELECT COUNT(*) FROM jobs WHERE status IN ({marks})", RESERVED
        ).fetchone()[0]
        return self.submitted_today() + waiting

    @locked
    def company_usage(self, company_id: str, days: int, exclude_job: int | None = None) -> int:
        """Applications to a company in the last `days`, counting approved ones not yet sent."""
        marks = ", ".join("?" * len(RESERVED))
        return self.db.execute(
            "SELECT COUNT(*) FROM jobs WHERE company_id = ? AND id != ? AND"
            f" (status IN ({marks}) OR (status = 'submitted' AND submitted_at >= ?))",
            (company_id, exclude_job or -1, *RESERVED, utc_days_ago(days)),
        ).fetchone()[0]

    @locked
    def all(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM jobs ORDER BY id")]

    # ----- accounts ----------------------------------------------------------

    @locked
    def record_account(self, domain: str, email: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO accounts (domain, email, created_at) VALUES (?, ?, ?)",
            (domain, email, now()),
        )
        self.db.commit()


    # ----- tasks (dashboard → worker) ----------------------------------------

    @locked
    def add_task(self, kind: str, job_id: int | None = None, arg: str | None = None) -> int:
        active = self.db.execute(
            "SELECT id FROM tasks WHERE kind = ? AND job_id IS ? AND arg IS ? AND status IN"
            " ('queued', 'running', 'waiting')", (kind, job_id, arg),
        ).fetchone()
        if active:
            return active["id"]
        cursor = self.db.execute(
            "INSERT INTO tasks (kind, job_id, arg, status, created_at, updated_at)"
            " VALUES (?, ?, ?, 'queued', ?, ?)", (kind, job_id, arg, now(), now()),
        )
        self.db.commit()
        return cursor.lastrowid

    @locked
    def claim_task(self, kinds: tuple[str, ...] = (), exclude: tuple[str, ...] = ()) -> dict | None:
        """The oldest queued task, optionally only of `kinds` or of none of `exclude`."""
        where, args = "status = 'queued'", []
        if kinds:
            where += f" AND kind IN ({','.join('?' * len(kinds))})"
            args += kinds
        if exclude:
            where += f" AND kind NOT IN ({','.join('?' * len(exclude))})"
            args += exclude
        row = self.db.execute(f"SELECT * FROM tasks WHERE {where} ORDER BY id LIMIT 1", args).fetchone()
        if not row:
            return None
        cursor = self.db.execute(
            "UPDATE tasks SET status = 'running', updated_at = ? WHERE id = ? AND status = 'queued'",
            (now(), row["id"]),
        )
        self.db.commit()
        return dict(row) if cursor.rowcount == 1 else None

    @locked
    def set_task(self, task_id: int, **fields) -> None:
        fields["updated_at"] = now()
        columns = ", ".join(f"{name} = ?" for name in fields)
        self.db.execute(f"UPDATE tasks SET {columns} WHERE id = ?", (*fields.values(), task_id))
        self.db.commit()

    @locked
    def task(self, task_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    @locked
    def tasks(self, limit: int = 30) -> list[dict]:
        rows = self.db.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    @locked
    def recover(self) -> list[str]:
        """After a crash: re-queue nothing that might have been sent."""
        notes = []
        stale = self.db.execute(
            "SELECT id FROM tasks WHERE status IN ('running', 'waiting')").fetchall()
        for row in stale:
            self.set_task(row["id"], status="failed", result="The worker stopped during this task.")
        for job in self.with_status("submitting"):
            self.move(job["id"], "submitting", "needs_you", reason=(
                "The worker stopped while submitting. Check the site or your email "
                "to see whether it went through, then mark it."))
            notes.append(f"job {job['id']} was mid-submit")
        for job in self.with_status("preparing"):
            self.move(job["id"], "preparing", "found", reason="Preparation was interrupted.")
        return notes
