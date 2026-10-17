"""The master job list: Simplify's GitHub lists plus company boards.

Simplify publishes its lists as JSON (.github/scripts/listings.json), so no
table parsing. Downloads use ETags, so an unchanged list costs one request.
"""

import html
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from . import config, filters
from .ats import identify
from .tracker import job_key
from .companies import Companies, reassign

RAW = "https://raw.githubusercontent.com/SimplifyJobs/{repo}/dev/.github/scripts/listings.json"
LISTS = {
    "simplify-intern": ("Summer2027-Internships", "internship"),
    "simplify-newgrad": ("New-Grad-Positions", "full_time"),
}
GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards"
CACHE = config.DATA_DIR / "cache"
# Simplify's category names have varied between list versions.
FIELD_WORDS = {"ai_ml_data": ("ai", "machine learning", "data")}


def http():
    import httpx

    return httpx.Client(timeout=30, follow_redirects=True,
                        headers={"User-Agent": "job-agent (personal job search)"})


def fetch_cached(client, url: str, name: str):
    """GET with If-None-Match; returns the parsed JSON (from cache when unchanged)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    body, etag = CACHE / f"{name}.json", CACHE / f"{name}.etag"
    headers = {"If-None-Match": etag.read_text()} if etag.exists() and body.exists() else {}
    response = client.get(url, headers=headers)
    if response.status_code == 304:
        return json.loads(body.read_text(encoding="utf-8"))
    response.raise_for_status()
    body.write_bytes(response.content)
    if response.headers.get("etag"):
        etag.write_text(response.headers["etag"])
    return response.json()


def in_fields(category: str, fields: list[str]) -> bool:
    words = [w for f in fields for w in FIELD_WORDS.get(f, (f.replace("_", " "),))]
    return any(re.search(rf"\b{re.escape(w)}\b", (category or "").lower()) for w in words)


def epoch_iso(value) -> str | None:
    try:
        return datetime.fromtimestamp(int(value), UTC).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return None


def simplify_jobs(client, fields: list[str]) -> list[dict]:
    jobs = []
    for site, (repo, job_type) in LISTS.items():
        for item in fetch_cached(client, RAW.format(repo=repo), site):
            if not item.get("is_visible", True) or not in_fields(item.get("category"), fields):
                continue
            jobs.append({
                "site": site, "url": item["url"], "company": item["company_name"],
                "title": item["title"], "location": "; ".join(item.get("locations") or []),
                "job_type": job_type, "posted_at": epoch_iso(item.get("date_posted")),
                "active": bool(item.get("active")),
                "details": {"source_id": item.get("id"), "category": item.get("category"),
                            "locations": item.get("locations") or [],
                            "terms": item.get("terms") or [],
                            "degrees": item.get("degrees") or [],
                            "sponsorship": item.get("sponsorship")},
            })
    return jobs


def greenhouse_board(client, token: str) -> list[dict]:
    """Every open job on one company's Greenhouse board."""
    response = client.get(f"{GREENHOUSE_API}/{token}/jobs", params={"content": "true"})
    if response.status_code == 404:
        return []
    response.raise_for_status()
    jobs = []
    for item in response.json().get("jobs", []):
        jobs.append({
            "site": "greenhouse", "url": item["absolute_url"],
            "company": item.get("company_name") or token, "title": item["title"],
            "location": (item.get("location") or {}).get("name", ""),
            "posted_at": item.get("first_published") or item.get("updated_at"),
            "active": True, "job_type": guess_job_type(item["title"]),
            "details": {"locations": [(item.get("location") or {}).get("name", "")],
                        "description": html_to_text(item.get("content", ""))[:20000]},
            "board": token, "job_id": str(item["id"]),
        })
    return jobs


def lever_board(client, slug: str) -> list[dict]:
    response = client.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
    if response.status_code == 404:
        return []
    response.raise_for_status()
    jobs = []
    for item in response.json():
        place = (item.get("categories") or {}).get("location") or ""
        commitment = (item.get("categories") or {}).get("commitment") or ""
        jobs.append({
            "site": "lever", "url": item["hostedUrl"], "company": slug, "title": item["text"],
            "location": place, "active": True,
            "posted_at": epoch_iso((item.get("createdAt") or 0) // 1000),
            "job_type": "internship" if re.search(r"intern", f"{commitment} {item['text']}", re.I)
            else "full_time",
            "details": {"locations": (item.get("categories") or {}).get("allLocations") or [place]},
            "board": slug, "job_id": item["id"],
        })
    return jobs


def ashby_board(client, name: str) -> list[dict]:
    response = client.get(f"https://api.ashbyhq.com/posting-api/job-board/{name}")
    if response.status_code == 404:
        return []
    response.raise_for_status()
    jobs = []
    for item in response.json().get("jobs", []):
        if not item.get("isListed", True):
            continue
        places = [item.get("location") or ""] + [
            s.get("location", "") for s in item.get("secondaryLocations") or []]
        if item.get("isRemote"):
            places.append("Remote")
        jobs.append({
            "site": "ashby", "url": item["jobUrl"], "company": name, "title": item["title"],
            "location": "; ".join(p for p in places if p), "active": True,
            "posted_at": item.get("publishedAt"),
            "job_type": "internship" if item.get("employmentType") == "Intern"
            or guess_job_type(item["title"]) == "internship" else "full_time",
            "details": {"locations": [p for p in places if p]},
            "board": name, "job_id": item["id"],
        })
    return jobs


BOARD_FEEDS = {"greenhouse": greenhouse_board, "lever": lever_board, "ashby": ashby_board}


def guess_job_type(title: str) -> str:
    return "internship" if re.search(r"\bintern(ship)?\b|co-?op", title, re.I) else "full_time"


def html_to_text(content: str) -> str:
    """Greenhouse sends posting HTML entity-escaped."""
    text = html.unescape(html.unescape(content or ""))
    text = re.sub(r"<(br|/p|/li|/h\d|/div)[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"<li[^>]*>", "- ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def job_ats(job: dict) -> str:
    return identify(job["url"]).ats


def board_of(job: dict) -> str | None:
    return identify(job["url"]).board


def matches_field(title: str, fields: list[str]) -> bool:
    """Board feeds have no category, so match AI/ML/data roles by title."""
    if "ai_ml_data" not in fields:
        return True
    return bool(re.search(
        r"\b(machine learning|ml|ai|artificial intelligence|data|deep learning|nlp|"
        r"computer vision|research scientist|applied scientist|llm|analytics)\b", title, re.I))


def refresh(tracker, profile: dict, boards: bool = False, log=print) -> dict:
    """Pull the lists into the tracker. New jobs are 'found'; closed ones drop out."""
    fields = profile.get("preferences", {}).get("fields") or ["ai_ml_data"]
    companies = Companies()
    counts = {"new": 0, "closed": 0, "seen": 0, "boards": 0}
    with http() as client:
        jobs = simplify_jobs(client, fields)
        log(f"Simplify lists: {len(jobs)} {'/'.join(fields)} postings")
        if boards:
            # Company names from the lists are better than board slugs.
            names = {(job_ats(j), board_of(j)): j["company"] for j in jobs}
            def read(ats_row):
                ats, row = ats_row
                try:
                    return ats, row, BOARD_FEEDS[ats](client, row["board"]), None
                except Exception as error:  # noqa: BLE001 - one bad board shouldn't stop a refresh.
                    return ats, row, [], error

            todo = [(ats, row) for ats in BOARD_FEEDS for row in companies.boards(ats)]
            # Polite parallelism: a few boards at a time, each a single request.
            with ThreadPoolExecutor(max_workers=8) as pool:
                for ats, row, board_jobs, error in pool.map(read, todo):
                    if error:
                        log(f"  {ats} board {row['board']}: {error}")
                        continue
                    for job in board_jobs:
                        if matches_field(job["title"], fields):
                            job["company"] = names.get((ats, row["board"]), row["name"])
                            jobs.append(job)
                    counts["boards"] += 1
            log(f"Company boards read: {counts['boards']}")
    # A URL can appear twice (reposted, or on both lists); any active copy keeps it open.
    live = {job_key(j["url"]) for j in jobs if j["active"]}
    for job in jobs:
        posting = identify(job["url"])
        board = job.pop("board", None) or posting.board
        job.pop("job_id", None)
        active = job.pop("active")
        if not active:
            # Close only the exact posting; a same-titled old listing is a different job.
            key = job_key(job["url"])
            existing = None if key in live else tracker.by_key(key)
            if existing and existing["status"] in ("found", "filtered_out", "prepared", "approved"):
                tracker.update(existing["id"], status="closed", reason="Posting closed on the list.")
                counts["closed"] += 1
            continue
        company_id = companies.record(job["company"], ats=posting.ats if board else "",
                                      board=board or "", source=job["site"],
                                      tags=",".join(fields))
        if tracker.find_duplicate(job["url"], job["company"], job["title"]):
            counts["seen"] += 1
            continue
        tracker.add_lead(job.pop("url"), job.pop("site"), job.pop("company"), job.pop("title"),
                         job.pop("location"), company_id=company_id,
                         ats_board=board, **job)
        counts["new"] += 1
    companies.save()
    reassign(tracker)  # Aliases may have changed: keep caps counting by one company ID.
    counts["filtered"] = apply_filters(tracker, profile)
    return counts


def apply_filters(tracker, profile: dict) -> int:
    """Re-run the hard filters on unscored jobs, so profile changes take effect."""
    moved = 0
    for job in tracker.with_status("found", "filtered_out"):
        if job["fit_score"] is not None:
            continue  # Filtered by fit or caps, not by these rules.
        reason = filters.check(job, json.loads(job["details"] or "{}"), profile)
        if reason and job["status"] == "found":
            tracker.update(job["id"], status="filtered_out", reason=reason)
            moved += 1
        elif not reason and job["status"] == "filtered_out" and not (job["reason"] or "").startswith(
                ("Company cap", "Active-application", "Paused", "Posting says")):
            tracker.update(job["id"], status="found", reason=None)
    return moved
