"""A1: the planner agent. You give it a goal; it uses the pipeline as tools until
the approval queue holds the applications that serve it.

It can find, rank, prepare, review and redo, and ask you one combined question.
It can't approve, submit or change caps: those stay with you and with code.
"""

import json
import re
from datetime import date
from pathlib import Path

from . import actions, companies, config, matching, packets, prepare, sources, systems
from .harness import AgentRun, StopRun, Tool, schema
from .prompts import PLANNER_PROMPT

QUEUE = ("prepared", "needs_you", "approved")
MAX_REDOS = 2
MAX_QUESTIONS = 3


def trace_path(task_id: int | str) -> Path:
    return config.DATA_DIR / "runs" / f"{task_id}.jsonl"


def run(goal: str, ctx, ask, log, task_id: int) -> str:
    agent = AgentRun(ctx.llm, PLANNER_PROMPT, Planner(ctx, ask, log).tools(), trace_path(task_id),
                     task="planner", max_steps=40, max_seconds=45 * 60,
                     observer=lambda kind, data: log(f"  [{kind}] {str(data)[:200]}")
                     if kind in ("thought", "call", "finish") else None)
    prefs = config.load_profile().get("preferences", {})
    return agent.run(json.dumps({
        "goal": goal,
        "today": date.today().isoformat(),
        "queue_now": Planner(ctx, ask, log).queue_status(),
        "search_settings": {k: prefs.get(k) for k in ("description", "roles", "job_types", "locations",
                                                       "terms", "fit_threshold")},
        "coded_systems": sorted(systems.SUPPORTED),
        "other_systems": "filled by a browser agent during prepare (slower; prefer coded ones when equally good)",
    }, ensure_ascii=False, indent=1))


class Planner:
    def __init__(self, ctx, ask, log):
        self.ctx, self.tracker, self.ask, self.log = ctx, ctx.tracker, ask, log
        self.redos: dict[int, int] = {}
        self.replies: list[str] = []

    # ----- tools ------------------------------------------------------------

    def refresh_jobs(self):
        counts = sources.refresh(self.tracker, config.load_profile(), boards=True, log=self.log)
        return {k: counts[k] for k in ("new", "closed", "seen") if k in counts}

    def rank_jobs(self, limit: int = 60):
        return matching.score(self.ctx.llm, self.tracker, limit=max(1, min(limit, 200)), log=self.log)

    def list_jobs(self, status: str = "found", query: str = "", limit: int = 15,
                  coded_only: bool = False):
        words = [w for w in query.lower().split() if w]
        rows = []
        for job in self.tracker.with_status(status):
            text = f"{job['title']} {job['company']} {job['location']}".lower()
            if words and not all(w in text for w in words):
                continue
            if coded_only and job["ats"] not in systems.SUPPORTED:
                continue
            rows.append(job)
        rows.sort(key=matching.rank_key)
        return {"total": len(rows), "jobs": [brief(j) for j in rows[:max(1, min(limit, 40))]]}

    def read_job(self, job_id: int):
        job = self.job(job_id)
        details = json.loads(job["details"] or "{}")
        keep = {k: details[k] for k in ("match", "locations", "terms", "sponsorship", "degrees")
                if k in details}
        if "fit" in details:
            fit = details["fit"]
            keep["fit"] = {"score": fit.get("score"), "summary": fit.get("summary"),
                           "unmet": [r["requirement"] for r in fit.get("requirements", [])
                                     if r.get("met") == "no"], "flags": fit.get("flags")}
        return {**brief(job), "url": job["url"], "reason": job["reason"], "details": keep}

    def check_caps(self, job_id: int):
        job = self.job(job_id)
        company_id = job["company_id"] or companies.Companies().resolve(job["company"])
        problem = companies.cap_problem(self.tracker, company_id, self.ctx.settings, job_id)
        return {"company": job["company"], "can_apply": not problem, "problem": problem}

    def prepare_job(self, job_id: int):
        job = self.job(job_id)
        if job["status"] in QUEUE:
            raise ValueError(f"Job {job_id} is already {job['status']}; read_packet, or redo with a note.")
        status = prepare.prepare(job_id, self.ctx, self.log)
        return {"status": status, **(self.read_packet(job_id) if status == "prepared" else
                                     {"reason": self.tracker.get(job_id)["reason"]})}

    def read_packet(self, job_id: int):
        job = self.job(job_id)
        packet = packets.load(job)
        if packet is None:
            raise ValueError(f"Job {job_id} has no packet (status {job['status']}).")
        fit, checks = packet.get("fit") or {}, packet.get("checks") or {}
        resume = (packet.get("documents") or {}).get("resume") or {}
        return {
            "job_id": job_id, "status": job["status"], "company": job["company"], "title": job["title"],
            "fit": {"score": fit.get("score"), "summary": fit.get("summary"),
                    "unmet_must_haves": [r["requirement"] for r in fit.get("requirements", [])
                                         if r.get("must_have") and r.get("met") != "yes"]},
            "resume": {"pages": resume.get("pages"), "last_page_used": resume.get("fill"),
                       "gaps": (packet.get("tailoring") or {}).get("gaps")},
            "cover_letter": bool((packet.get("documents") or {}).get("cover_letter")),
            "needs_you": [{"key": a["key"], "label": a["label"][:200], "kind": a.get("kind"),
                           "options": (a.get("options") or [])[:12]} for a in packets.missing(packet)],
            "problems": (checks.get("consistency") or []) + [f"invented number: {n}" for n in
                                                              checks.get("invented_numbers") or []],
            "redos_left": MAX_REDOS - self.redos.get(job_id, 0),
        }

    def redo(self, job_id: int, note: str):
        if self.redos.get(job_id, 0) >= MAX_REDOS:
            raise ValueError(f"Job {job_id} was already redone {MAX_REDOS} times; leave it for the user.")
        if self.job(job_id)["status"] == "approved":
            raise ValueError("The user approved this one; it can't be changed.")
        self.redos[job_id] = self.redos.get(job_id, 0) + 1
        status = prepare.prepare(job_id, self.ctx, self.log, note=note.strip()[:800])
        return {"status": status, **(self.read_packet(job_id) if status == "prepared" else {})}

    def queue_status(self):
        return {status: len(self.tracker.with_status(status)) for status in QUEUE}

    def ask_user(self, question: str):
        if len(self.replies) >= MAX_QUESTIONS:
            raise ValueError("You've asked the user enough this run; leave the rest in the queue.")
        reply = self.ask(question.strip()[:1500])
        self.replies.append(reply)
        return {"reply": reply}

    def fill_answer(self, job_id: int, key: str, value: str):
        """Put the user's own words into an open question on a packet."""
        source = next((r for r in self.replies if normalize(value) and normalize(value) in normalize(r)), None)
        if source is None:
            raise ValueError("Only values the user gave in a reply this run can be filled in. Ask them first.")
        if DEFERRAL.search(source) and len(normalize(value)) > 0.6 * len(normalize(source)):
            raise ValueError("The user deferred or declined that one. Leave it for them to answer in review.")
        if self.job(job_id)["status"] == "approved":
            raise ValueError("The user approved this one; it can't be changed.")
        actions.edit_answer(self.tracker, job_id, key, value)
        return {"filled": key, "needs_you_left": len(self.read_packet(job_id)["needs_you"])}

    def finish(self, summary: str):
        raise StopRun(summary)

    # ----- plumbing ---------------------------------------------------------

    def job(self, job_id: int) -> dict:
        job = self.tracker.get(int(job_id))
        if job is None:
            raise ValueError(f"No job {job_id}.")
        return job

    def tools(self) -> list[Tool]:
        integer, string = {"type": "integer"}, {"type": "string"}
        return [
            Tool("refresh_jobs", "Pull the latest job lists and company boards (about a minute). "
                 "Only if the found list is thin or stale.", schema(), self.refresh_jobs),
            Tool("rank_jobs", "Score unranked found jobs against the user's description (one model "
                 "call per 25 jobs).", schema({"limit": integer}, []), self.rank_jobs),
            Tool("list_jobs", "Jobs with a status (found, prepared, needs_you, approved, submitted, "
                 "filtered_out, failed), best score first. query matches title, company, location. "
                 "how=coded jobs are fast and reliable; how=agent jobs are filled by a browser agent "
                 "(slower, more model calls).",
                 schema({"status": string, "query": string, "limit": integer, "coded_only": {"type": "boolean"}}, []),
                 self.list_jobs),
            Tool("read_job", "One job's details: match reason, locations, terms, fit if prepared.",
                 schema({"job_id": integer}), self.read_job),
            Tool("check_caps", "Whether the user can still apply at this job's company.",
                 schema({"job_id": integer}), self.check_caps),
            Tool("prepare_job", "Prepare an application packet (fit check, tailored resume, answers). "
                 "Several model calls; it may end filtered_out if the fit is low.",
                 schema({"job_id": integer}), self.prepare_job),
            Tool("read_packet", "Review a prepared packet: fit, resume fill, open questions, problems.",
                 schema({"job_id": integer}), self.read_packet),
            Tool("redo", f"Prepare again with a specific note on what to fix (max {MAX_REDOS} per job). "
                 "The note must point at something in the packet or posting.",
                 schema({"job_id": integer, "note": string}), self.redo),
            Tool("queue_status", "How many packets are prepared, waiting on the user, or approved.",
                 schema(), self.queue_status),
            Tool("ask_user", f"Ask the user something (max {MAX_QUESTIONS} per run). Combine questions "
                 "shared by several packets into one.", schema({"question": string}), self.ask_user),
            Tool("fill_answer", "Fill an open packet question with the user's reply (it must appear "
                 "in what they said).", schema({"job_id": integer, "key": string, "value": string}),
                 self.fill_answer),
            Tool("finish", "End the run with a short report for the user.",
                 schema({"summary": string}), self.finish),
        ]


def brief(job: dict) -> dict:
    score, kind = matching.ranking_score(job)
    return {"id": job["id"], "company": job["company"], "title": job["title"],
            "location": (job["location"] or "")[:60], "system": job["ats"],
            "how": "coded" if job["ats"] in systems.SUPPORTED else "agent", "score": score,
            "score_kind": kind or None,
            "posted": (job["posted_at"] or "")[:10], "status": job["status"]}


DEFERRAL = re.compile(r"\b(skip|later|myself|in review|not now|don'?t know|no idea|n/?a|leave (it|this|that)|"
                      r"decline|rather not|prefer not|i'?ll (answer|do|handle))\b", re.I)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip().lower()
