"""Ranking jobs against what you're looking for, cheapest first.

Keyword scores are free and instant: every found job's title (and list category)
against your role titles and description. Match scores ask the model about the
title, company and location, about 60 jobs per call, but only for the keyword
shortlist, so a long list costs a couple of calls rather than dozens. Fit scores
(prepare) read the whole posting, one call per job. A job's scores are redone
when your description or background changes.
"""

import hashlib
import json
import math
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from . import config
from .prompts import MATCH_PROMPT

BATCH = 60
SHORTLIST = 120  # Jobs the model scores per run: two calls.
PARALLEL = 2  # Model calls at once: quicker, and gentle on plan rate limits.
STOPWORDS = set("""a an and or the of for in on at to with without by from as is are be i im i'm me my we our
you your it its this that these those any all some more most less very also just not no nor but
role roles job jobs position positions work working team teams company companies opportunity
looking interested interest want would like prefer especially fine too e g eg etc ok kind kinds
internship internships intern full time fulltime new grad graduate entry level""".split())
# Spellings job titles use for the same thing.
ALIASES = {"machine learning": "ml", "artificial intelligence": "ai", "natural language processing": "nlp",
           "large language models": "llm", "large language model": "llm", "llms": "llm",
           "computer vision": "cv", "data scientist": "data science", "scientist": "science",
           "engineering": "engineer", "engineers": "engineer", "researcher": "research"}
SCHEMA = {
    "type": "object",
    "properties": {"scores": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "kind_fits": {"type": "boolean"},
                       "level_fits": {"type": "boolean"}, "score": {"type": "integer"},
                       "why": {"type": "string"}},
        "required": ["id", "kind_fits", "level_fits", "score", "why"], "additionalProperties": False}}},
    "required": ["scores"],
    "additionalProperties": False,
}


def basis(profile: dict, resume: str) -> str:
    """What a match score depends on; when it changes, scores are stale."""
    prefs = profile.get("preferences", {})
    text = json.dumps([prefs.get("description", ""), prefs.get("roles", []), prefs.get("job_types", []),
                       prefs.get("terms", []), prefs.get("locations", []), resume[:3000], MATCH_PROMPT])
    return hashlib.sha1(text.encode()).hexdigest()[:12]


def stale(tracker, profile: dict, resume: str) -> list[dict]:
    current = basis(profile, resume)
    return [job for job in tracker.with_status("found", "prepared", "needs_you")
            if json.loads(job["details"] or "{}").get("match", {}).get("basis") != current]


def terms(text: str) -> list[str]:
    text = (text or "").lower()
    for phrase, short in ALIASES.items():
        text = re.sub(rf"\b{re.escape(phrase)}\b", short, text)
    return [w for w in re.findall(r"[a-z][a-z0-9+#.]*[a-z0-9+#]|[a-z]", text) if w not in STOPWORDS]


NEGATION = re.compile(r"\b(?:not|no|never|except|avoid|excluding|don'?t|without)\b[^.;\n]*", re.I)


def query_of(profile: dict) -> tuple[Counter, set, list[str]]:
    """What a job title is scored against: words from your role titles (weighted 3) and
    description, words the description rules out, and the role titles themselves."""
    prefs = profile.get("preferences", {})
    description = prefs.get("description", "")
    unwanted = {t for clause in NEGATION.findall(description) for t in terms(clause)}
    query = Counter()
    for role in prefs.get("roles") or []:
        query.update({t: 3 for t in terms(role)})
    query.update({t: 1 for t in terms(NEGATION.sub(" ", description)) if t not in query})
    unwanted -= set(query)
    return query, unwanted, [r.lower() for r in prefs.get("roles") or [] if r.strip()]


def relevant(profile: dict):
    """A test for titles worth keeping at all: sharing a word with your role titles or
    description. One sharing none would score 0, at the bottom of the list."""
    query, _, phrases = query_of(profile)
    if not query:
        return lambda title: True
    words = set(query)
    return lambda title: bool(words & set(terms(title))) or any(p in (title or "").lower() for p in phrases)


def keyword_rank(tracker, profile: dict) -> int:
    """Free first pass: score every found job 0-100 by how well its title fits the roles and
    description. Words rare among job titles count more, role words triple, and the exact role
    title a bonus; words the description rules out ("not interested in marketing") count against.
    100 is the best job in the list. Returns how many scores changed."""
    query, unwanted, phrases = query_of(profile)
    jobs = tracker.with_status("found", "prepared", "needs_you")
    if not query or not jobs:
        return 0
    titles = {j["id"]: set(terms(j["title"])) for j in jobs}
    seen = Counter(t for words in titles.values() for t in words)
    idf = {t: math.log(1 + len(jobs) / (1 + seen[t])) for t in query}
    bonus = max(idf.values()) * 3

    def raw(job) -> float:
        words = titles[job["id"]]
        value = sum(w * idf[t] for t, w in query.items() if t in words)
        if any(p in (job["title"] or "").lower() for p in phrases):
            value += bonus  # The exact role title.
        return value / 2 if words & unwanted else value

    raws = {j["id"]: raw(j) for j in jobs}
    best = max(raws.values()) or 1
    current = hashlib.sha1(json.dumps([sorted(query.items()), sorted(unwanted), best]).encode()).hexdigest()[:12]
    changed = 0
    for job in jobs:
        details = json.loads(job["details"] or "{}")
        value = round(100 * raws[job["id"]] / best)
        if details.get("keyword") == {"score": value, "basis": current}:
            continue
        details["keyword"] = {"score": value, "basis": current}
        tracker.update(job["id"], details=details)
        changed += 1
    return changed


def keyword_score(job: dict) -> int:
    return json.loads(job["details"] or "{}").get("keyword", {}).get("score", 0)


def score(llm, tracker, limit: int | None = None, log=print) -> dict:
    """Model match scores for the best `limit` (default SHORTLIST) keyword matches not yet scored,
    PARALLEL calls at a time."""
    profile, resume = config.load_profile(), config.read_optional("resume.md") or ""  # Ranking works before a resume.
    prefs = profile.get("preferences", {})
    if not prefs.get("description") and not prefs.get("roles"):
        raise ValueError("Describe what you're looking for on the Search page first.")
    keyword_rank(tracker, profile)
    todo = stale(tracker, profile, resume)
    # Best keyword matches first; among equals, jobs the agent can apply to, newest first.
    from .systems import SUPPORTED

    todo.sort(key=lambda j: (-keyword_score(j), j["ats"] not in SUPPORTED, -(_epoch(j["posted_at"]))))
    todo = todo[:limit or SHORTLIST]
    current = basis(profile, resume)
    wanted = {"looking_for": prefs.get("description", ""), "roles": prefs.get("roles", []),
              "job_types": prefs.get("job_types", []), "seasons": prefs.get("terms", []),
              "locations": prefs.get("locations", []), "background (resume start)": resume[:2500]}
    batches = [todo[start:start + BATCH] for start in range(0, len(todo), BATCH)]
    log(f"  scoring {len(todo)} job(s) in {len(batches)} model call(s), {PARALLEL} at a time")

    def run(batch):
        jobs = [{"id": j["id"], "title": j["title"], "company": j["company"],
                 "location": (j["location"] or "")[:80], "type": j["job_type"]} for j in batch]
        reply = llm.json(MATCH_PROMPT, json.dumps({"candidate": wanted, "jobs": jobs},
                                                  ensure_ascii=False), SCHEMA, "match_scores")
        by_id = {item["id"]: item for item in reply["scores"]}
        done = 0
        for job in batch:
            item = by_id.get(job["id"])
            if item is None:
                continue
            details = json.loads(tracker.get(job["id"])["details"] or "{}")
            value = max(0, min(100, int(item["score"])))
            # Caps in code: one model said "but it's full-time" and still scored it 90.
            if item.get("kind_fits") is False:
                value = min(value, 35)
            if item.get("level_fits") is False:
                value = min(value, 45)
            details["match"] = {"score": value,
                                "why": item["why"][:120], "basis": current}
            tracker.update(job["id"], details=details)
            done += 1
        log(f"  scored {done} job(s)")
        return done

    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        scored = sum(pool.map(run, batches))
    return {"scored": scored, "remaining": len(stale(tracker, profile, resume))}


def _epoch(stamp: str | None) -> float:
    from datetime import datetime

    try:
        return datetime.fromisoformat((stamp or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def ranking_score(job: dict) -> tuple[int | None, str]:
    """(score, kind): the fit score when prepared, else the model's match score, else the keyword score."""
    if job.get("fit_score") is not None:
        return job["fit_score"], "fit"
    details = json.loads(job["details"] or "{}")
    if details.get("match"):
        return details["match"]["score"], "match"
    if details.get("keyword"):
        return details["keyword"]["score"], "keyword"
    return None, ""


def grouped(jobs: list[dict]) -> list[tuple[dict, list[dict]]]:
    """Ranked jobs with listings of the same role (same company and title: one role in several
    offices, or posted more than once) under the best-ranked one, as (job, other listings)."""
    groups: dict[str, list[dict]] = {}
    out = []
    for job in sorted(jobs, key=rank_key):
        key = job.get("match_key")
        if key and key in groups:
            groups[key].append(job)
            continue
        others: list[dict] = []
        if key:
            groups[key] = others
        out.append((job, others))
    return out


def rank_key(job: dict) -> tuple:
    """Sort key, best first: model-scored jobs, then keyword-only ones, then the rest; newest breaks ties."""
    score, kind = ranking_score(job)
    tier = 0 if kind in ("fit", "match") else 1 if kind == "keyword" else 2
    return (tier, -(score or 0), -_epoch(job["posted_at"]))
