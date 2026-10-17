"""Recognize which application system a posting URL belongs to.

A posting found on several lists (Simplify, a company board, LinkedIn) gets
one identity from its system's own job ID, so it is never applied to twice.
"""

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

# Systems whose job IDs are only unique within one company's board.
PER_BOARD_IDS = {"workday", "bamboohr", "icims"}
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# Systems recognized by host only; applying to them is not supported yet.
HOSTS = {
    "oraclecloud.com": "oracle",
    "successfactors": "successfactors",
    "taleo.net": "taleo",
    "eightfold.ai": "eightfold",
}


@dataclass(frozen=True)
class Posting:
    ats: str                 # greenhouse, lever, ashby, workday, ... or "other"
    board: str | None = None  # The company's board: Greenhouse token, Lever/Ashby slug, Workday tenant/site...
    job_id: str | None = None

    @property
    def key(self) -> str | None:
        """A stable identity for the posting, or None when the URL has no job ID."""
        if not self.job_id:
            return None
        if self.ats in PER_BOARD_IDS:
            return f"{self.ats}:{self.board}:{self.job_id}"
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
    if host.endswith("smartrecruiters.com") and host != "smartrecruiters.com":
        # jobs.smartrecruiters.com/<Company>/<id>-<title>
        match = re.match(r"(\d{6,})", path[1]) if len(path) > 1 else None
        return Posting("smartrecruiters", path[0] if path else None, match.group(1) if match else None)
    if host.endswith("workable.com") and host != "workable.com":
        # apply.workable.com/<account>/j/<shortcode>
        job = path[2] if len(path) > 2 and path[1] == "j" else None
        return Posting("workable", path[0] if path else None, job)
    if host.endswith(".bamboohr.com"):
        # <company>.bamboohr.com/careers/<id>
        job = path[1] if len(path) > 1 and path[0] == "careers" and path[1].isdigit() else query.get("id")
        return Posting("bamboohr", host.split(".")[0], job)
    if host == "jobs.jobvite.com":
        # jobs.jobvite.com/<company>/job/<id>
        job = path[2] if len(path) > 2 and path[1] == "job" else None
        return Posting("jobvite", path[0] if path else None, job)
    if host.endswith(".applytojob.com"):
        # <company>.applytojob.com/apply/<id>/<title>  (JazzHR)
        job = path[1] if len(path) > 1 and path[0] == "apply" else None
        return Posting("jazzhr", host.split(".")[0], job)
    if host.endswith(".icims.com"):
        # careers-<company>.icims.com/jobs/<id>/<title>/job
        job = path[1] if len(path) > 1 and path[0] == "jobs" and path[1].isdigit() else None
        return Posting("icims", host.split(".")[0], job)
    for marker, name in HOSTS.items():
        if marker in host:
            return Posting(name)
    return Posting("other")
