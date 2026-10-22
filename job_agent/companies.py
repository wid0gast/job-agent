"""Company identities and application limits.

Caps count by company ID, so "TikTok" and "ByteDance" share one limit.
companies.csv (public, in the repo) holds IDs, aliases and job boards;
company-rules.yaml (public) holds known limits. Limits the agent learns from
pages go to data/learned-rules.yaml until you confirm them.
"""

import csv
import os
import re
from datetime import date
from pathlib import Path

import yaml

from . import config

COMPANIES_FILE = config.ROOT / "companies.csv"
RULES_FILE = config.ROOT / "company-rules.yaml"
LEARNED_FILE = config.DATA_DIR / "learned-rules.yaml"
FIELDS = ["company_id", "name", "aliases", "website", "ats", "board", "field_tags",
          "source", "added"]
LEGAL_SUFFIX = re.compile(
    r"\b(inc|llc|l\.l\.c|ltd|limited|corp|corporation|co|plc|gmbh|ag|sa|lp|llp)\b\.?", re.I)
# "Active applications" has no published window; this approximates it until
# Phase 3 tracks rejections.
ACTIVE_WINDOW_DAYS = 60


def slug(name: str) -> str:
    text = LEGAL_SUFFIX.sub("", name.lower().replace("&", " and "))
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def own_copy() -> tuple[Path, Path]:
    """(read from, save to). The bundled list, unless the app folder is read-only (the class
    platform): then companies the app adds are saved in your data folder and read from there."""
    if os.access(COMPANIES_FILE, os.W_OK):
        return COMPANIES_FILE, COMPANIES_FILE
    mine = config.DATA_DIR / "companies.csv"
    return (mine if mine.exists() else COMPANIES_FILE), mine


class Companies:
    def __init__(self, path: Path | None = None):
        source, self.path = (path, path) if path else own_copy()
        self.rows: dict[str, dict] = {}
        self.alias: dict[str, str] = {}
        if source.exists():
            with source.open(newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    self._index(row)
        self.dirty = False

    def _index(self, row: dict) -> None:
        self.rows[row["company_id"]] = row
        for name in [row["name"], *filter(None, (row.get("aliases") or "").split(";")),
                     row["company_id"]]:
            self.alias.setdefault(compact(slug(name)), row["company_id"])

    def resolve(self, name: str) -> str:
        """The company's ID. Spacing doesn't matter ("TwoSigma" is "Two Sigma")."""
        key = slug(name or "")
        return self.alias.get(compact(key), key)

    def add_alias(self, company_id: str, alias: str) -> None:
        row = self.rows[company_id]
        names = [a for a in (row.get("aliases") or "").split(";") if a]
        if alias not in names:
            row["aliases"] = ";".join(names + [alias])
            self.alias[compact(slug(alias))] = company_id
            self.dirty = True

    def name(self, company_id: str) -> str:
        return self.rows.get(company_id, {}).get("name") or company_id

    def record(self, name: str, *, ats: str = "", board: str = "", website: str = "",
               source: str = "", tags: str = "") -> str:
        """Add a company, or fill in its job board. Returns its ID."""
        company_id = self.resolve(name)
        row = self.rows.get(company_id)
        if row is None:
            row = {"company_id": company_id, "name": name, "aliases": "", "website": website,
                   "ats": ats, "board": board, "field_tags": tags, "source": source,
                   "added": date.today().isoformat()}
            self._index(row)
            self.dirty = True
        elif board and not row.get("board"):
            row.update(ats=ats, board=board)
            self.dirty = True
        return company_id

    def by_board(self, ats: str, board: str) -> str | None:
        """The company already known to own this job board, if any."""
        return next((cid for cid, r in self.rows.items()
                     if r.get("ats") == ats and (r.get("board") or "").lower() == board.lower()), None)

    def boards(self, ats: str) -> list[dict]:
        return [r for r in self.rows.values() if r.get("ats") == ats and r.get("board")]

    def save(self) -> None:
        if not self.dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        partial = self.path.with_name(self.path.name + ".partial")  # Swapped in whole.
        with partial.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            for company_id in sorted(self.rows):
                writer.writerow({k: self.rows[company_id].get(k, "") for k in FIELDS})
        partial.replace(self.path)
        self.dirty = False


def compact(key: str) -> str:
    return key.replace("-", "")


def reassign(tracker) -> int:
    """Re-file every job under its company's current ID (after aliases change)."""
    registry, changed = Companies(), 0
    for job in tracker.all():
        company_id = registry.resolve(job["company"] or "")
        if company_id and company_id != job["company_id"]:
            tracker.update(job["id"], company_id=company_id)
            changed += 1
    return changed


def load_rules() -> dict:
    rules = {}
    for path in (RULES_FILE, LEARNED_FILE):
        if path.exists():
            for company_id, rule in (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).items():
                rules.setdefault(company_id, {}).update(rule or {})
    return rules


def learn_rule(company_id: str, note: str, url: str) -> None:
    """A page said the limit was reached: pause the company until you review it."""
    learned = {}
    if LEARNED_FILE.exists():
        learned = yaml.safe_load(LEARNED_FILE.read_text(encoding="utf-8")) or {}
    learned[company_id] = {"paused": True, "note": note[:300], "source": url,
                           "confidence": "unverified", "learned": date.today().isoformat()}
    LEARNED_FILE.parent.mkdir(parents=True, exist_ok=True)
    LEARNED_FILE.write_text(yaml.safe_dump(learned, sort_keys=True), encoding="utf-8")


def cap_problem(tracker, company_id: str, settings, job_id: int | None = None,
                rules: dict | None = None) -> str | None:
    """Why another application to this company isn't allowed now, or None."""
    rule = (load_rules() if rules is None else rules).get(company_id, {})
    if rule.get("paused"):
        return f"Paused: {rule.get('note', 'the site reported a limit')}."
    limit = rule.get("max_applications", settings.company_cap)
    window = rule.get("window_days", settings.company_window_days)
    used = tracker.company_usage(company_id, window, exclude_job=job_id)
    if used >= limit:
        return f"Company cap reached: {used} of {limit} in {window} days."
    active = rule.get("max_active_applications")
    if active is not None:
        used = tracker.company_usage(company_id, ACTIVE_WINDOW_DAYS, exclude_job=job_id)
        if used >= active:
            return f"Active-application cap reached: {used} of {active}."
    return None
