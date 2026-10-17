"""Hard filters, in code, before any model sees a job.

Each returns a reason when the job is out, so the dashboard can show why.
Preferences come from profile.yaml; anything missing means "no filter".
"""

import re
from datetime import UTC, datetime, timedelta

# Common spellings on job lists, so "New York, NY" matches "NYC".
PLACE_ALIASES = {
    "new york": ("nyc", "new york", "manhattan", "brooklyn"),
    "san francisco": ("sf", "san francisco", "bay area"),
    "los angeles": ("la", "los angeles"),
    "washington": ("dc", "washington"),
}
SENIOR = re.compile(r"\b(senior|sr\.?|staff|principal|lead|manager|director|head of|vp)\b", re.I)
DEGREES = {
    "bachelor": "Bachelor's", "b.s": "Bachelor's", "b.a": "Bachelor's", "bs ": "Bachelor's",
    "master": "Master's", "m.s": "Master's", "m.eng": "Master's", "mba": "MBA",
    "ph.d": "PhD", "phd": "PhD", "doctor": "PhD", "associate": "Associate's",
}
# Posting text that rules out applicants who need sponsorship.
NO_SPONSORSHIP = re.compile(
    r"(not|unable to|cannot|can't|won't|will not|does not|do not)\s+(provide\s+|offer\s+|support\s+)?"
    r"(visa\s+)?sponsor|without\s+(the\s+need\s+for\s+)?(visa\s+)?sponsorship|"
    r"u\.?s\.?\s+citizen(ship)?\s+(is\s+)?required|must\s+be\s+a\s+u\.?s\.?\s+citizen|"
    r"security\s+clearance|ITAR", re.I)


def place_tokens(place: str) -> set[str]:
    city = place.split(",")[0].strip().lower()
    return set(PLACE_ALIASES.get(city, (city,)))


# "Remote in Canada", "Remote - EMEA", "Remote Spain": remote, but not from the US.
REMOTE_ELSEWHERE = re.compile(
    r"remote\s*(?:in|-|,|\(|:|–)?\s*(?=[a-z])(?!(?:in\s+)?(?:the\s+)?(?:usa|us\b|u\.s|united states|america)|"
    r"friendly|first|eligible|ok\b|okay|optional|possible|available|only|work|position|role|option)", re.I)
# A season with a year in the title: "Summer 2026 Intern", "Fall '27 Co-op".
SEASON = re.compile(r"\b(spring|summer|fall|autumn|winter)\s*'?(\d{2}|20\d\d)\b", re.I)
SEASON_ENDS = {"spring": (5, 31), "summer": (8, 31), "fall": (12, 31), "autumn": (12, 31), "winter": (3, 31)}


def title_season(title: str) -> tuple[str, datetime] | None:
    """("Summer 2026", the day that season ends) when the title names one."""
    match = SEASON.search(title or "")
    if not match:
        return None
    season, year = match.group(1).lower(), int(match.group(2)) % 100 + 2000
    month, day = SEASON_ENDS[season]
    return f"{season.title()} {year}", datetime(year, month, day, 23, 59, tzinfo=UTC)


def location_ok(job_locations: list[str], wanted: list[str]) -> bool:
    """`Remote` means remote in the US (or unspecified); list e.g. "Remote in Canada" otherwise."""
    if not wanted or any(w.lower() in ("any", "anywhere") for w in wanted):
        return True
    text = " ; ".join(job_locations).lower()
    if any(w.lower() == "remote" for w in wanted):
        if any("remote" in place.lower() and not REMOTE_ELSEWHERE.search(place)
               for place in job_locations):
            return True
    for place in wanted:
        if place.lower() == "remote":
            continue
        for token in place_tokens(place):
            if re.search(rf"(^|[^a-z]){re.escape(token)}([^a-z]|$)", text):
                return True
    return False


def degree_levels(profile: dict) -> set[str]:
    levels = set()
    for entry in profile.get("education", []):
        text = f" {entry.get('degree', '')} ".lower()
        levels |= {level for marker, level in DEGREES.items() if marker in text}
    return levels


def check(job: dict, details: dict, profile: dict) -> str | None:
    """Why this job is filtered out, or None if it passes."""
    prefs = profile.get("preferences", {})
    excluded = {c.lower() for c in prefs.get("exclude_companies", [])}
    if (job.get("company") or "").lower() in excluded:
        return "Excluded company."
    from . import sources  # (sources imports this module)

    fields = sources.chosen_fields(profile)
    if set(fields) != set(sources.ALL_FIELDS):
        category = details.get("category")
        inside = (sources.in_fields(category, [f for f in fields if f in sources.FIELDS]) if category
                  else sources.matches_field(job.get("title") or "", fields))
        if not inside:
            return f"Not in your fields{f' ({category})' if category else ''}."
    job_types = prefs.get("job_types") or ["internship", "full_time"]
    if job.get("job_type") and job["job_type"] not in job_types:
        return f"Job type {job['job_type']} not wanted."
    locations = details.get("locations") or [job.get("location") or ""]
    if not location_ok(locations, prefs.get("locations", [])):
        return f"Location: {'; '.join(locations)[:120]}."
    terms = prefs.get("terms") or []
    job_terms = [t for t in details.get("terms", []) if t and t != "N/A"]
    named = title_season(job.get("title"))
    if named and named[1] < datetime.now(UTC):
        return f"{named[0]} has passed."
    if named and not job_terms:
        job_terms = [named[0]]  # Board postings have no term field; the title says it.
    if terms and job_terms and not set(terms) & set(job_terms):
        return f"Term: {', '.join(job_terms)}."
    if (job.get("job_type") == "full_time" and not prefs.get("include_senior")
            and SENIOR.search(job.get("title") or "")):
        return "Senior role (include senior roles on the Search page)."
    for word in prefs.get("exclude_title_words", []):
        if re.search(rf"\b{re.escape(word)}\b", job.get("title") or "", re.I):
            return f"Title contains {word!r}."
    mine, wanted = degree_levels(profile), set(details.get("degrees") or [])
    if mine and wanted and not mine & wanted:
        return f"Degree: asks for {', '.join(sorted(wanted))}."
    max_age = prefs.get("max_posting_age_days")
    if max_age and job.get("posted_at"):
        posted = datetime.fromisoformat(job["posted_at"].replace("Z", "+00:00"))
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=UTC)
        if datetime.now(UTC) - posted > timedelta(days=int(max_age)):
            return f"Posted more than {max_age} days ago."
    return None


def sponsorship_problem(description: str, profile: dict) -> str | None:
    """Checked once the posting text is available (Simplify doesn't record it reliably)."""
    auth = profile.get("work_authorization", {})
    needs = auth.get("requires_sponsorship_now") or auth.get("requires_sponsorship_future")
    if not needs:
        return None
    match = NO_SPONSORSHIP.search(description or "")
    return f"Posting says: “{match.group(0)}”." if match else None
