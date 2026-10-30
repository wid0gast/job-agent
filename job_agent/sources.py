"""The master job list: Simplify's GitHub lists plus company boards.

You pick the fields on the Search page (FIELDS). Simplify's lists (internships and
new-grad roles) are filtered by their category; company boards list every role at
those companies, at every level, so their titles are matched instead, and "other"
keeps roles outside these fields (sales, operations, finance...).

Simplify publishes its lists as JSON (.github/scripts/listings.json), so no
table parsing. Downloads use ETags, so an unchanged list costs one request.
"""

import html
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

from . import config, filters
from .ats import identify
from .tracker import job_key, normalize
from .companies import Companies, reassign

RAW = "https://raw.githubusercontent.com/SimplifyJobs/{repo}/dev/.github/scripts/listings.json"
LISTS = {
    "simplify-intern": ("Summer2027-Internships", "internship"),
    "simplify-newgrad": ("New-Grad-Positions", "full_time"),
}
GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards"
CACHE = config.DATA_DIR / "cache"
# Field -> (label, words in Simplify's category, words in a board job's title).
# Simplify's category names have varied between list versions.
FIELDS = {
    "software": ("Software", ("software",),
                 r"software|engineer(ing)?|developer|swe|sde|back-?end|front-?end|full[- ]?stack|devops|"
                 r"site reliability|sre|platform|infrastructure|mobile|ios|android|security|cloud|web"),
    "ai_ml_data": ("Data & AI", ("ai", "machine learning", "data"),
                   r"machine learning|ml|ai|artificial intelligence|data|deep learning|nlp|computer vision|"
                   r"research scientist|applied scientist|llm|analytics"),
    "hardware": ("Hardware", ("hardware",),
                 r"hardware|electrical|embedded|firmware|asic|fpga|rf|mechanical|robotics|silicon|chip|"
                 r"circuit|semiconductor|manufacturing"),
    "quant": ("Quant", ("quant", "quantitative"), r"quant|quantitative|trader|trading"),
    "product": ("Product", ("product",), r"product|program manager|ux|ui designer|design"),
}
OTHER = "other"  # Board roles in none of the fields above.
ALL_FIELDS = [*FIELDS, OTHER]
FIELD_WORDS = {key: words for key, (_, words, _) in FIELDS.items()}


def chosen_fields(profile: dict) -> list[str]:
    """The fields on the Search page; none chosen means every field and role."""
    fields = [f for f in (profile.get("preferences", {}).get("fields") or []) if f in ALL_FIELDS]
    return fields or ALL_FIELDS


def http():
    import httpx

    # Hosted, the class proxy cuts a tunnel after 16 MiB or 90 s in total, however many requests
    # reuse it, so each request gets its own connection there.
    limits = httpx.Limits(max_keepalive_connections=0) if config.HOSTED else httpx.Limits()
    return httpx.Client(timeout=30, follow_redirects=True, limits=limits,
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
    # Without content: descriptions are read when a job is prepared. Kept for tens of thousands of
    # jobs they'd fill the class platform's workspace (and slow every refresh).
    response = client.get(f"{GREENHOUSE_API}/{token}/jobs")
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
            "details": {"locations": [(item.get("location") or {}).get("name", "")]},
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


def lead(site: str, url: str, company: str, title: str, places: list[str], board: str, job_id: str,
         posted_at: str | None = None, intern: bool = False) -> dict:
    """One board posting in the shape refresh() takes."""
    places = [p for p in places if p]
    return {"site": site, "url": url, "company": company, "title": title.strip(),
            "location": "; ".join(places), "active": True, "posted_at": posted_at,
            "job_type": "internship" if intern or guess_job_type(title) == "internship" else "full_time",
            "details": {"locations": places}, "board": board, "job_id": job_id}


def place(*parts) -> str:
    return ", ".join(p for p in parts if p)


def workable_board(client, account: str) -> list[dict]:
    """apply.workable.com/<account>: Workable's widget feed, every published job in one request.
    (Its paged v3 feed refuses a refresh's worth of boards with a 429 to retry a day later.)"""
    response = client.get(f"https://apply.workable.com/api/v1/widget/accounts/{account}")
    if response.status_code == 404:
        return []
    response.raise_for_status()
    jobs = []
    for item in response.json().get("jobs", []):
        places = [place(where.get("city"), where.get("region"), where.get("country"))
                  for where in item.get("locations") or [] if not where.get("hidden")]
        places = places or [place(item.get("city"), item.get("state"), item.get("country"))]
        if item.get("telecommuting"):
            places.append("Remote")
        jobs.append(lead("workable", f"https://apply.workable.com/{account}/j/{item['shortcode']}/", account,
                         item["title"], places, account, item["shortcode"], item.get("published_on"),
                         "intern" in (item.get("employment_type") or "").lower()))
    return jobs


def smartrecruiters_board(client, company: str) -> list[dict]:
    """jobs.smartrecruiters.com/<Company>: SmartRecruiters' public postings API."""
    jobs = []
    for offset in range(0, 1000, 100):
        response = client.get(f"https://api.smartrecruiters.com/v1/companies/{company}/postings",
                              params={"limit": 100, "offset": offset})
        if response.status_code == 404:
            return []
        response.raise_for_status()
        data = response.json()
        for item in data.get("content", []):
            where = item.get("location") or {}
            places = [where.get("fullLocation") or place(where.get("city"), where.get("region"), where.get("country"))]
            if where.get("remote"):
                places.append("Remote")
            kind = ((item.get("typeOfEmployment") or {}).get("label") or "").lower()
            jobs.append(lead("smartrecruiters", f"https://jobs.smartrecruiters.com/{company}/{item['id']}",
                             (item.get("company") or {}).get("name") or company, item["name"], places, company,
                             item["id"], item.get("releasedDate"), "intern" in kind))
        if offset + 100 >= data.get("totalFound", 0):
            break
    return jobs


def bamboohr_board(client, company: str) -> list[dict]:
    """<company>.bamboohr.com/careers: BambooHR's careers list (JSON for real BambooHR customers)."""
    response = client.get(f"https://{company}.bamboohr.com/careers/list")
    if response.status_code == 404 or "json" not in response.headers.get("content-type", ""):
        return []
    response.raise_for_status()
    jobs = []
    for item in response.json().get("result", []):
        where = item.get("atsLocation") or item.get("location") or {}
        places = [place(where.get("city"), where.get("state"), where.get("country"))]
        if item.get("isRemote"):
            places.append("Remote")
        title = item.get("jobOpeningName") or ""
        jobs.append(lead("bamboohr", f"https://{company}.bamboohr.com/careers/{item['id']}", company, title,
                         places, company, str(item["id"]),
                         intern="intern" in (item.get("employmentStatusLabel") or "").lower()))
    return jobs


def strip(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def jobvite_board(client, company: str) -> list[dict]:
    """jobs.jobvite.com/<company>/jobs: the careers page's job table (no public API)."""
    response = client.get(f"https://jobs.jobvite.com/{company}/jobs")
    if response.status_code == 404:
        return []
    response.raise_for_status()
    jobs = []
    for row in re.findall(r"<tr>(.*?)</tr>", response.text, re.S):
        link = re.search(rf'href="/{re.escape(company)}/job/([A-Za-z0-9]+)"[^>]*>(.*?)</a>', row, re.S)
        if not link:
            continue
        where = re.search(r'jv-job-list-location">(.*?)</td>', row, re.S)
        jobs.append(lead("jobvite", f"https://jobs.jobvite.com/{company}/job/{link.group(1)}", company,
                         strip(link.group(2)), [strip(where.group(1)) if where else ""], company, link.group(1)))
    return jobs


def jazzhr_board(client, company: str) -> list[dict]:
    """<company>.applytojob.com/apply: a JazzHR careers page (no public API)."""
    response = client.get(f"https://{company}.applytojob.com/apply")
    if response.status_code == 404:
        return []
    response.raise_for_status()
    jobs = []
    for block in re.findall(r"list-group-item-heading'>(.*?)</ul>", response.text, re.S):
        link = re.search(r'href="(https?://[^"]+/apply/([A-Za-z0-9]+)/[^"]*)"[^>]*>(.*?)</a>', block, re.S)
        if not link:
            continue
        where = re.search(r"fa-map-marker'></i>(.*?)</li>", block, re.S)
        jobs.append(lead("jazzhr", link.group(1), company, strip(link.group(3)),
                         [strip(where.group(1)) if where else ""], company, link.group(2)))
    return jobs


def icims_board(client, board: str) -> list[dict]:
    """careers-<company>.icims.com: iCIMS's job search pages (no public API)."""
    jobs, seen = [], set()
    for page in range(10):
        response = client.get(f"https://{board}.icims.com/jobs/search", params={"ss": 1, "in_iframe": 1, "pr": page})
        if response.status_code == 404:
            return jobs
        response.raise_for_status()
        found = 0
        for block in re.split(r'class="row"', response.text):
            link = re.search(r'href="(https://[^"]+/jobs/(\d+)/[^"]+)"[^>]*title="\d+ - ([^"]+)"', block)
            if not link or link.group(2) in seen:
                continue
            seen.add(link.group(2))
            found += 1
            where = re.search(r"Job Locations</span>\s*<span[^>]*>(.*?)</span>", block, re.S)
            jobs.append(lead("icims", link.group(1).replace("?in_iframe=1", ""), board.removeprefix("careers-"),
                             html.unescape(link.group(3)), [strip(where.group(1)) if where else ""], board,
                             link.group(2)))
        if not found:
            break
    return jobs


BOARD_FEEDS = {"greenhouse": greenhouse_board, "lever": lever_board, "ashby": ashby_board,
               "workable": workable_board, "smartrecruiters": smartrecruiters_board, "bamboohr": bamboohr_board,
               "jobvite": jobvite_board, "jazzhr": jazzhr_board, "icims": icims_board}


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
    """Board feeds have no category, so a job's title decides its field."""
    if OTHER in fields:
        return True
    return any(re.search(rf"\b({FIELDS[f][2]})\b", title or "", re.I) for f in fields if f in FIELDS)


def read_boards(client, todo: list[tuple[str, dict]], fields: list[str], counts: dict, log=print,
                names: dict | None = None) -> list[dict]:
    """Every posting on these (ats, company row) boards that's in your fields."""
    return [job for board_jobs in each_board(client, todo, fields, counts, log, names) for job in board_jobs]


def each_board(client, todo: list[tuple[str, dict]], fields: list[str], counts: dict, log=print,
               names: dict | None = None):
    """The postings in your fields, board by board as each finishes (a slow one holds up nothing)."""
    refused: dict[str, int] = {}  # Site -> boards skipped after it answered 429 (Too Many Requests).

    def read(ats_row):
        ats, row = ats_row
        if ats in refused:
            refused[ats] += 1
            return ats, row, None, None
        try:
            return ats, row, BOARD_FEEDS[ats](client, row["board"]), None
        except Exception as error:  # noqa: BLE001 - one bad board shouldn't stop a refresh.
            if getattr(getattr(error, "response", None), "status_code", None) == 429:
                refused.setdefault(ats, 0)
            return ats, row, [], error

    # Polite parallelism: a few boards at a time, each a single request. Hosted, the
    # class proxy admits four connections per visit, and model calls need one too.
    with ThreadPoolExecutor(max_workers=3 if config.HOSTED else 8) as pool:
        for future in as_completed([pool.submit(read, item) for item in todo]):
            ats, row, board_jobs, error = future.result()
            if error:  # httpx adds a "For more information" line to every status error.
                log(f"  {ats} board {row['board']}: {str(error).splitlines()[0]}")
            if error or board_jobs is None:
                yield []
                continue
            counts["boards"] += 1
            # Company names from the lists (or the company list) are better than board slugs.
            yield [{**job, "company": (names or {}).get((ats, row["board"]), row["name"])}
                   for job in board_jobs if matches_field(job["title"], fields)]
    for ats, skipped in refused.items():
        if skipped:
            log(f"  {ats} is refusing requests (429 Too Many Requests): skipped its other {skipped} boards "
                "this refresh; they're read again next time.")


# Listings sometimes carry a word from the posting as the company (Simplify listed one of
# Interplay's jobs under "Internship"); the job board then says whose job it is.
NOT_A_COMPANY = {"", "internship", "internships", "intern", "interns", "new grad", "careers", "jobs",
                 "job", "hiring", "company", "unknown", "n a", "remote"}


def company_name(companies, name: str, ats: str, board: str | None) -> str:
    if normalize(name) not in NOT_A_COMPANY or not board:
        return name
    owner = companies.by_board(ats, board)
    if owner and normalize(companies.name(owner)) not in NOT_A_COMPANY:
        return companies.name(owner)
    token = re.sub(r"[-_]+", " ", board.split("/")[0]).strip()
    return token.title() if token.islower() else token


def ingest(tracker, companies, jobs: list[dict], fields: list[str], counts: dict, keep=None) -> None:
    """Add new postings as found; close ones the lists show as closed; skip ones already known.
    keep(job): False drops a new posting without storing it (see wanted)."""
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
        job["company"] = company_name(companies, job["company"] or "", posting.ats, board)
        company_id = companies.record(job["company"], ats=posting.ats if board else "",
                                      board=board or "", source=job["site"],
                                      tags=",".join(fields))
        if tracker.find_duplicate(job["url"], job["company"], job["title"], company_id):
            counts["seen"] += 1
            continue
        if keep and not keep(job):
            counts["skipped"] = counts.get("skipped", 0) + 1
            continue
        tracker.add_lead(job.pop("url"), job.pop("site"), job.pop("company"), job.pop("title"),
                         job.pop("location"), company_id=company_id,
                         ats_board=board, **job)
        counts["new"] += 1


BOARD_BATCH = 50  # Company boards finished between two updates of the job list.


def wanted(profile: dict):
    """New postings worth storing: they pass your filters and share a word with your roles or
    description. The rest are never shown near the top, and tens of thousands of them filled the
    class platform's workspace, so they're dropped (changing Search refreshes again)."""
    from . import matching

    relevant = matching.relevant(profile)

    def keep(job: dict) -> bool:
        return relevant(job.get("title")) and not filters.check(job, job.get("details") or {}, profile)
    return keep


def refresh(tracker, profile: dict, boards: bool = False, log=print, progress=None, lists_read=None) -> dict:
    """Pull the lists into the tracker in batches: the lists, then company boards a batch at a
    time, each filtered and ranked as it lands (so Ranked jobs fills in while it runs).
    New jobs are 'found'; closed ones drop out. `lists_read()` is called once the lists are in,
    a second or so in, before the boards' minutes."""
    from . import matching

    fields = chosen_fields(profile)
    companies = Companies()
    counts = {"new": 0, "closed": 0, "seen": 0, "boards": 0, "skipped": 0}
    keep = wanted(profile)

    def settle(note: str) -> None:
        apply_filters(tracker, profile)
        matching.keyword_rank(tracker, profile)
        if progress:
            progress(f"{note}: {counts['new']} new jobs kept so far, {counts['skipped']} that don't fit dropped")

    with http() as client:
        jobs = simplify_jobs(client, [f for f in fields if f in FIELDS])
        log(f"Simplify lists: {len(jobs)} postings ({', '.join(FIELDS[f][0] if f in FIELDS else 'other roles' for f in fields)})")
        names = {(job_ats(j), board_of(j)): j["company"] for j in jobs}
        ingest(tracker, companies, jobs, fields, counts, keep)
        settle("Job lists read")
        if lists_read:
            lists_read()
        if boards:
            todo = [(ats, row) for ats in BOARD_FEEDS for row in companies.boards(ats)]
            batch = []
            for done, board_jobs in enumerate(each_board(client, todo, fields, counts, log, names), 1):
                batch += board_jobs
                if done % BOARD_BATCH == 0 or done == len(todo):
                    ingest(tracker, companies, batch, fields, counts, keep)
                    settle(f"{done} of {len(todo)} company boards read")
                    batch = []
            log(f"Company boards read: {counts['boards']}")
    companies.save()
    reassign(tracker)  # Aliases may have changed: keep caps counting by one company ID.
    counts["filtered"] = apply_filters(tracker, profile)
    return counts


APPLIED = "Looks like your application"  # The reason given when a job matches one you sent.


def applied_reason(tracker, job: dict, index: dict | None = None) -> str | None:
    """'Looks like your application to "MLE" (2026-09-12)': the same role at the same company.
    `index`: tracker.applied_index(), when checking many jobs."""
    if job.get("reason") == "Requeued by you.":
        return None  # You said it's a different job.
    sent = tracker.likely_applied(job, index)
    if not sent:
        return None
    when = (sent.get("submitted_at") or "")[:10]
    return f'{APPLIED} to "{sent["title"]}"' + (f" ({when})" if when else "") + "."


def set_aside_reposts(tracker) -> int:
    """The same posting found twice (on two of a company's career sites): keep the first."""
    from .tracker import same_posting

    kept: dict[str, list[dict]] = {}
    moved = 0
    for job in sorted(tracker.with_status("found", "prepared", "needs_you", "approved", "submitted"),
                      key=lambda j: (j["status"] == "found", j["id"])):  # Prepared or applied ones stay.
        first = next((k for k in kept.get(job["match_key"], []) if same_posting(k["job_key"], job["job_key"])), None)
        if first and job["status"] == "found":
            tracker.update(job["id"], status="skipped",
                           reason=f"Same posting as job #{first['id']}, listed on another career site.")
            moved += 1
        else:
            kept.setdefault(job["match_key"], []).append(job)
    return moved


def apply_filters(tracker, profile: dict) -> int:
    """Re-run the hard filters on unscored jobs, so profile changes take effect, and set aside
    jobs that look like ones you've already applied to."""
    moved = set_aside_reposts(tracker)
    applied = tracker.applied_index()
    for job in tracker.with_status("found", "filtered_out"):
        if job["fit_score"] is not None:  # Filtered by fit or caps, not by these rules...
            reason = applied_reason(tracker, job, applied) if job["status"] == "found" else None
            if reason:  # ...but an application you already sent counts whatever the score.
                tracker.update(job["id"], status="filtered_out", reason=reason)
                moved += 1
            continue
        reason = (filters.check(job, json.loads(job["details"] or "{}"), profile)
                  or applied_reason(tracker, job, applied))
        if reason and job["status"] == "found":
            tracker.update(job["id"], status="filtered_out", reason=reason)
            moved += 1
        elif not reason and job["status"] == "filtered_out" and not (job["reason"] or "").startswith(
                ("Company cap", "Active-application", "Paused", "Posting says")):
            tracker.update(job["id"], status="found", reason=None)
    return moved
