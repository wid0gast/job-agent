"""Discover: find companies hiring for your roles on job sites you'd never browse one by one.

Most jobs, especially at startups, are on a company's own applicant-tracking page and
never reach the big job sites. The app searches those systems' sites for your role
titles and locations (research.search: Brave with BRAVE_API_KEY, else DuckDuckGo),
a few sites per query, and reads the postings straight from the results, so no model
call is spent. A model with its own web search (web_json) is used instead when there is
one. Each company board found joins the company list, so every refresh from then on
reads the whole board; the boards are read now too, and Workday postings (no board
feed) are added directly.
"""

import json
import re
import time

from . import matching, sources
from .ats import identify
from .companies import Companies

# Applicant-tracking systems and the sites their postings live on.
ATS_DOMAINS = {
    "greenhouse": ["job-boards.greenhouse.io", "boards.greenhouse.io"],
    "lever": ["jobs.lever.co"],
    "ashby": ["jobs.ashbyhq.com"],
    "workday": ["myworkdayjobs.com"],
    "smartrecruiters": ["jobs.smartrecruiters.com"],
    "workable": ["apply.workable.com"],
    "bamboohr": ["bamboohr.com"],
    "jobvite": ["jobs.jobvite.com"],
    "jazzhr": ["applytojob.com"],
    "icims": ["icims.com"],
}
MAX_SEARCHES = 10
SYSTEM = ("You find job postings with web search. Report only URLs that appeared in your search results, "
          "exactly as they appeared; never guess or construct a URL.")
SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["postings"],
    "properties": {"postings": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["url", "title", "company", "location"],
        "properties": {"url": {"type": "string"}, "title": {"type": "string"},
                       "company": {"type": "string"}, "location": {"type": "string"}}}}},
}


def searches_for(prefs: dict) -> tuple[list[str], list[str]]:
    """(roles, locations) to search for, from the Search page."""
    roles = [r for r in prefs.get("roles") or [] if r.strip()][:4]
    if not roles and prefs.get("description"):
        roles = [" ".join(prefs["description"].split()[:10])]
    if not roles:
        raise ValueError("Add role titles (or describe what you want) on the Search page first.")
    places = [p for p in prefs.get("locations") or [] if p.lower() not in ("any", "anywhere")][:3]
    return roles, places


def ask(llm, prefs: dict) -> list[dict]:
    roles, places = searches_for(prefs)
    domains = [d for group in ATS_DOMAINS.values() for d in group]
    request = {
        "roles": roles, "locations": places or ["anywhere"],
        "job_types": prefs.get("job_types") or ["internship", "full_time"],
        "job_sites": domains,
        "how": (f"Run up to {MAX_SEARCHES} searches, one per role and location (add 'remote' when Remote is "
                "listed), across all the job sites. Prefer postings at companies' own pages on these sites over "
                "aggregators. Return every posting you saw on these sites, with its title, company and location "
                "as shown in the result."),
    }
    reply = llm.web_json(SYSTEM, json.dumps(request, ensure_ascii=False), SCHEMA, "discover", domains, MAX_SEARCHES)
    return reply["postings"]


SITES_PER_QUERY = 4


def search_postings(prefs: dict, log=print) -> list[dict]:
    """Postings from web searches run here: each role and place, a few job sites per query."""
    from . import research

    roles, places = searches_for(prefs)
    sites = [group[0] for group in ATS_DOMAINS.values()]
    groups = [sites[i:i + SITES_PER_QUERY] for i in range(0, len(sites), SITES_PER_QUERY)]
    queries = [f'"{role}" {place} ({" OR ".join(f"site:{d}" for d in group)})'.replace("  ", " ")
               for role in roles for place in (places or [""]) for group in groups][:MAX_SEARCHES]
    postings, errors = [], []
    with sources.http() as client:
        for n, query in enumerate(queries):
            if n:
                time.sleep(1.5)  # Gentle on the free search.
            try:
                results = research.search(client, query)
            except Exception as error:  # noqa: BLE001 - one failed search shouldn't stop the rest.
                errors.append(str(error)[:200])
                continue
            for result in results:
                title, company = posting_title(result["title"])
                postings.append({"url": result["url"], "title": title, "company": company,
                                 "location": query.split('" ', 1)[-1].split(" (")[0].strip()})
    if not postings and errors:
        raise ValueError(f"The web search failed: {errors[0]} Set BRAVE_API_KEY for reliable searches.")
    log(f"  {len(queries)} searches, {len(postings)} results" + (f", {len(errors)} failed" if errors else ""))
    return postings


def posting_title(text: str) -> tuple[str, str]:
    """(role, company) from a search result's title, as the job sites write them."""
    text = re.sub(r"\s+", " ", text).strip()
    match = re.match(r"(?:Job Application for )?(.+?) at (.+?)(?: - .*)?$", text)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    parts = [p.strip() for p in re.split(r" [-|–] ", text) if p.strip()]
    if len(parts) >= 2 and len(parts[0].split()) <= 3 and len(parts[1].split()) > 1:  # "Acme - Data Engineer"
        return parts[1], parts[0]
    return (parts[0] if parts else text), ""


def run(llm, tracker, profile: dict, log=print) -> dict:
    """Search, then add the companies and jobs found. Returns counts."""
    searches_web = llm is not None and getattr(llm, "searches_web", True)
    prefs = profile.get("preferences", {})
    fields = sources.chosen_fields(profile)
    log("  searching job sites for your roles")
    postings = ask(llm, prefs) if searches_web else search_postings(prefs, log)
    companies = Companies()
    counts = {"postings": len(postings), "new_boards": 0, "boards": 0, "new": 0, "closed": 0, "seen": 0}
    boards, direct = {}, []
    for item in postings:
        posting = identify(item["url"])
        if posting.ats not in ATS_DOMAINS or not posting.board:
            continue
        owner = companies.by_board(posting.ats, posting.board)  # "DoorDash USA" is DoorDash's board.
        name = (companies.name(owner) if owner else
                (item.get("company") or "").strip() or posting.board.replace("-", " ").title())
        known = companies.rows.get(companies.resolve(name), {})
        company_id = companies.record(name, ats=posting.ats, board=posting.board, source="discover")
        row = companies.rows[company_id]
        if row.get("ats") == posting.ats and row.get("board") == posting.board:
            counts["new_boards"] += not known.get("board")
            if posting.ats in sources.BOARD_FEEDS:
                boards[(posting.ats, posting.board)] = row
        if posting.job_id and item.get("title") and sources.matches_field(item["title"], fields):
            direct.append(sources.lead("discover", item["url"], name, item["title"], [item.get("location", "")],
                                       posting.board, posting.job_id))
    log(f"  {counts['postings']} postings found; {counts['new_boards']} new company boards")
    jobs = []
    if boards:
        with sources.http() as client:
            jobs = sources.read_boards(client, [(ats, row) for (ats, _), row in boards.items()],
                                       fields, counts, log)
    # Board feeds first: they carry exact locations and dates; the search's own copies of the
    # same postings are then recognized as already known.
    sources.ingest(tracker, companies, jobs + direct, fields, counts)
    companies.save()
    sources.apply_filters(tracker, profile)
    matching.keyword_rank(tracker, profile)
    return counts
