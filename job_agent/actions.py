"""What you can do from the dashboard, the assistant or a script.

Approval is where limits are enforced for you: it refuses when a required
answer is missing, an answer contradicts your profile, or the company or
daily cap has no slot left. Approving reserves the slot.
"""

import json
import re
from datetime import UTC, datetime

import yaml

from . import answers, companies, config, matching, packets, sources
from .forms import pick_option


def approve(tracker, settings, job_id: int) -> None:
    job = tracker.get(job_id)
    packet = packets.load(job)
    if job["status"] != "prepared" or packet is None:
        raise ValueError(f"Only prepared jobs can be approved (this one is {job['status']}).")
    missing = packets.missing(packet)
    if missing:
        raise ValueError("Answer these first: " + "; ".join(a["label"] for a in missing))
    problems = packets.consistency(packet, config.load_profile())
    if problems:
        raise ValueError(" ".join(problems))
    problem = companies.cap_problem(tracker, packet["job"]["company_id"], settings, job_id)
    if problem:
        raise ValueError(problem)
    if tracker.reserved_today() >= settings.daily_submit_limit:
        raise ValueError(f"Today's {settings.daily_submit_limit} slots are taken "
                         "(submitted or approved). Approve more tomorrow.")
    packet["approved_hash"] = packets.content_hash(packet)
    packet["approved_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    packets.store(packet)
    answers.remember_approved(packet["answers"])
    tracker.move(job_id, "prepared", "approved", company_id=packet["job"]["company_id"])


def edit_answer(tracker, job_id: int, key: str, value, remember: bool = False) -> None:
    """Your edit. An approved packet goes back to prepared, since approval covers exact content."""
    job = tracker.get(job_id)
    packet = packets.load(job)
    if packet is None or job["status"] not in ("prepared", "approved", "needs_you"):
        raise ValueError(f"This job can't be edited now (it is {job['status']}).")
    answer = next((a for a in packet["answers"] if a["key"] == key), None)
    if answer is None:
        raise ValueError(f"No question {key!r} in this packet.")
    if answer["kind"] in ("select", "multiselect") and answer["options"]:
        values = value if isinstance(value, list) else [value]
        values = [v for v in values if v not in ("", None)]
        indexes = [pick_option(answer["options"], v, loose=False) for v in values]
        if None in indexes:
            raise ValueError(f"Choose one of: {', '.join(answer['options'])}")
        chosen = [answer["options"][i] for i in indexes]
        value = chosen if answer["kind"] == "multiselect" else (chosen[0] if chosen else None)
    if isinstance(value, str):  # A browser's text box sends line breaks as \r\n.
        value = value.replace("\r\n", "\n").replace("\r", "\n")
    answer.update(value=value or None, source="you", note="Edited by you.")
    packet["approved_hash"] = None
    packets.store(packet)
    if remember and value:
        path = config.PROFILE_DIR / "answers.yaml"
        saved = config.load_answers()
        saved[answer["label"]] = value
        path.write_text(yaml.safe_dump(saved, sort_keys=True, allow_unicode=True), encoding="utf-8")
    if job["status"] == "approved":
        tracker.move(job_id, "approved", "prepared", reason="Edited after approval.")
    elif job["status"] == "needs_you" and not packets.missing(packet):
        tracker.move(job_id, "needs_you", "prepared", reason=None)


def reject(tracker, job_id: int, reason: str = "Rejected by you.") -> None:
    tracker.move(job_id, ("prepared", "approved", "needs_you"), "rejected", reason=reason)


def resolve(tracker, job_id: int, outcome: str) -> None:
    """For a job that needs you: what actually happened, or send it back to the queue."""
    if outcome not in ("submitted", "failed", "found", "prepared"):
        raise ValueError("Outcome must be submitted, failed, found or prepared.")
    tracker.move(job_id, "needs_you", outcome, reason=f"Marked {outcome} by you.")


def skip(tracker, job_id: int) -> None:
    """You're not interested in a found job: set aside, with the reason; requeue undoes it."""
    tracker.move(job_id, "found", "skipped", reason="Not interested (you).")


def below_threshold(job: dict) -> bool:
    """Set aside only because its fit check scored under your threshold (other filters still apply)."""
    return job["status"] == "filtered_out" and bool(re.match(r"Fit \d+ is below your threshold", job["reason"] or ""))


def prepare_anyway(tracker, job_id: int) -> int:
    """Prepare a job whose fit scored under your threshold, reusing that fit check. Returns the task."""
    job = tracker.get(job_id)
    if job is None or not below_threshold(job):
        raise ValueError("Prepare anyway is for jobs set aside only because their fit is below your threshold.")
    details = json.loads(job["details"] or "{}")
    tracker.update(job_id, details={**details, "prepare_anyway": True})
    tracker.move(job_id, "filtered_out", "found", reason="Preparing anyway, as you chose.")
    return tracker.add_task("prepare", job_id)


def top_picks(tracker, settings, count: int) -> tuple[list[dict], list[dict]]:
    """The best-ranked found jobs to prepare: one per role and one per company (several at one
    company would use up its limit at once), none at a company already at its limit, and none
    the route says to think twice about. Returns (picks, passed over as not worth it)."""
    from . import strategy, systems

    found = [j for j in tracker.with_status("found") if j["ats"] in systems.SUPPORTED]
    advisor, picks, passed, taken, known = strategy.Advisor(tracker, settings), [], [], set(), companies.Companies()
    for job, _ in matching.grouped(found):  # One per role, best first.
        if len(picks) == count:
            break
        company_id = job["company_id"] or known.resolve(job["company"])
        if company_id in taken or companies.cap_problem(tracker, company_id, settings, job["id"]):
            continue
        advice = advisor.advise(job)
        if advice and advice["route"] == "think_twice":
            passed.append(job)
            continue
        picks.append(job)
        taken.add(company_id)
    return picks, passed


def requeue(tracker, job_id: int) -> None:
    tracker.move(job_id, ("failed", "rejected", "skipped", "closed", "filtered_out"), "found",
                 reason="Requeued by you.")



def ready_to_submit(tracker, settings, job_ids: list[int]) -> tuple[list[int], list[str]]:
    """Approve the prepared ones (approved ones stay); returns (ready ids, problems)."""
    ready, problems = [], []
    for job_id in job_ids:
        job = tracker.get(job_id)
        try:
            if job is None:
                raise ValueError("no such job")
            if job["status"] == "prepared":
                approve(tracker, settings, job_id)
            elif job["status"] != "approved":
                raise ValueError(f"it is {job['status'].replace('_', ' ')}, not prepared")
            ready.append(job_id)
        except ValueError as error:
            problems.append(f"#{job_id}: {error}")
    return ready, problems


# Search settings that can bring in jobs a refresh dropped (excluding more only narrows).
WIDENING = ("roles", "description", "job_types", "include_senior", "locations", "terms", "max_posting_age_days")


def save_search(tracker, settings, values: dict) -> tuple[str, bool]:
    """Save search settings and apply them to the jobs found. Returns (message, refresh needed)."""
    fields_before = sources.chosen_fields(config.load_profile())
    search_before = {k: config.load_search().get(k) for k in WIDENING}
    config.save_search(values)
    profile = config.load_profile()
    before = len(tracker.with_status("found"))
    sources.apply_filters(tracker, profile)
    matching.keyword_rank(tracker, profile)
    # A lower threshold brings back jobs whose fit was below the old one.
    threshold = config.fit_threshold(profile, settings)
    for job in tracker.with_status("filtered_out"):
        if (job["reason"] or "").startswith("Fit ") and (job["fit_score"] or 0) >= threshold:
            tracker.update(job["id"], status="found", reason="Back: above your new threshold.")
    if sources.chosen_fields(profile) != fields_before:
        return "Saved. Your fields changed, so the job lists are being refreshed.", True
    if any(config.load_search().get(k) != search_before[k] for k in WIDENING):
        # A refresh keeps only jobs that fit the search, so a different search reads them again.
        return "Saved. Your search changed, so the job lists are being read again for it.", True
    if not tracker.all():  # Nothing fetched yet.
        return "Saved. No jobs yet, so the job lists are being refreshed with these filters.", True
    after = len(tracker.with_status("found"))
    return f"Saved. {after} job(s) pass your filters ({after - before:+d}).", False


def save_profile(tracker, name: str, content: str) -> None:
    """Save a profile file; preferences may change which jobs pass the filters."""
    config.save_profile_file(name, content)
    if name == "profile.yaml":
        sources.apply_filters(tracker, config.load_profile())
        matching.keyword_rank(tracker, config.load_profile())
