"""Local dashboard at http://127.0.0.1:<port>: today, approval queue, jobs, activity.

It only reads the database and queues tasks; the worker process it starts
does the browsing. Any website you visit can send requests to 127.0.0.1, so
every request must carry the right Host, and every change must come from
this page's own origin with this session's token.
"""

import difflib
import html
import json
import re
import secrets
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from email.parser import BytesParser
from email.policy import default as email_policy

from . import actions, config, history, matching, packets, resumes, sources, systems
from .tracker import OUTCOMES, Tracker
from .worker import DB_PATH

LOG_PATH = config.DATA_DIR / "logs" / "worker.log"
QUEUE = ("prepared", "needs_you", "approved", "submitting")
ORDER = ("found", "preparing", "prepared", "needs_you", "approved", "submitting", "submitted",
         "filtered_out", "failed", "rejected", "skipped", "closed", "working")

CSS = """
:root{--bg:#f7f7f5;--card:#fff;--text:#1d1d1b;--muted:#6b6b66;--line:#e3e2dd;--accent:#2357d9;
--good:#1f7a3f;--bad:#b3261e;--warn:#9a6200;--pill:#efeee9}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#1f1f1d;--text:#ecebe6;--muted:#a3a29b;
--line:#33332f;--accent:#7aa2ff;--good:#6fcf8e;--bad:#ff8a80;--warn:#f2b84b;--pill:#2a2a27}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:14px/1.45 system-ui,-apple-system,sans-serif}
header{display:flex;gap:18px;align-items:center;padding:12px 20px;border-bottom:1px solid var(--line);
background:var(--card);flex-wrap:wrap}header b{font-size:15px}header a{color:var(--muted);text-decoration:none}
header a.on{color:var(--text);font-weight:600}main{max-width:1100px;margin:0 auto;padding:20px 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:0 0 16px}
h1{font-size:20px;margin:0 0 12px}h2{font-size:15px;margin:0 0 10px}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:7px 8px;border-top:1px solid var(--line);
vertical-align:top}th{color:var(--muted);font-weight:500;border-top:0}
.pill{display:inline-block;padding:1px 8px;border-radius:99px;background:var(--pill);font-size:12px}
.submitted,.approved,.good{color:var(--good)}.failed,.needs_you,.rejected{color:var(--bad)}.prepared{color:var(--accent)}
.muted{color:var(--muted)}.warn{color:var(--warn)}.bad{color:var(--bad)}
button,.button{font:inherit;padding:6px 12px;border-radius:7px;border:1px solid var(--line);background:var(--card);
color:var(--text);cursor:pointer;text-decoration:none;display:inline-block}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
input[type=text],input[type=number],textarea,select{font:inherit;width:100%;padding:6px 8px;border:1px solid var(--line);
border-radius:7px;background:var(--bg);color:var(--text)}textarea{min-height:90px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.stats{display:grid;
grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px}.stat{padding:12px;border:1px solid var(--line);
border-radius:9px;background:var(--card)}.stat b{display:block;font-size:22px}
.flash{padding:10px 14px;border-radius:8px;margin-bottom:14px;background:var(--pill)}
pre{white-space:pre-wrap;font:12px/1.5 ui-monospace,monospace;max-height:520px;overflow:auto}
.src{font-size:12px;color:var(--muted)}
.below{opacity:.45}.score{font-weight:700;font-size:16px}.kind{font-size:11px;color:var(--muted);display:block}
.diff .add{background:color-mix(in srgb,var(--good) 18%,transparent)}
.diff .del{background:color-mix(in srgb,var(--bad) 18%,transparent);text-decoration:line-through}
"""


# Hosted only: the class platform refuses plain form posts as cross-origin but
# accepts the same request sent by a script, so forms are sent with fetch().
FORMS_JS = """
document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (!(form instanceof HTMLFormElement) || form.method.toLowerCase() !== "post") return;
  event.preventDefault();
  const data = new FormData(form);
  if (event.submitter && event.submitter.name) data.append(event.submitter.name, event.submitter.value);
  const buttons = form.querySelectorAll("button");
  buttons.forEach((b) => { b.disabled = true; });
  document.body.style.cursor = "progress";
  try {
    const response = await fetch(form.action, { method: "POST", body: data, credentials: "same-origin" });
    if (!response.ok) throw new Error(response.status + " " + (await response.text()).slice(0, 300));
    location.href = response.url;
  } catch (error) {
    buttons.forEach((b) => { b.disabled = false; });
    document.body.style.cursor = "";
    const note = document.createElement("div");
    note.className = "flash bad";
    note.textContent = "Could not send that: " + error.message;
    document.querySelector("main").prepend(note);
  }
});
"""


def esc(value) -> str:
    return html.escape("" if value is None else str(value))


class Dashboard:
    def __init__(self, settings, port: int):
        self.settings, self.port = settings, port
        self.tracker = Tracker(DB_PATH)
        self.token = secrets.token_urlsafe(24)
        self.worker: subprocess.Popen | None = None

    def ensure_worker(self) -> None:
        if self.worker and self.worker.poll() is None:
            return
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        log = LOG_PATH.open("a", encoding="utf-8")
        self.worker = subprocess.Popen([sys.executable, "-m", "job_agent.cli", "worker"],
                                       cwd=config.ROOT, stdout=log, stderr=subprocess.STDOUT)

    def stop_worker(self) -> None:
        if self.worker and self.worker.poll() is None:
            self.worker.terminate()
            try:
                self.worker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.worker.kill()


SCRIPT = '<script src="/forms.js" defer></script>'


def layout(app: Dashboard, active: str, body: str, flash: str = "", refresh: bool = False) -> bytes:
    links = [("/", "Today"), ("/search", "Search"), ("/jobs", "Ranked jobs"),
             ("/queue", "Approval queue"), ("/applications", "Applications"),
             ("/resumes", "Resumes"), ("/activity", "Activity")]
    nav = "".join(f'<a href="{h}" class="{"on" if h == active else ""}">{t}</a>' for h, t in links)
    auto = '<meta http-equiv="refresh" content="4">' if refresh else ""
    banner = ('<div class="flash warn">Auto-submit is on.</div>'
              if app.settings.submit_mode == "auto" else "")
    if config.HOSTED:
        banner += hosted_banner(active)
    note = f'<div class="flash">{esc(flash)}</div>' if flash else ""
    return (f"<!doctype html><html><head><meta charset=utf-8><meta name=viewport "
            f"content='width=device-width,initial-scale=1'>{auto}<title>Job Agent</title>"
            f"<link rel=stylesheet href=/style.css>{SCRIPT if config.HOSTED else ''}</head><body><header><b>Job Agent</b>{nav}</header>"
            f"<main>{banner}{note}{body}</main></body></html>").encode()


def hosted_banner(active: str) -> str:
    """Class app store: the session is temporary, so say when it ends and offer the download."""
    import os
    from datetime import datetime

    parts = []
    if os.environ.get("JOB_AGENT_MODEL_ERROR"):
        parts.append(f'<div class="flash bad">Could not connect to the class models: '
                     f'{esc(os.environ["JOB_AGENT_MODEL_ERROR"])}</div>')
    ends = os.environ.get("JOB_AGENT_SESSION_ENDS", "")
    try:
        minutes = int((datetime.fromisoformat(ends.replace("Z", "+00:00")).timestamp()
                       - datetime.now().timestamp()) // 60)
        left = f"This session ends in about {max(0, minutes)} minutes and forgets everything. "
    except ValueError:
        left = "This session is temporary and forgets everything when it ends. "
    parts.append(f'<div class="flash">{left}<a href="/export.zip">Download everything</a> '
                 '(resumes, applications, answers, passwords) before then.</div>')
    if not (config.PROFILE_DIR / "resume.md").exists() and active not in ("/resumes",):
        parts.append('<div class="flash warn">Start here: <a href="/resumes">upload your resume PDF(s)</a>, '
                     'accept the merged master resume, then describe the jobs you want on '
                     '<a href="/search">Search</a>. Optional: import your job sheet on '
                     '<a href="/applications">Applications</a>.</div>')
    return "".join(parts)


def page_passwords() -> str:
    """Hosted: site accounts made this session; there's no keychain to keep them in."""
    if not config.HOSTED or not config.PASSWORDS_FILE.exists():
        return ""
    stored = json.loads(config.PASSWORDS_FILE.read_text())
    rows = "".join(f"<tr><td>{esc(k.split('|')[0].split(':', 1)[-1])}</td><td>{esc(k.split('|')[-1])}</td>"
                   f"<td><code>{esc(v)}</code></td></tr>" for k, v in stored.items())
    return (f'<div class=card><h2>Accounts created this session</h2><p class=muted>Save these: the session '
            f'forgets them.</p><table><tr><th>Site</th><th>Email</th><th>Password</th></tr>{rows}</table></div>')


def export_zip() -> bytes:
    """Everything the session made, minus caches."""
    import io
    import zipfile

    import sqlite3

    buffer = io.BytesIO()
    skip = {"browser-profile", "worker.lock", "cache"}  # cache: raw job lists, fetched again on refresh.
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for folder in (config.PROFILE_DIR, config.DATA_DIR):
            for path in folder.rglob("*"):
                if (path.is_file() and not skip & set(path.relative_to(config.HOME).parts)
                        and not path.name.startswith("applications.db")):
                    archive.write(path, path.relative_to(config.HOME))
        # One consistent copy of the database, even while the worker is writing to it.
        live, copy = sqlite3.connect(DB_PATH), sqlite3.connect(":memory:")
        try:
            live.backup(copy)
            archive.writestr("data/applications.db", copy.serialize())
        finally:
            live.close()
            copy.close()
    return buffer.getvalue()


def form(app: Dashboard, action: str, button: str, fields: dict | None = None,
         primary: bool = False, extra: str = "") -> str:
    hidden = "".join(f'<input type=hidden name="{esc(k)}" value="{esc(v)}">'
                     for k, v in (fields or {}).items())
    return (f'<form method=post action="{action}" style="display:inline">'
            f'<input type=hidden name=token value="{app.token}">{hidden}{extra}'
            f'<button class="{"primary" if primary else ""}">{esc(button)}</button></form>')


# ----- pages ----------------------------------------------------------------

def page_today(app: Dashboard, flash: str) -> bytes:
    jobs = app.tracker.all()
    counts = {s: 0 for s in ORDER}
    for job in jobs:
        counts[job["status"]] = counts.get(job["status"], 0) + 1
    stats = "".join(f'<a class="stat" href="/jobs?status={s}" style="text-decoration:none;color:inherit">'
                    f'<b>{counts[s]}</b><span class="muted">{s.replace("_", " ")}</span></a>'
                    for s in ("found", "prepared", "needs_you", "approved", "submitted", "filtered_out")
                    if s in counts)
    tasks = app.tracker.tasks(12)
    # Reload while the worker is busy, but not while it waits for you to type.
    waiting = any(t["status"] == "waiting" for t in tasks)
    running = not waiting and any(t["status"] in ("queued", "running") for t in tasks)
    rows = []
    for task in tasks:
        reply = ""
        if task["status"] == "waiting":
            from .worker import LIVE_SCREENSHOT

            shot = (f' <a href="/file?path={quote(str(LIVE_SCREENSHOT))}" target=_blank>See the page</a>'
                    if config.HOSTED and LIVE_SCREENSHOT.exists() else "")
            reply = (f'<div class="card" style="margin:8px 0 0"><b>{esc(task["question"])}</b>{shot}'
                     + form(app, "/reply", "Send", {"task_id": task["id"]}, True,
                            '<input type=text name=answer autofocus style="max-width:260px;margin-right:8px">')
                     + "</div>")
        job = f' <a href="/job/{task["job_id"]}">job {task["job_id"]}</a>' if task["job_id"] else ""
        if task["kind"] == "plan":
            job = f' <a href="/runs/{task["id"]}">trace</a>'
            if task["arg"]:
                job += f'<span class=kind>{esc(task["arg"][:90])}</span>'
        rows.append(f'<tr><td>{task["id"]}</td><td>{esc(task["kind"])}{job}</td>'
                    f'<td><span class="pill {esc(task["status"])}">{esc(task["status"])}</span>{reply}</td>'
                    f'<td class="muted">{esc((task["result"] or "")[:2000 if task["kind"] == "warmup" else 160])}</td></tr>')
    body = f"""
    <h1>Today</h1>
    <div class="stats">{stats}</div>
    <div class="card" style="margin-top:16px"><div class="row">
      {form(app, "/refresh", "Refresh job lists", primary=True)}
      {form(app, "/prepare-top", "Prepare top matches", extra='<input type=number name=count value=3 min=1 max=20 style="width:70px;margin-right:6px">')}
      <span class="muted">Submitted today: {app.tracker.submitted_today()} of {app.settings.daily_submit_limit}
      · model {esc(app.settings.model or "not set")}</span>
    </div></div>
    <div class="card"><h2>Give the agent a goal</h2>
    <form method=post action="/plan"><input type=hidden name=token value="{app.token}">
    <textarea name=goal rows=2 required placeholder="Fill the queue with my 8 best ML internships, NYC or remote. One per company."></textarea>
    <div class=row style="margin-top:8px"><button class=primary>Start the agent</button>
    <span class=muted>It finds, ranks, prepares and reviews applications, and asks you if it needs to. You still approve every one.</span></div></form></div>
    <div class="card"><h2>Tasks</h2><table><tr><th>#</th><th>Task</th><th>Status</th><th>Result</th></tr>
    {"".join(rows) or '<tr><td colspan=4 class=muted>Nothing yet. Refresh the job lists to start.</td></tr>'}
    </table></div>"""
    return layout(app, "/", body, flash, refresh=running)


def page_queue(app: Dashboard, flash: str) -> bytes:
    rows = []
    for job in app.tracker.with_status(*QUEUE, order="fit_score DESC, id"):
        box = (f'<input type=checkbox name=ids value="{job["id"]}">'
               if job["status"] == "prepared" else "")
        rows.append(f'<tr><td>{box}</td><td><a href="/job/{job["id"]}">{esc(job["title"])}</a>'
                    f'<div class=muted>{esc(job["company"])} · {esc((job["location"] or "")[:60])}</div></td>'
                    f'<td>{esc(job["fit_score"])}</td><td><span class="pill {job["status"]}">'
                    f'{job["status"].replace("_", " ")}</span><div class=muted>{esc(job["reason"] or "")}</div></td></tr>')
    approved = [j for j in app.tracker.with_status("approved")]
    send = (form(app, "/submit", f"Submit {len(approved)} approved", {"ids": ",".join(str(j["id"]) for j in approved)}, True)
            if approved else "")
    body = f"""<h1>Approval queue</h1>
    <form method=post action="/approve"><input type=hidden name=token value="{app.token}">
    <div class="card"><table><tr><th></th><th>Job</th><th>Fit</th><th>Status</th></tr>
    {"".join(rows) or '<tr><td colspan=4 class=muted>Nothing waiting. Prepare some jobs from Today or Jobs.</td></tr>'}
    </table><div class="row" style="margin-top:12px"><button class=primary>Approve selected</button></div></div></form>
    <div class="row">{send}</div>"""
    return layout(app, "/queue", body, flash)


ACTIVE = ("found", "prepared", "needs_you", "approved")


def page_jobs(app: Dashboard, status: str, flash: str) -> bytes:
    """Jobs ranked by score: the fit score once prepared, else the quick match score."""
    profile = config.load_profile()
    threshold = config.fit_threshold(profile, app.settings)
    views = [("active", "To apply"), *[(s, s.replace("_", " ")) for s in ORDER if s != "working"], ("all", "all")]
    tabs = " ".join(f'<a class="button" href="/jobs?status={key}" style="{"font-weight:600" if key == status else ""}">'
                    f'{name}</a>' for key, name in views)
    wanted = ACTIVE if status == "active" else tuple(ORDER) if status == "all" else (status,)
    jobs = app.tracker.with_status(*wanted)

    def rank(job):
        score, _ = matching.ranking_score(job)
        return (score is None, -(score or 0), -matching._epoch(job["posted_at"]))

    jobs.sort(key=rank)
    unscored = len(matching.stale(app.tracker, profile, config.read_master_resume())) \
        if (profile.get("preferences", {}).get("description") or profile.get("preferences", {}).get("roles")) else None
    rows = []
    for job in jobs[:500]:
        score, kind = matching.ranking_score(job)
        why = json.loads(job["details"] or "{}").get("match", {}).get("why", "") if kind == "match" else ""
        if job["status"] == "found":
            act = form(app, "/prepare", "Prepare", {"job_id": job["id"]}) if job["ats"] in systems.SUPPORTED \
                else f'<span class="muted">{esc(job["ats"])} not supported yet</span>'
        elif job["status"] in ("failed", "rejected", "skipped", "closed", "filtered_out"):
            act = form(app, "/requeue", "Back to found", {"job_id": job["id"]})
        else:
            act = f'<a href="/job/{job["id"]}">open</a>'
        below = score is not None and score < threshold
        badge = (f'<span class=score>{score}</span><span class=kind>{kind}</span>' if score is not None
                 else '<span class=muted>—</span>')
        rows.append(f'<tr class="{"below" if below else ""}"><td>{badge}</td>'
                    f'<td><a href="/job/{job["id"]}">{esc(job["title"])}</a>'
                    f'<div class=muted>{esc(job["company"])} · {esc((job["location"] or "")[:70])}</div>'
                    f'{f"<div class=src>{esc(why)}</div>" if why else ""}</td>'
                    f'<td class=muted style="white-space:nowrap">{esc((job["posted_at"] or "")[:10])}</td>'
                    f'<td>{esc(job["ats"])}</td>'
                    f'<td><span class="pill {job["status"]}">{job["status"].replace("_", " ")}</span>'
                    f'<div class=muted>{esc((job["reason"] or "")[:120])}</div></td><td>{act}</td></tr>')
    if unscored is None:
        scoring = '<a href="/search">Describe what you\'re looking for</a> to rank jobs.'
    elif unscored:
        calls = -(-unscored // matching.BATCH)
        scoring = (form(app, "/score", f"Score {unscored} unranked job(s)", primary=True)
                   + f' <span class=muted>about {calls} model request(s), ~{matching.BATCH} jobs each</span>')
    else:
        scoring = '<span class=muted>Every job is ranked against your current description.</span>'
    body = f"""<h1>Ranked jobs</h1>
    <div class=card><div class=row>{scoring}</div>
    <p class=muted style="margin:10px 0 0">Score = <b>fit</b> (read the whole posting, when prepared) or
    <b>match</b> (title, company and location against your description). Greyed rows are below your
    threshold of <b>{threshold}</b> (<a href="/search">change</a>); jobs whose fit falls below it aren't prepared.</p></div>
    <div class="row" style="margin-bottom:14px">{tabs}</div>
    <div class="card"><table><tr><th>Score</th><th>Job</th><th>Posted</th><th>System</th><th>Status</th><th></th></tr>
    {"".join(rows) or '<tr><td colspan=6 class=muted>None.</td></tr>'}</table>
    {f'<p class=muted>Showing the top 500 of {len(jobs)}.</p>' if len(jobs) > 500 else ''}</div>"""
    return layout(app, "/jobs", body, flash)


def page_search(app: Dashboard, flash: str) -> bytes:
    prefs = config.load_profile().get("preferences", {})
    threshold = config.fit_threshold({"preferences": prefs}, app.settings)
    types = prefs.get("job_types") or []

    def listed(key, sep=", "):
        return esc(sep.join(prefs.get(key) or []))

    checks = "".join(f'<label class=row style="gap:6px"><input type=checkbox name=job_types value="{t}"'
                     f'{" checked" if t in types else ""}> {label}</label>'
                     for t, label in (("internship", "Internships"), ("full_time", "Full-time")))
    body = f"""<h1>Search</h1>
    <form method=post action="/search"><input type=hidden name=token value="{app.token}">
    <div class=card><h2>What are you looking for?</h2>
    <p class=muted>In your own words: roles, fields, kinds of teams or companies, anything that makes a job
    right or wrong for you. Used to rank jobs and to judge fit when preparing.</p>
    <textarea name=description style="min-height:130px" placeholder="e.g. ML research or applied ML internships, especially NLP and LLM evaluation. Data science roles are fine too. Prefer AI-first startups and research labs; not interested in pure analytics or marketing roles.">{esc(prefs.get("description", ""))}</textarea>
    <p style="margin:12px 0 4px"><b>Role titles</b> <span class=muted>(comma-separated)</span></p>
    <input type=text name=roles value="{listed('roles')}"></div>

    <div class=card><h2>Filters</h2><p class=muted>Applied in code before any model call; filtered jobs show the reason.</p>
    <div class=row style="gap:18px">{checks}</div>
    <p style="margin:12px 0 4px"><b>Locations</b> <span class=muted>(one per line; "Remote" means remote in the US; "any" for no filter)</span></p>
    <textarea name=locations style="min-height:70px">{listed('locations', chr(10))}</textarea>
    <p style="margin:12px 0 4px"><b>Internship seasons</b> <span class=muted>(comma-separated, e.g. Summer 2027; empty for any)</span></p>
    <input type=text name=terms value="{listed('terms')}">
    <p style="margin:12px 0 4px"><b>Only jobs posted in the last … days</b> <span class=muted>(empty for any)</span></p>
    <input type=number name=max_posting_age_days min=1 max=365 value="{esc(prefs.get('max_posting_age_days') or '')}" style="width:120px">
    <p style="margin:12px 0 4px"><b>Exclude companies</b> <span class=muted>(comma-separated)</span></p>
    <input type=text name=exclude_companies value="{listed('exclude_companies')}">
    <p style="margin:12px 0 4px"><b>Exclude titles containing</b> <span class=muted>(comma-separated words)</span></p>
    <input type=text name=exclude_title_words value="{listed('exclude_title_words')}"></div>

    <div class=card><h2>Fit threshold</h2>
    <p class=muted>0–100. Jobs scoring below this are greyed in the ranking, and a job whose fit (read
    from the full posting) falls below it isn't prepared. 80+ strong fit · 60–79 reasonable · below 60 weak.</p>
    <input type=number name=fit_threshold min=0 max=100 step=5 value="{threshold}" style="width:120px"></div>

    <div class=card><h2>Tailored resume length</h2>
    <p class=muted>Typeset in your own LaTeX style (profile/resume-templates/). Longer versions are trimmed to fit.</p>
    <select name=resume_pages style="width:auto">
      <option value="1"{" selected" if str(prefs.get("resume_pages") or 1) == "1" else ""}>One page</option>
      <option value="2"{" selected" if str(prefs.get("resume_pages") or 1) == "2" else ""}>Two pages</option>
    </select></div>
    <button class=primary>Save</button></form>"""
    return layout(app, "/search", body, flash)


def answer_input(app: Dashboard, job_id: int, answer: dict, editable: bool) -> str:
    value = answer.get("value")
    if not editable:
        return esc(", ".join(value) if isinstance(value, list) else value)
    name = 'name=value'
    if answer["kind"] == "file":
        return esc(value or "—")
    if answer["kind"] in ("select", "multiselect") and answer["options"]:
        chosen = value if isinstance(value, list) else [value]
        multiple = " multiple" if answer["kind"] == "multiselect" else ""
        options = "".join(f'<option{" selected" if o in chosen else ""}>{esc(o)}</option>'
                          for o in [""] + answer["options"])
        field = f"<select {name}{multiple}>{options}</select>"
    elif answer["kind"] == "textarea":
        field = f"<textarea {name}>{esc(value)}</textarea>"
    else:
        field = f'<input type=text {name} value="{esc(value)}">'
    remember = '<label class=muted style="font-size:12px"><input type=checkbox name=remember value=1> remember</label>'
    return (f'<form method=post action="/edit"><input type=hidden name=token value="{app.token}">'
            f'<input type=hidden name=job_id value="{job_id}"><input type=hidden name=key value="{esc(answer["key"])}">'
            f'{field}<div class=row style="margin-top:4px"><button>Save</button>{remember}</div></form>')


def page_job(app: Dashboard, job_id: int, flash: str) -> bytes:
    job = app.tracker.get(job_id)
    if job is None:
        return layout(app, "/queue", "<h1>No such job</h1>")
    packet = packets.load(job)
    head = (f'<h1>{esc(job["title"])}</h1><p class=muted>{esc(job["company"])} · {esc(job["location"])} · '
            f'<a href="{esc(job["url"])}" target=_blank rel=noopener>posting</a> · '
            f'<span class="pill {job["status"]}">{job["status"].replace("_", " ")}</span> '
            f'{esc(job["reason"] or "")}</p>')
    if packet is None:
        details = json.loads(job["details"] or "{}")
        fit = details.get("fit")
        extra = f'<div class=card><b>Fit {fit["score"]}</b><p>{esc(fit["summary"])}</p></div>' if fit else ""
        note = "" if job["status"] == "submitted" else '<p class=muted>Not prepared yet.</p>'
        mark = (form(app, "/mark-applied", "I applied to this myself", {"job_id": job_id})
                if job["status"] in ("found", "filtered_out", "closed", "skipped", "failed", "rejected") else "")
        return layout(app, "/applications" if job["status"] == "submitted" else "/jobs",
                      head + application_block(app, job) + extra + note + mark, flash)
    editable = job["status"] in ("prepared", "approved", "needs_you")
    fit = packet["fit"]
    reqs = "".join(f'<tr><td>{esc(r["requirement"])}{" <span class=muted>(must)</span>" if r["must_have"] else ""}</td>'
                   f'<td class="{"good" if r["met"] == "yes" else "warn" if r["met"] == "partly" else "bad"}">{r["met"]}</td>'
                   f'<td class=muted>{esc(r["evidence"])}</td></tr>' for r in fit["requirements"])
    problems = packets.missing(packet)
    checks = packet["checks"]
    warn = []
    if problems:
        warn.append("Needs your answer: " + "; ".join(esc(a["label"]) for a in problems))
    warn += [esc(c) for c in packets.consistency(packet, config.load_profile())]
    if checks.get("resume_pages", 1) > 1:
        warn.append(f"The resume is {checks['resume_pages']} pages.")
    if checks.get("invented_numbers"):
        warn.append(f"Tailoring invented {esc(checks['invented_numbers'])}; your resume was used unchanged.")
    warn += [esc(n) for n in packet.get("form_notes", [])]
    for flag in fit.get("flags", []):
        warn.append("Posting flag: " + esc(flag))
    docs = "".join(f'<a class=button target=_blank href="/file?path={esc(d["pdf"])}">{k.replace("_", " ")} PDF</a> '
                   for k, d in packet["documents"].items() if d)
    rows = "".join(
        f'<tr><td><b>{esc(a["label"])}</b>{" *" if a["required"] else ""}'
        f'<div class=src>{esc(a["source"])}{" · " + esc(a["note"]) if a.get("note") else ""}</div></td>'
        f'<td style="width:50%">{answer_input(app, job_id, a, editable)}</td></tr>'
        for a in packet["answers"])
    tail = packet.get("submission")
    proof = ""
    if packet.get("research"):  # Company facts the letter and "why us" answers could use.
        facts = "".join(f'<li>{esc(f["fact"])} <span class=muted>“{esc(f["quote"][:160])}”</span> '
                        f'<a href="{esc(f["url"])}" target=_blank rel=noopener>source</a></li>'
                        for f in packet["research"]["facts"])
        proof += (f'<div class=card><b>Company research</b><p class=muted>{esc(packet["research"]["summary"])} '
                  f'<a href="/runs/research-{job_id}">How it searched</a></p>'
                  f'<ul>{facts or "<li class=muted>No verified facts; the letter sticks to the posting.</li>"}</ul></div>')
    if packet.get("recording"):  # Filled by the explore agent; submit replays these steps.
        from . import web

        review = Path(packet["_dir"]) / "review.png"
        shot = f' · <a target=_blank href="/file?path={esc(review)}">the form before submit</a>' if review.exists() else ""
        steps = "".join(f"<li>{esc(line)}</li>" for line in web.summary(packet["recording"]))
        proof += (f'<div class=card><b>Filled by the browser agent</b><p class=muted>On submit, code replays '
                  f'these steps with your approved answers; nothing else is clicked. '
                  f'<a href="/runs/explore-{job_id}">How it got there</a>{shot}</p>'
                  f'<details><summary>{len(packet["recording"]["steps"])} recorded steps</summary><ol>{steps}</ol></details></div>')
    if tail:
        shot = f' <a target=_blank href="/file?path={esc(tail["screenshot"])}">screenshot</a>' if tail.get("screenshot") else ""
        proof = f'<div class=card><b>Submission</b><p>{esc(tail["result"].get("detail"))}{shot}</p></div>'
    buttons = []
    if job["status"] == "prepared":
        buttons.append(form(app, "/approve", "Approve", {"ids": job_id}, True))
    if job["status"] == "approved":
        buttons.append(form(app, "/submit", "Submit now", {"ids": job_id}, True))
    if job["status"] in ("prepared", "approved", "needs_you"):
        buttons.append(form(app, "/reject", "Reject", {"job_id": job_id}))
    if job["status"] in ("needs_you", "prepared") and packets.missing(packet):
        buttons.append(form(app, "/draft", "Draft missing answers with the model", {"job_id": job_id}))
    if job["status"] == "needs_you":
        for outcome, label in (("submitted", "It went through"), ("prepared", "Back to queue"),
                               ("failed", "Mark failed")):
            buttons.append(form(app, "/resolve", label, {"job_id": job_id, "outcome": outcome}))
    t = packet["tailoring"]
    body = f"""{head}
    {"".join(f'<div class="flash bad">{w}</div>' for w in warn)}
    <div class="row" style="margin-bottom:16px">{" ".join(buttons)} {docs}</div>
    {proof}
    {application_block(app, job)}
    <div class=card><h2>Fit {fit["score"]}</h2><p>{esc(fit["summary"])}</p>
    <table><tr><th>Requirement</th><th>Met</th><th>Evidence</th></tr>{reqs}</table></div>
    <div class=card><h2>Tailoring</h2><p><b>Matched:</b> {esc("; ".join(t["matched"]))}</p>
    <p><b>Emphasized:</b> {esc("; ".join(t["emphasized"]))}</p><p><b>Gaps:</b> {esc("; ".join(t["gaps"]))}</p></div>
    <div class=card><h2>Answers</h2><table>{rows}</table></div>"""
    return layout(app, "/queue", body, flash)


def page_resumes(app: Dashboard, flash: str) -> bytes:
    versions = resumes.parsed()
    rows = []
    for v in versions:
        checks = v.get("checks", {})
        flags = [f"numbers not in the PDF: {', '.join(checks['invented_numbers'])}"] if checks.get("invented_numbers") else []
        if checks.get("unknown_words"):
            flags.append(f"words not in the PDF: {', '.join(checks['unknown_words'][:12])}")
        rows.append(
            f'<tr><td><a target=_blank href="/file?path={esc(resumes.folder() / v["source"])}">{esc(v["source"])}</a>'
            f'<div class=muted>{esc(v.get("notes") or "")}</div>'
            f'{"".join(f"<div class=warn>{esc(f)}</div>" for f in flags)}</td>'
            f'<td>{esc(v["latest_date"] or "unknown")}</td>'
            f'<td>{form(app, "/resumes/remove", "Remove", {"stem": v["stem"]})}</td></tr>')
    proposal = resumes.proposal()
    review = ""
    if proposal:
        master = resumes.current_master() or ""
        diff = []
        for line in difflib.unified_diff(master.splitlines(), proposal["markdown"].splitlines(), lineterm="", n=2):
            if line.startswith(("---", "+++", "@@")):
                continue
            kind = "add" if line.startswith("+") else "del" if line.startswith("-") else ""
            diff.append(f'<div class="{kind}">{esc(line)}</div>')
        checks = proposal["checks"]
        warn = []
        if checks.get("invented_numbers"):
            warn.append(f"Numbers that are in none of your resumes: {', '.join(checks['invented_numbers'])}")
        if checks.get("unknown_words"):
            warn.append(f"Words that are in none of your resumes: {', '.join(checks['unknown_words'][:20])}")
        review = f"""<div class=card><h2>Proposed master resume</h2>
        <p class=muted>Merged from, oldest first: {esc("; ".join(proposal["sources"]))}</p>
        {"".join(f'<div class="flash bad">{esc(w)}</div>' for w in warn)}
        <p><b>How it was merged:</b></p><ul>{"".join(f"<li>{esc(c)}</li>" for c in proposal["changes"])}</ul>
        <p><b>Kept from older versions</b> <span class=muted>(nothing in the newest version
        covers these; if one says the same thing as a newer line, delete it below)</span>:</p>
        <ul>{"".join(f"<li>{esc(k['line'])} <span class=muted>({esc(k['from'])})</span></li>" for k in proposal.get("kept", [])) or "<li>None</li>"}</ul>
        <div class=row style="margin:12px 0">{form(app, "/resumes/accept", "Accept as master resume", primary=True)}
        {form(app, "/resumes/discard", "Discard")}</div>
        <details open><summary>Changes against your current master</summary>
        <pre class=diff>{"".join(diff) or "No differences."}</pre></details>
        <details><summary>Edit the proposed resume</summary>
        <form method=post action="/resumes/edit"><input type=hidden name=token value="{app.token}">
        <textarea name=markdown style="min-height:520px;font:12px/1.5 ui-monospace,monospace">{esc(proposal["markdown"])}</textarea>
        <div class=row style="margin-top:8px"><button>Save edits</button>
        <span class=muted>The truth checks run again on your edits.</span></div></form></details></div>"""
    master = resumes.current_master()
    derived = config.PROFILE_DIR / "resume-profile.yaml"
    facts = (f'<div class=card><h2>Profile from your resume</h2><p class=muted>Used to answer '
             f'application questions. Updated each time you accept a master resume.</p>'
             f'<pre>{esc(derived.read_text(encoding="utf-8"))}</pre></div>') if derived.exists() else ""
    body = f"""<h1>Resumes</h1>
    <div class=card><h2>Add resumes</h2>
    <p class=muted>Upload one or more resume PDFs. Each is read and turned into Markdown with its
    wording kept, then all are merged into one master resume: everything from every version, with
    the newest version winning where they disagree. You review the result before it replaces
    your master resume, and the old one is kept in profile/resume-history/.</p>
    <form method=post action="/resumes/upload" enctype="multipart/form-data">
    <input type=hidden name=token value="{app.token}">
    <div class=row><input type=file name=pdf accept="application/pdf" multiple required>
    <button class=primary>Upload and merge</button></div></form></div>
    <div class=card><h2>Your resumes <span class=muted>(oldest content first)</span></h2>
    <table><tr><th>File</th><th>Content through</th><th></th></tr>
    {"".join(rows) or '<tr><td colspan=3 class=muted>None yet.</td></tr>'}</table>
    {'<div class=row style="margin-top:12px">' + form(app, "/resumes/merge", "Merge again") + '</div>' if versions else ''}</div>
    {review}
    {facts}
    <div class=card><h2>Current master resume</h2>
    {f"<pre>{esc(master)}</pre>" if master else '<p class=muted>Still the example from setup. Upload your resumes above.</p>'}</div>"""
    running = any(t["status"] in ("queued", "running") and t["kind"] in ("import_resume", "merge_resumes")
                  for t in app.tracker.tasks(10))
    return layout(app, "/resumes", body, flash, refresh=running)


OUTCOME_NAMES = {"applied": "applied, no answer yet", "oa": "online assessment", "interview": "interview",
                 "offer": "offer", "rejected": "rejected", "withdrawn": "withdrawn",
                 "no_response": "no response"}


def page_applications(app: Dashboard, show: str, flash: str) -> bytes:
    stats = history.summary(app.tracker)
    tiles = "".join(f'<a class="stat" href="/applications?outcome={o}" style="text-decoration:none;color:inherit">'
                    f'<b>{stats["counts"][o]}</b><span class=muted>{OUTCOME_NAMES[o]}</span></a>'
                    for o in OUTCOMES if stats["counts"][o] or o in ("applied", "interview", "offer"))
    rows = []
    for job in app.tracker.applications():
        outcome = job["outcome"] or "applied"
        if show and outcome != show:
            continue
        options = "".join(f'<option value="{o}"{" selected" if o == outcome else ""}>{OUTCOME_NAMES[o]}</option>'
                          for o in OUTCOMES)
        date = (job["submitted_at"] or "")[:10]
        unknown = "" if job["applied_date_known"] else ' <span class=warn title="Date unknown: imported without one">?</span>'
        rows.append(
            f'<tr><td style="white-space:nowrap">{date}{unknown}</td>'
            f'<td><a href="/job/{job["id"]}">{esc(job["company"])}</a><div class=muted>{esc(job["title"])}</div></td>'
            f'<td class=muted>{esc(job["applied_via"] or "")}</td>'
            f'<td><form method=post action="/outcome" class=row><input type=hidden name=token value="{app.token}">'
            f'<input type=hidden name=job_id value="{job["id"]}"><select name=outcome style="width:auto">{options}</select>'
            f'<input type=text name=note placeholder="note (optional)" style="width:160px">'
            f'<button>Save</button></form>'
            f'<div class=muted style="font-size:12px">since {(job["outcome_at"] or "")[:10]}</div></td></tr>')
    body = f"""<h1>Applications</h1>
    <div class=stats>{tiles}</div>
    <p class=muted>{stats["total"]} applications · {stats["response_rate"]}% heard back ·
    {stats["positive_rate"]}% reached an assessment, interview or offer.
    {'<a href="/applications">Show all</a>' if show else ''}</p>
    <div class=card><table><tr><th>Applied</th><th>Job</th><th>How</th><th>Outcome</th></tr>
    {"".join(rows) or '<tr><td colspan=4 class=muted>No applications yet. Import your job sheet below.</td></tr>'}
    </table><p class=muted style="margin-top:10px"><span class=warn>?</span> = date unknown (imported
    without one; counted from the import day for company caps). Set the real date on the job's page.</p></div>
    <div class=card><h2>Import or export</h2>
    <p class=muted>Import a CSV job sheet with a link, company, role and a status column
    (Applied, OA, Interview, Offer, Rejected…). Rows matching jobs in your lists are marked applied, so
    the agent never prepares them. Importing again only adds what's new.</p>
    <form method=post action="/applications/import" enctype="multipart/form-data" class=row>
    <input type=hidden name=token value="{app.token}"><input type=file name=sheet accept=".csv,text/csv" required>
    <input type=text name=status_column placeholder="status column (optional)" style="width:200px">
    <button class=primary>Import</button></form>
    <p style="margin-top:12px"><a class=button href="/applications.csv">Export as CSV</a></p></div>"""
    return layout(app, "/applications", body + page_passwords(), flash)


def application_block(app: Dashboard, job: dict) -> str:
    """Outcome and history on a job's page, once you've applied."""
    if job["status"] != "submitted":
        return ""
    events = "".join(f'<tr><td class=muted style="white-space:nowrap">{esc(e["at"][:10])}</td>'
                     f'<td>{esc(OUTCOME_NAMES.get(e["kind"], e["kind"]))}</td><td class=muted>{esc(e["note"] or "")}</td></tr>'
                     for e in app.tracker.events(job["id"]))
    date = (job["submitted_at"] or "")[:10]
    return (f'<div class=card><h2>Application</h2><p>Applied {date}'
            f'{" (date unknown)" if not job["applied_date_known"] else ""} · '
            f'{esc(OUTCOME_NAMES.get(job["outcome"] or "applied"))}</p>'
            f'<form method=post action="/applied-date" class=row><input type=hidden name=token value="{app.token}">'
            f'<input type=hidden name=job_id value="{job["id"]}"><input type=date name=date value="{date}" style="width:auto">'
            f'<button>Set applied date</button></form>'
            f'<table style="margin-top:10px"><tr><th>When</th><th>What</th><th>Note</th></tr>{events}</table></div>')


def page_run(app: Dashboard, name: str) -> bytes:
    from .harness import read_trace
    from .planner import trace_path

    if "-" in name:  # An exploration or research run: shown like a run, titled by its job.
        kind, job_id = name.split("-")
        job = app.tracker.get(int(job_id))
        doing = "Fill in the application" if kind == "explore" else "Research the company"
        task = {"id": name, "status": "done", "question": None,
                "arg": f"{doing} for {job['title']} at {job['company']}" if job else ""}
    else:
        task = app.tracker.task(int(name))
    if task is None:
        return layout(app, "/", "<h1>No such run</h1>")
    task_id = task["id"]
    rows = []
    for entry in read_trace(trace_path(name)):
        kind, data = entry["kind"], entry["data"]
        if kind == "thought":
            rows.append(f'<div class=card style="margin:0 0 8px">{esc(data)}</div>')
        elif kind == "call":
            args = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)[:120]}" for k, v in data["args"].items())
            rows.append(f'<div style="margin:0 0 4px"><b>→ {esc(data["name"])}</b>'
                        f'<span class=muted>({esc(args)})</span></div>')
        elif kind == "result":
            ok = data.get("ok")
            body = data.get("result") if ok else data.get("error")
            text = json.dumps(body, ensure_ascii=False, indent=1) if not isinstance(body, str) else body
            rows.append(f'<details style="margin:0 0 10px 18px"><summary class="{"muted" if ok else "bad"}">'
                        f'{"result" if ok else "error"}: {esc(text[:140])}</summary><pre>{esc(text[:6000])}</pre></details>')
        elif kind == "finish":
            rows.append(f'<div class=card><h2>Report</h2>{esc(data)}</div>')
    running = task["status"] in ("queued", "running", "waiting")
    waiting = (f'<div class="flash warn">Waiting for you on <a href="/">Today</a>: {esc(task["question"])}</div>'
               if task["status"] == "waiting" else "")
    body = (f'<h1>Agent run {task_id} <span class="pill {esc(task["status"])}">{esc(task["status"])}</span></h1>'
            f'<p class=muted>Goal: {esc(task["arg"])}</p>{waiting}'
            + ("".join(rows) or "<p class=muted>Starting…</p>"))
    return layout(app, "/", body, refresh=running and task["status"] != "waiting")


def page_activity(app: Dashboard) -> bytes:
    text = LOG_PATH.read_text(encoding="utf-8")[-20000:] if LOG_PATH.exists() else "No activity yet."
    running = app.worker is not None and app.worker.poll() is None
    body = (f'<h1>Activity</h1><p class=muted>Worker {"running" if running else "not running"}. '
            f'Full step logs are in data/logs/.</p><div class=card><pre>{esc(text)}</pre></div>')
    return layout(app, "/activity", body, refresh=running)


# ----- requests -------------------------------------------------------------

def handler_for(app: Dashboard):
    allowed_hosts = {f"127.0.0.1:{app.port}", f"localhost:{app.port}"}
    origin = f"http://127.0.0.1:{app.port}"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, body: bytes, status: int = 200, ctype: str = "text/html; charset=utf-8",
                 headers: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if not config.HOSTED:  # The class portal may show the app in a frame.
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Content-Security-Policy",
                                 "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def host_ok(self) -> bool:
            # Hosted, the platform fronts the app and the session token guards changes.
            return config.HOSTED or self.headers.get("Host", "") in allowed_hosts

        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == "/healthz":
                return self.send(b"ok", ctype="text/plain")
            if not self.host_ok():
                return self.send(b"Forbidden", 403, "text/plain")
            if url.path == "/style.css":
                return self.send(CSS.encode(), ctype="text/css; charset=utf-8")
            if url.path == "/forms.js":
                return self.send(FORMS_JS.encode(), ctype="text/javascript; charset=utf-8")
            if url.path == "/export.zip" and config.HOSTED:
                return self.send(export_zip(), ctype="application/zip",
                                 headers={"Content-Disposition": 'attachment; filename="job-agent-session.zip"'})
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            flash = query.get("msg", "")
            parts = url.path.strip("/").split("/")
            if url.path == "/":
                return self.send(page_today(app, flash))
            if url.path == "/queue":
                return self.send(page_queue(app, flash))
            if url.path == "/jobs":
                return self.send(page_jobs(app, query.get("status", "active"), flash))
            if url.path == "/activity":
                return self.send(page_activity(app))
            if url.path == "/search":
                return self.send(page_search(app, flash))
            if url.path == "/resumes":
                return self.send(page_resumes(app, flash))
            if url.path == "/applications":
                return self.send(page_applications(app, query.get("outcome", ""), flash))
            if url.path == "/applications.csv":
                target = config.DATA_DIR / "applications-export.csv"
                history.export_sheet(target, app.tracker)
                return self.send(target.read_bytes(), ctype="text/csv",
                                 headers={"Content-Disposition": 'attachment; filename="applications.csv"'})
            if parts[0] == "runs" and len(parts) == 2 and re.fullmatch(r"((explore|research)-)?\d+", parts[1]):
                return self.send(page_run(app, parts[1]))
            if parts[0] == "job" and len(parts) == 2 and parts[1].isdigit():
                return self.send(page_job(app, int(parts[1]), flash))
            if url.path == "/file":
                return self.send_file(query.get("path", ""))
            self.send(b"Not found", 404, "text/plain")

        def send_file(self, raw: str):
            roots = {(config.DATA_DIR / "applications").resolve(), resumes.folder().resolve()}
            path = Path(raw).resolve()
            if (not roots & set(path.parents) or not path.is_file()
                    or path.suffix not in (".pdf", ".png")):
                return self.send(b"Not found", 404, "text/plain")
            kind = "application/pdf" if path.suffix == ".pdf" else "image/png"
            self.send(path.read_bytes(), ctype=kind)

        def do_POST(self):
            sent_origin = self.headers.get("Origin")
            if not self.host_ok() or (not config.HOSTED and sent_origin
                                      and sent_origin not in (origin, f"http://localhost:{app.port}")):
                return self.send(b"Forbidden", 403, "text/plain")
            size = int(self.headers.get("Content-Length", 0))
            if size > 30 * 1024 * 1024:
                return self.send(b"Too large.", 413, "text/plain")
            raw = self.rfile.read(size)
            ctype = self.headers.get("Content-Type", "")
            self.uploads = []
            if ctype.startswith("multipart/form-data"):
                fields = self.multipart(raw, ctype)
            else:
                fields = parse_qs(raw.decode())
            one = {k: v[0] for k, v in fields.items()}
            if not secrets.compare_digest(one.get("token", ""), app.token):
                return self.send(b"Forbidden: reload the dashboard.", 403, "text/plain")
            referer = urlsplit(self.headers.get("Referer") or "/")
            keep = {k: v[0] for k, v in parse_qs(referer.query).items() if k != "msg"}
            back = (referer.path or "/") + (f"?{urlencode(keep)}" if keep else "")
            try:
                message, back = self.act(urlsplit(self.path).path, one, fields, back)
            except ValueError as error:
                message = str(error)
            joiner = "&" if "?" in back else "?"
            self.send(b"", 303, headers={"Location": f"{back}{joiner}msg={quote(message[:500])}"})

        def multipart(self, raw: bytes, ctype: str) -> dict:
            message = BytesParser(policy=email_policy).parsebytes(
                f"Content-Type: {ctype}\r\n\r\n".encode() + raw)
            fields: dict[str, list[str]] = {}
            for part in message.iter_parts():
                name = part.get_param("name", header="content-disposition")
                if part.get_filename():
                    self.uploads.append((part.get_filename(), part.get_payload(decode=True) or b""))
                elif name:
                    fields.setdefault(name, []).append(part.get_content().strip())
            return fields

        def act(self, path: str, one: dict, fields: dict, back: str) -> tuple[str, str]:
            tracker = app.tracker
            ids = [int(i) for raw in fields.get("ids", []) for i in raw.split(",") if i.strip().isdigit()]
            if path == "/refresh":
                app.ensure_worker()
                tracker.add_task("refresh")
                return "Refreshing the job lists.", "/"
            if path == "/plan":
                goal = one.get("goal", "").strip()
                if not goal:
                    raise ValueError("Describe what the agent should do.")
                app.ensure_worker()
                task_id = tracker.add_task("plan", arg=goal[:1000])
                return "The agent is working on it. Follow along here.", f"/runs/{task_id}"
            if path == "/prepare":
                app.ensure_worker()
                tracker.add_task("prepare", int(one["job_id"]))
                return "Queued for preparation.", back
            if path == "/prepare-top":
                count = max(1, min(20, int(one.get("count") or 3)))
                found = [j for j in tracker.with_status("found") if j["ats"] in systems.SUPPORTED]
                found.sort(key=lambda j: (-(matching.ranking_score(j)[0] or -1),
                                          -matching._epoch(j["posted_at"])))
                picks = found[:count]
                app.ensure_worker()
                for job in picks:
                    tracker.add_task("prepare", job["id"])
                return f"Queued the {len(picks)} best-ranked job(s) for preparation.", "/"
            if path == "/score":
                app.ensure_worker()
                tracker.add_task("score_matches")
                return "Ranking jobs against your description. This page shows the scores as they arrive.", "/jobs"
            if path == "/search":
                def words(key):
                    return [w.strip() for w in one.get(key, "").split(",") if w.strip()]

                threshold = int(one.get("fit_threshold") or app.settings.fit_threshold)
                age = one.get("max_posting_age_days", "").strip()
                config.save_search({
                    "description": one.get("description", "").strip(),
                    "roles": words("roles"),
                    "job_types": fields.get("job_types", []),
                    "locations": [l.strip() for l in one.get("locations", "").splitlines() if l.strip()],
                    "terms": words("terms"),
                    "max_posting_age_days": int(age) if age.isdigit() else None,
                    "exclude_companies": words("exclude_companies"),
                    "exclude_title_words": words("exclude_title_words"),
                    "fit_threshold": max(0, min(100, threshold)),
                    "resume_pages": 2 if one.get("resume_pages") == "2" else 1,
                })
                before = len(tracker.with_status("found"))
                sources.apply_filters(tracker, config.load_profile())
                # A lower threshold brings back jobs whose fit was below the old one.
                for job in tracker.with_status("filtered_out"):
                    if (job["reason"] or "").startswith("Fit ") and (job["fit_score"] or 0) >= threshold:
                        tracker.update(job["id"], status="found", reason="Back: above your new threshold.")
                after = len(tracker.with_status("found"))
                return (f"Saved. {after} job(s) pass your filters ({after - before:+d}). "
                        "Rank them on the Ranked jobs page."), "/search"
            if path == "/draft":
                app.ensure_worker()
                tracker.add_task("draft", int(one["job_id"]))
                return "Drafting the missing answers. You'll review them before anything is sent.", back
            if path == "/approve":
                done, errors = 0, []
                for job_id in ids:
                    try:
                        actions.approve(tracker, app.settings, job_id)
                        done += 1
                    except ValueError as error:
                        errors.append(f"#{job_id}: {error}")
                return " ".join([f"Approved {done}."] + errors), back
            if path == "/submit":
                app.ensure_worker()
                for job_id in ids:
                    tracker.add_task("submit", job_id)
                where = "this page" if config.HOSTED else "the Chrome window and this page"
                return f"Submitting {len(ids)}. Watch {where}.", "/"
            if path == "/edit":
                value = fields.get("value", [""])
                value = value if len(value) > 1 else value[0]
                actions.edit_answer(tracker, int(one["job_id"]), one["key"], value,
                                    remember=bool(one.get("remember")))
                return "Saved.", back
            if path == "/reject":
                actions.reject(tracker, int(one["job_id"]))
                return "Rejected.", back
            if path == "/resolve":
                actions.resolve(tracker, int(one["job_id"]), one["outcome"])
                return "Updated.", back
            if path == "/requeue":
                actions.requeue(tracker, int(one["job_id"]))
                return "Back in found.", back
            if path == "/outcome":
                tracker.set_outcome(int(one["job_id"]), one["outcome"], None, one.get("note", ""))
                return "Saved.", back
            if path == "/applied-date":
                tracker.set_applied_date(int(one["job_id"]), one["date"] + "T12:00:00+00:00")
                return "Applied date saved.", back
            if path == "/mark-applied":
                tracker.mark_applied(int(one["job_id"]), None, "you", "Marked applied by you.")
                return "Recorded as applied (dated today; set the real date below).", back
            if path == "/applications/import":
                if not self.uploads:
                    raise ValueError("Choose a CSV file.")
                name, data = self.uploads[0]
                target = config.DATA_DIR / "imports" / Path(name).name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                report = history.import_sheet(target, tracker, one.get("status_column") or None)
                return (f"From the “{report['status_column']}” column: {report['added']} added, "
                        f"{report['updated']} outcomes updated, {report['unchanged']} already recorded, "
                        f"{len(report['skipped'])} skipped (no status for you)."), "/applications"
            if path == "/resumes/upload":
                saved = [resumes.save_upload(name, data) for name, data in self.uploads]
                if not saved:
                    raise ValueError("Choose at least one PDF.")
                app.ensure_worker()
                for pdf in saved:
                    tracker.add_task("import_resume", arg=str(pdf))
                tracker.add_task("merge_resumes")
                return f"Reading {len(saved)} resume(s), then merging. This page updates itself.", "/resumes"
            if path == "/resumes/merge":
                app.ensure_worker()
                tracker.add_task("merge_resumes")
                return "Merging.", "/resumes"
            if path == "/resumes/remove":
                resumes.remove(one["stem"])
                return "Removed. Merge again to update the proposal.", "/resumes"
            if path == "/resumes/accept":
                backup = resumes.accept()
                app.ensure_worker()
                tracker.add_task("sync_profile")
                return ("Saved as your master resume; updating your profile from it."
                        + (f" The previous one is in {backup.relative_to(config.HOME)}." if backup else "")), "/resumes"
            if path == "/resumes/edit":
                resumes.edit_proposal(one.get("markdown", ""))
                return "Saved your edits to the proposal.", "/resumes"
            if path == "/resumes/discard":
                resumes.discard()
                return "Discarded the proposal.", "/resumes"
            if path == "/reply":
                tracker.set_task(int(one["task_id"]), answer=one.get("answer", ""))
                return "Sent to the agent.", "/"
            raise ValueError("Unknown action.")

    return Handler


def serve(settings, port: int = 8770, start_worker: bool = True, host: str = "127.0.0.1") -> None:
    app = Dashboard(settings, port)
    if start_worker:
        app.ensure_worker()
    server = ThreadingHTTPServer((host, port), handler_for(app))
    print(f"Dashboard: http://{host}:{port}/  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.stop_worker()
