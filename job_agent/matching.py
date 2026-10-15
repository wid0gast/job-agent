"""Match scores: a quick ranking of every found job against what you're looking for.

Fit scores (prepare) read the whole posting and cost one model call per job;
a free key allows about 50 calls a day. Match scores rank by title, company
and location only, about 60 jobs per call, so the whole list can be ranked
cheaply. A job's score is redone when your description or background changes.
"""

import hashlib
import json

from . import config
from .prompts import MATCH_PROMPT

BATCH = 60
SCHEMA = {
    "type": "object",
    "properties": {"scores": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "score": {"type": "integer"},
                       "why": {"type": "string"}},
        "required": ["id", "score", "why"], "additionalProperties": False}}},
    "required": ["scores"],
    "additionalProperties": False,
}


def basis(profile: dict, resume: str) -> str:
    """What a match score depends on; when it changes, scores are stale."""
    prefs = profile.get("preferences", {})
    text = json.dumps([prefs.get("description", ""), prefs.get("roles", []), resume[:3000]])
    return hashlib.sha1(text.encode()).hexdigest()[:12]


def stale(tracker, profile: dict, resume: str) -> list[dict]:
    current = basis(profile, resume)
    return [job for job in tracker.with_status("found", "prepared", "needs_you")
            if json.loads(job["details"] or "{}").get("match", {}).get("basis") != current]


def score(llm, tracker, limit: int | None = None, log=print) -> dict:
    profile, resume = config.load_profile(), config.read_master_resume()
    prefs = profile.get("preferences", {})
    if not prefs.get("description") and not prefs.get("roles"):
        raise ValueError("Describe what you're looking for on the Search page first.")
    todo = stale(tracker, profile, resume)
    # Jobs the agent can apply to first, newest first, so a small budget goes furthest.
    from .systems import SUPPORTED

    todo.sort(key=lambda j: (j["ats"] not in SUPPORTED, -(_epoch(j["posted_at"]))))
    if limit:
        todo = todo[:limit]
    current = basis(profile, resume)
    wanted = {"looking_for": prefs.get("description", ""), "roles": prefs.get("roles", []),
              "background (resume start)": resume[:2500]}
    scored = 0
    for start in range(0, len(todo), BATCH):
        batch = todo[start:start + BATCH]
        jobs = [{"id": j["id"], "title": j["title"], "company": j["company"],
                 "location": (j["location"] or "")[:80], "type": j["job_type"]} for j in batch]
        log(f"  scoring {len(batch)} job(s) ({start + len(batch)} of {len(todo)})")
        reply = llm.json(MATCH_PROMPT, json.dumps({"candidate": wanted, "jobs": jobs},
                                                  ensure_ascii=False), SCHEMA, "match_scores")
        by_id = {item["id"]: item for item in reply["scores"]}
        for job in batch:
            item = by_id.get(job["id"])
            if item is None:
                continue
            details = json.loads(job["details"] or "{}")
            details["match"] = {"score": max(0, min(100, int(item["score"]))),
                                "why": item["why"][:120], "basis": current}
            tracker.update(job["id"], details=details)
            scored += 1
    return {"scored": scored, "remaining": len(stale(tracker, profile, resume))}


def _epoch(stamp: str | None) -> float:
    from datetime import datetime

    try:
        return datetime.fromisoformat((stamp or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def ranking_score(job: dict) -> tuple[int | None, str]:
    """(score, kind): the fit score when prepared, else the match score."""
    if job.get("fit_score") is not None:
        return job["fit_score"], "fit"
    match = json.loads(job["details"] or "{}").get("match")
    return (match["score"], "match") if match else (None, "")
