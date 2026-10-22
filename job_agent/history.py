"""Your application history: import a job sheet, export a record.

Import reads any CSV with a link, company, role and a status column, matches
each row to the job lists (so the agent never prepares a job you've applied
to), and records the outcome. Rows without a status are skipped. When several
columns hold statuses (yours and a friend's), the one named after you is used.
Without a date column, applications are dated today and marked unknown, so
they still count toward company caps; set real dates on the Applications page.
"""

import csv
import re
from datetime import UTC, datetime
from pathlib import Path

from . import config
from .companies import Companies, reassign
from .tracker import OUTCOMES, Tracker, job_key

# Sheet wording → outcome.
STATUS_WORDS = [
    (r"offer", "offer"),
    (r"interview|phone screen|onsite|on-site|final round|superday|recruiter (call|screen)", "interview"),
    (r"\boa\b|online assessment|assessment|hackerrank|codesignal|coding test|take[- ]home", "oa"),
    (r"reject|declin|not selected|no longer|turned down|\bno\b", "rejected"),
    (r"withdr", "withdrawn"),
    (r"ghost|no response|no reply|silence", "no_response"),
    (r"appl|submit|sent|done|yes", "applied"),
]
DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d %b %Y", "%b %d, %Y", "%B %d, %Y",
                "%b %d %Y", "%Y/%m/%d", "%d-%m-%Y")


def outcome_of(value: str) -> str | None:
    text = (value or "").strip().lower()
    if not text:
        return None
    return next((outcome for pattern, outcome in STATUS_WORDS if re.search(pattern, text)), "applied")


def looks_like_status(value: str) -> bool:
    return any(re.search(pattern, value.lower()) for pattern, _ in STATUS_WORDS) and len(value) < 30


def parse_date(value: str) -> str | None:
    text = (value or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(hour=12, tzinfo=UTC).isoformat(timespec="seconds")
        except ValueError:
            continue
    return None


def find_column(header: list[str], pattern: str) -> int | None:
    return next((i for i, h in enumerate(header) if re.search(pattern, h, re.I)), None)


def status_column(header: list[str], rows: list[list[str]], name: str | None, chosen: str | None) -> int:
    """The column with your statuses."""
    if chosen:
        if chosen not in header:
            raise ValueError(f"No column named {chosen!r}. Columns: {', '.join(header)}")
        return header.index(chosen)
    candidates = []
    for index, title in enumerate(header):
        values = [r[index].strip() for r in rows if index < len(r) and r[index].strip()]
        if values and not re.search(r"link|url|company|role|title|note|outreach", title, re.I) and \
                sum(1 for v in values if looks_like_status(v)) / len(values) >= 0.7:
            candidates.append(index)
    if len(candidates) == 1:
        return candidates[0]
    first = (name or "").split()[0].lower() if name else ""
    for index in candidates:
        if first and header[index].strip().lower() == first:
            return index
    for index in candidates:
        if re.search(r"status|outcome|stage", header[index], re.I):
            return index
    raise ValueError("Couldn't tell which column holds your statuses"
                     + (f" (candidates: {', '.join(header[i] for i in candidates)})" if candidates else "")
                     + ". Pass --status-column.")


def import_sheet(path: Path, tracker: Tracker, status: str | None = None) -> dict:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = [r for r in csv.reader(handle) if any(cell.strip() for cell in r)]
    header, rows = [h.strip() for h in rows[0]], rows[1:]
    link = find_column(header, r"link|url")
    company = find_column(header, r"company|employer|organi[sz]ation")
    role = find_column(header, r"role|title|position|^job$")
    if link is None or company is None:
        raise ValueError(f"The sheet needs a link and a company column. Columns: {', '.join(header)}")
    profile_name = config.load_profile().get("contact", {}).get("name")
    mine = status_column(header, rows, profile_name, status)
    date = find_column(header, r"date|applied on|when")
    notes = find_column(header, r"note|comment")
    # Extra columns about you, e.g. "Jane Outreach": kept as notes.
    extras = [i for i, h in enumerate(header)
              if i != mine and h.lower().startswith(header[mine].lower() + " ")]
    registry = Companies()
    report = {"status_column": header[mine], "added": 0, "updated": 0, "unchanged": 0,
              "skipped": [], "unknown_dates": 0}

    def cell(row, index):
        return row[index].strip() if index is not None and index < len(row) else ""

    for row in rows:
        url, name, title = cell(row, link), cell(row, company), cell(row, role) or "(role not given)"
        outcome = outcome_of(cell(row, mine))
        if not url or outcome is None:
            report["skipped"].append(f"{name or url}: no status for you")
            continue
        when = parse_date(cell(row, date))
        note = "; ".join(filter(None, [cell(row, notes)] + [
            f"{header[i]}: {cell(row, i)}" for i in extras if cell(row, i)]))
        if cell(row, mine) and outcome == "applied" and not re.search(r"appl|submit", cell(row, mine), re.I):
            note = "; ".join(filter(None, [note, f"Status in sheet: {cell(row, mine)}"]))
        job = tracker.by_key(job_key(url)) or tracker.find_duplicate(url, name, title, registry.resolve(name))
        if job is None:
            tracker.add_lead(url, "imported", name, title, "", company_id=registry.resolve(name))
            job = tracker.by_key(job_key(url))
        already = job["status"] == "submitted"
        if not already:
            tracker.mark_applied(job["id"], when, "you", note or "Imported from your job sheet.")
            if not job.get("company_id"):
                tracker.update(job["id"], company_id=registry.resolve(name))
            report["added"] += 1
            report["unknown_dates"] += 0 if when else 1
        elif note and not any(e["note"] == note for e in tracker.events(job["id"])):
            tracker.event(job["id"], "note", when or job["submitted_at"], note)
        current = tracker.get(job["id"])["outcome"]
        if outcome != "applied" and current != outcome:
            tracker.set_outcome(job["id"], outcome, None, "From your job sheet.")
            report["updated"] += 1 if already else 0
        elif already:
            report["unchanged"] += 1
    reassign(tracker)
    return report


def export_sheet(path: Path, tracker: Tracker) -> int:
    rows = tracker.applications()
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Applied", "Date known", "Company", "Role", "Link", "Outcome",
                         "Outcome date", "Applied via", "History"])
        for job in rows:
            history = "; ".join(f"{e['at'][:10]} {e['kind']}" + (f": {e['note']}" if e["note"] else "")
                                for e in tracker.events(job["id"]))
            writer.writerow([(job["submitted_at"] or "")[:10], "yes" if job["applied_date_known"] else "no",
                             job["company"], job["title"], job["url"], job["outcome"] or "applied",
                             (job["outcome_at"] or "")[:10], job["applied_via"] or "", history])
    return len(rows)


def summary(tracker: Tracker) -> dict:
    """Counts by outcome, and the share that got any response."""
    apps = tracker.applications()
    counts = {o: 0 for o in OUTCOMES}
    for job in apps:
        counts[job["outcome"] or "applied"] += 1
    answered = sum(counts[o] for o in ("oa", "interview", "offer", "rejected"))
    return {"total": len(apps), "counts": counts,
            "response_rate": round(100 * answered / len(apps)) if apps else 0,
            "positive_rate": round(100 * sum(counts[o] for o in ("oa", "interview", "offer")) / len(apps))
            if apps else 0}


QUIET_DAYS = 30  # No reply after this long: most employers that answer do so well before.
HEARD = ("oa", "interview", "offer", "rejected")
POSITIVE = ("oa", "interview", "offer")


def mark_quiet(tracker: Tracker, days: int = QUIET_DAYS) -> int:
    """Applications still "applied" after `days` become "no response". One you set back to
    applied (its history has a no_response entry) is left as you set it."""
    from .tracker import utc_days_ago

    cutoff, marked = utc_days_ago(days), 0
    for job in tracker.applications():
        if ((job["outcome"] or "applied") != "applied" or not job["applied_date_known"]
                or (job["submitted_at"] or "") > cutoff
                or any(e["kind"] == "no_response" for e in tracker.events(job["id"]))):
            continue
        tracker.set_outcome(job["id"], "no_response", note=f"No reply in {days} days (marked automatically; "
                            "set it back if you're still waiting).")
        marked += 1
    return marked


def breakdown(tracker: Tracker) -> list[dict]:
    """Response rates by what you can change: fit, reaching out, how and where you applied."""
    contacted = set(tracker.contacted_jobs())

    def fit_band(job):
        score = job["fit_score"]
        return "not scored" if score is None else "fit 80+" if score >= 80 else "fit 60-79" if score >= 60 else "fit under 60"

    groups = {
        "Fit": fit_band,
        "Contacted someone": lambda job: "yes" if job["id"] in contacted else "no",
        "Applied": lambda job: {"agent": "by the agent", "import": "imported"}.get(job["applied_via"] or "",
                                                                                  "by you"),
        "System": lambda job: job["ats"] or "other",
    }
    apps = tracker.applications()
    out = []
    for name, key in groups.items():
        rows: dict[str, list[dict]] = {}
        for job in apps:
            rows.setdefault(key(job), []).append(job)
        for value, jobs in sorted(rows.items(), key=lambda kv: -len(kv[1])):
            heard = sum((j["outcome"] or "applied") in HEARD for j in jobs)
            good = sum((j["outcome"] or "applied") in POSITIVE for j in jobs)
            out.append({"group": name, "value": value, "n": len(jobs),
                        "heard": round(100 * heard / len(jobs)), "positive": round(100 * good / len(jobs))})
    return out
