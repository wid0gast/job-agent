"""Recognize which application system a posting URL belongs to.

A posting found on several lists (Simplify, a company board, LinkedIn) gets
one identity from its system's own job ID, so it is never applied to twice.
"""

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# Systems recognized by host only; applying to them is not supported yet.
HOSTS = {
    "icims.com": "icims",
    "oraclecloud.com": "oracle",
    "successfactors": "successfactors",
    "smartrecruiters.com": "smartrecruiters",
    "jobvite.com": "jobvite",
    "taleo.net": "taleo",
    "eightfold.ai": "eightfold",
}


@dataclass(frozen=True)
class Posting:
    ats: str                 # greenhouse, lever, ashby, workday, ... or "other"
    board: str | None = None  # Greenhouse board token, Lever/Ashby company slug, Workday tenant/site
    job_id: str | None = None

    @property
    def key(self) -> str | None:
        """A stable identity for the posting, or None when the URL has no job ID."""
        if not self.job_id:
            return None
        if self.ats == "workday":
            return f"workday:{self.board}:{self.job_id}"
        return f"{self.ats}:{self.job_id}"


def identify(url: str) -> Posting:
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    path = [p for p in parts.path.split("/") if p]
    query = {k.lower(): v[0] for k, v in parse_qs(parts.query).items()}

    if host.endswith("greenhouse.io"):
        if path[:2] == ["embed", "job_app"] or path[-1:] == ["job_app"]:
            return Posting("greenhouse", query.get("for"), query.get("token"))
        if len(path) >= 3 and path[1] == "jobs" and path[2].isdigit():
            return Posting("greenhouse", path[0], path[2])
        return Posting("greenhouse", path[0] if path else None)
    if "gh_jid" in query and query["gh_jid"].isdigit():
        return Posting("greenhouse", None, query["gh_jid"])
    if host.endswith("lever.co"):
        job = path[1] if len(path) > 1 and re.fullmatch(UUID, path[1]) else None
        return Posting("lever", path[0] if path else None, job)
    if host.endswith("ashbyhq.com"):
        job = path[1] if len(path) > 1 and re.fullmatch(UUID, path[1]) else None
        return Posting("ashby", path[0] if path else None, job)
    if "myworkdayjobs.com" in host:
        tenant = host.split(".")[0]
        if "job" in path:
            site = path[path.index("job") - 1] if path.index("job") > 0 else ""
            match = re.search(r"_([A-Za-z0-9-]+)$", path[-1])
            return Posting("workday", f"{tenant}/{site}", match.group(1) if match else None)
        return Posting("workday", tenant)
    for marker, name in HOSTS.items():
        if marker in host:
            return Posting(name)
    return Posting("other")
