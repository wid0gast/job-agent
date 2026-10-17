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

from . import actions, claude_login, config, history, matching, packets, resumes, sources, systems
from .tracker import OUTCOMES, Tracker
from .worker import DB_PATH

LOG_PATH = config.DATA_DIR / "logs" / "worker.log"
QUEUE = ("prepared", "needs_you", "approved", "submitting")
ORDER = ("found", "preparing", "prepared", "needs_you", "approved", "submitting", "submitted",
         "filtered_out", "failed", "rejected", "skipped", "closed", "working")

# A briefcase with a spark: the app's mark, inline so the page loads nothing else.
LOGO = ('<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" '
        'stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="7" width="18" height="13" rx="2.5"/>'
        '<path d="M9 7V5.5A1.5 1.5 0 0 1 10.5 4h3A1.5 1.5 0 0 1 15 5.5V7"/><path d="M3 13h18"/></svg>')

CSS = """
:root{--bg:#f6f7fb;--card:#fff;--text:#0f172a;--muted:#64748b;--line:#e4e7ef;--soft:#f1f3f9;
--accent:#4f46e5;--accent-2:#7c3aed;--accent-soft:#eef0ff;--good:#059669;--good-soft:#e7f8f1;
--bad:#dc2626;--bad-soft:#fdecec;--warn:#b45309;--warn-soft:#fff5e5;--info:#0369a1;--info-soft:#e8f4fb;
--shadow:0 1px 2px rgba(15,23,42,.04),0 4px 16px rgba(15,23,42,.05);--radius:14px}
@media (prefers-color-scheme:dark){:root{--bg:#0b0d14;--card:#141824;--text:#e6e8f0;--muted:#94a0b8;
--line:#242a3a;--soft:#1a1f2e;--accent:#8b85ff;--accent-2:#b18cff;--accent-soft:#1f2147;--good:#34d399;
--good-soft:#0f2a22;--bad:#f87171;--bad-soft:#2d1517;--warn:#fbbf24;--warn-soft:#2b2210;--info:#60a5fa;
--info-soft:#12233a;--shadow:0 1px 2px rgba(0,0,0,.3),0 6px 20px rgba(0,0,0,.25)}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);-webkit-font-smoothing:antialiased;
font:14px/1.55 "Inter",ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
header{position:sticky;top:0;z-index:5;display:flex;gap:6px;align-items:center;padding:10px 24px;
background:color-mix(in srgb,var(--card) 88%,transparent);backdrop-filter:blur(10px);
border-bottom:1px solid var(--line);flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:9px;margin-right:14px;font-weight:700;font-size:15px;
color:var(--text);letter-spacing:-.01em}.brand:hover{text-decoration:none}
.logo{width:28px;height:28px;border-radius:8px;display:grid;place-items:center;color:#fff;
background:linear-gradient(135deg,var(--accent),var(--accent-2));box-shadow:0 2px 8px rgba(79,70,229,.35)}
header nav{display:flex;gap:2px;flex-wrap:wrap}
header nav a{color:var(--muted);padding:6px 11px;border-radius:9px;font-weight:500}
header nav a:hover{background:var(--soft);color:var(--text);text-decoration:none}
header nav a.on{background:var(--accent-soft);color:var(--accent)}
.chip{margin-left:auto;display:inline-flex;align-items:center;gap:7px;padding:5px 11px;border-radius:99px;
border:1px solid var(--line);background:var(--card);color:var(--text);font-size:12.5px;font-weight:500}
.chip:hover{text-decoration:none;border-color:var(--accent)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}.dot.on{background:var(--good);
box-shadow:0 0 0 3px var(--good-soft)}.dot.off{background:var(--bad);box-shadow:0 0 0 3px var(--bad-soft)}
main{max-width:1160px;margin:0 auto;padding:28px 24px 48px}
h1{font-size:24px;letter-spacing:-.02em;margin:0 0 18px}h2{font-size:15px;margin:0 0 12px;letter-spacing:-.01em}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:20px;
margin:0 0 18px;box-shadow:var(--shadow)}
table{width:100%;border-collapse:separate;border-spacing:0}
td,th{text-align:left;padding:11px 10px;border-top:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em;border-top:0}
tbody tr:hover td,table tr:hover td{background:color-mix(in srgb,var(--soft) 70%,transparent)}
td a{font-weight:600;color:var(--text)}td a:hover{color:var(--accent)}
.pill{display:inline-block;padding:2px 9px;border-radius:99px;background:var(--soft);color:var(--muted);
font-size:12px;font-weight:600;white-space:nowrap}
.pill.found{background:var(--info-soft);color:var(--info)}
.pill.preparing,.pill.working,.pill.running,.pill.queued,.pill.submitting{background:var(--accent-soft);color:var(--accent)}
.pill.prepared{background:var(--accent-soft);color:var(--accent)}
.pill.approved,.pill.submitted,.pill.done{background:var(--good-soft);color:var(--good)}
.pill.needs_you,.pill.waiting{background:var(--warn-soft);color:var(--warn)}
.pill.failed,.pill.rejected{background:var(--bad-soft);color:var(--bad)}
.submitted,.approved,.good{color:var(--good)}.failed,.needs_you,.rejected{color:var(--bad)}.prepared{color:var(--accent)}
.muted{color:var(--muted)}.warn{color:var(--warn)}.bad{color:var(--bad)}
button,.button{font:inherit;font-weight:600;padding:8px 14px;border-radius:10px;border:1px solid var(--line);
background:var(--card);color:var(--text);cursor:pointer;text-decoration:none;display:inline-block;
transition:background .15s,border-color .15s,transform .05s}
button:hover,.button:hover{border-color:color-mix(in srgb,var(--accent) 45%,var(--line));text-decoration:none}
button:active{transform:translateY(1px)}
button.primary{background:linear-gradient(135deg,var(--accent),var(--accent-2));border-color:transparent;color:#fff;
box-shadow:0 2px 10px rgba(79,70,229,.28)}button.primary:hover{filter:brightness(1.06)}
input[type=text],input[type=number],input[type=password],input[type=email],textarea,select{font:inherit;width:100%;
padding:9px 11px;border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--text);
transition:border-color .15s,box-shadow .15s}
input[type=file]{font:inherit;color:var(--muted)}
input[type=file]::file-selector-button{font:inherit;font-weight:600;margin-right:12px;padding:8px 14px;border-radius:10px;
border:1px dashed color-mix(in srgb,var(--accent) 50%,var(--line));background:var(--accent-soft);color:var(--accent);cursor:pointer}
input:focus,textarea:focus,select:focus{outline:none;border-color:var(--accent);
box-shadow:0 0 0 3px color-mix(in srgb,var(--accent) 18%,transparent)}textarea{min-height:90px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:18px}
.stat{position:relative;padding:16px 16px 14px;border:1px solid var(--line);border-radius:var(--radius);
background:var(--card);box-shadow:var(--shadow);overflow:hidden;transition:transform .12s,border-color .12s}
.stat:hover{transform:translateY(-2px);border-color:color-mix(in srgb,var(--accent) 40%,var(--line))}
.stat::before{content:"";position:absolute;inset:0 0 auto 0;height:3px;background:var(--muted)}
.stat b{display:block;font-size:28px;letter-spacing:-.02em;line-height:1.15}
.stat span{font-size:12.5px;font-weight:500;text-transform:capitalize}
.st-found::before{background:var(--info)}.st-prepared::before{background:var(--accent)}
.st-needs_you::before{background:var(--warn)}.st-approved::before{background:var(--good)}
.st-submitted::before{background:linear-gradient(90deg,var(--good),var(--accent))}
.flash{padding:12px 16px;border-radius:12px;margin-bottom:16px;background:var(--info-soft);color:var(--text);
border:1px solid color-mix(in srgb,var(--info) 25%,transparent)}
.flash.warn{background:var(--warn-soft);border-color:color-mix(in srgb,var(--warn) 30%,transparent)}
.flash.bad{background:var(--bad-soft);border-color:color-mix(in srgb,var(--bad) 30%,transparent)}
.flash.good{background:var(--good-soft);border-color:color-mix(in srgb,var(--good) 30%,transparent)}
.flash.warn,.flash.bad{color:var(--text)}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 16px}
.tab{padding:6px 12px;border-radius:99px;border:1px solid var(--line);background:var(--card);color:var(--muted);
font-weight:600;font-size:13px;text-transform:capitalize}
.tab:hover{color:var(--text);text-decoration:none;border-color:color-mix(in srgb,var(--accent) 40%,var(--line))}
.tab.on{background:var(--text);border-color:var(--text);color:var(--card)}
pre{white-space:pre-wrap;font:12px/1.55 ui-monospace,"SF Mono",monospace;max-height:520px;overflow:auto;
background:var(--soft);border-radius:10px;padding:12px}
code{font:12.5px ui-monospace,"SF Mono",monospace;background:var(--soft);padding:1px 6px;border-radius:6px}
.src{font-size:12px;color:var(--muted);margin-top:2px}
.below{opacity:.45}
.score{display:inline-grid;place-items:center;min-width:42px;height:30px;padding:0 8px;border-radius:9px;
font-weight:700;font-size:14.5px;background:var(--soft);color:var(--text)}
.score.b-high{background:var(--good-soft);color:var(--good)}.score.b-mid{background:var(--warn-soft);color:var(--warn)}
.score.b-low{background:var(--bad-soft);color:var(--bad)}
.kind{font-size:10.5px;color:var(--muted);display:block;margin-top:3px;text-transform:uppercase;letter-spacing:.05em}
.steps{counter-reset:step;list-style:none;padding:0;margin:0}
.steps li{counter-increment:step;display:flex;align-items:center;flex-wrap:wrap;gap:6px 14px;min-height:38px;
padding:0 0 12px}.steps li form.code{flex-basis:100%;margin-left:40px}
.steps li::before{content:counter(step);flex:none;width:26px;height:26px;border-radius:50%;
display:grid;place-items:center;font-weight:700;font-size:13px;background:var(--accent-soft);color:var(--accent)}
.steps li.done::before{content:"✓";background:var(--good-soft);color:var(--good)}
.diff .add{background:color-mix(in srgb,var(--good) 18%,transparent)}
.diff .del{background:color-mix(in srgb,var(--bad) 18%,transparent);text-decoration:line-through}
"""


def esc(value) -> str:
    return html.escape("" if value is None else str(value))


class Dashboard:
    def __init__(self, settings, port: int):
        self.settings, self.port = settings, port
        self.tracker = Tracker(DB_PATH)
        self.token = secrets.token_urlsafe(24)
        self.worker: subprocess.Popen | None = None
        from .llm import client

        llm = client(settings)
        self.backend = None if llm is None else type(llm).__name__  # ClaudeCode or AnthropicAPI
        self.model = llm.model if llm else ""  # Set only when CLAUDE_MODEL forces one model.
        self.plan = llm.plan() if llm else {}  # Task -> (model, effort)
        self._missing: bool | None = None  # Tests set llm_missing directly.

    @property
    def llm_missing(self) -> bool:
        """No usable connection: none set up, or Claude Code isn't signed in."""
        if self._missing is not None:
            return self._missing
        if self.backend == "ClaudeCode":
            return not claude_login.status()["loggedIn"]
        return self.backend is None

    @llm_missing.setter
    def llm_missing(self, value: bool) -> None:
        self._missing = value

    @property
    def connection(self) -> str:
        if self.backend == "AnthropicAPI":
            return "Anthropic API" + (f" · {self.model}" if self.model else "")
        if self.backend == "ClaudeCode":
            info = claude_login.status()
            if not info["loggedIn"]:
                return "Claude: not signed in"
            # The plan only: the header shows on every page (and in screenshots); the Connect page has the email.
            return claude_login.describe({**info, "email": ""}) + (f" · {self.model}" if self.model else "")
        return "Claude not connected"

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




def layout(app: Dashboard, active: str, body: str, flash: str = "", refresh: bool = False) -> bytes:
    links = [("/", "Today"), ("/search", "Search"), ("/jobs", "Ranked jobs"),
             ("/queue", "Approval queue"), ("/applications", "Applications"),
             ("/resumes", "Resumes"), ("/activity", "Activity")]
    nav = "".join(f'<a href="{h}" class="{"on" if h == active else ""}">{t}</a>' for h, t in links)
    chip = (f'<a class="chip" href="/connect"><span class="dot {"off" if app.llm_missing else "on"}"></span>'
            f'{esc(app.connection)}</a>')
    auto = '<meta http-equiv="refresh" content="4">' if refresh else ""
    banner = ('<div class="flash warn">Auto-submit is on.</div>'
              if app.settings.submit_mode == "auto" else "")
    banner += start_here(app, active)
    note = f'<div class="flash">{esc(flash)}</div>' if flash else ""
    return (f"<!doctype html><html><head><meta charset=utf-8><meta name=viewport "
            f"content='width=device-width,initial-scale=1'>{auto}<title>Job Agent</title>"
            f"<link rel=stylesheet href=/style.css></head><body><header>"
            f'<a class="brand" href="/"><span class="logo">{LOGO}</span>Job Agent</a><nav>{nav}</nav>{chip}</header>'
            f"<main>{banner}{note}{body}</main></body></html>").encode()


def start_here(app: Dashboard, active: str) -> str:
    """First-run guidance: connect Claude, then upload a resume and describe the jobs you want."""
    parts = []
    if app.llm_missing and active != "/connect":
        parts.append('<div class="flash bad"><b>Claude isn\'t connected yet.</b> '
                     '<a href="/connect">Connect Claude</a> to rank jobs and prepare applications.</div>')
    if not (config.PROFILE_DIR / "resume.md").exists() and active not in ("/resumes",):
        parts.append('<div class="flash warn">Start here: <a href="/resumes">upload your resume PDF(s)</a>, '
                     'accept the merged master resume, then describe the jobs you want on '
                     '<a href="/search">Search</a>. Optional: import your job sheet on '
                     '<a href="/applications">Applications</a>.</div>')
    return "".join(parts)


def form(app: Dashboard, action: str, button: str, fields: dict | None = None,
         primary: bool = False, extra: str = "") -> str:
    hidden = "".join(f'<input type=hidden name="{esc(k)}" value="{esc(v)}">'
                     for k, v in (fields or {}).items())
    return (f'<form method=post action="{action}" style="display:inline">'
            f'<input type=hidden name=token value="{app.token}">{hidden}{extra}'
            f'<button class="{"primary" if primary else ""}">{esc(button)}</button></form>')


# ----- pages ----------------------------------------------------------------


TASK_LABELS = {
    "match_scores": "Ranking job titles", "resume_markdown": "Reading resume PDFs",
    "resume_facts": "Profile facts from your resume", "research": "Company research (agent)",
    "planner": "Planner (agent)", "fit": "Fit check on the full posting", "tailored_resume": "Tailored resume",
    "cover_letter": "Cover letter", "form_answers": "Application answers and essays",
    "explore": "Filling unfamiliar forms (agent)",
}
MODEL_NAMES = {"claude-haiku-5-5": "Haiku 5.5", "claude-sonnet-5-5": "Sonnet 5.5", "claude-opus-5-5": "Opus 5.5"}


def models_card(app: Dashboard) -> str:
    """Which model and effort each task uses: lighter models for bulk work, Opus for what employers see."""
    if not app.plan:
        return ""
    rows = "".join(f"<tr><td>{TASK_LABELS.get(task, task)}</td><td><b>{esc(MODEL_NAMES.get(model, model))}</b></td>"
                   f"<td class=muted>{effort}</td></tr>"
                   for task, (model, effort) in sorted(app.plan.items(), key=lambda kv: (kv[1][0], kv[0])))
    how = ("<code>CLAUDE_MODEL</code> is set, so every task uses it." if app.model else
           "Bulk work goes to lighter models; what decides whether you apply, and what employers read, gets Opus. "
           "Change a tier with <code>CLAUDE_MODEL_LIGHT</code>, <code>_STANDARD</code> or <code>_BEST</code> in "
           "<code>.env</code>, or use one model everywhere with <code>CLAUDE_MODEL</code>.")
    return (f'<div class=card><h2>Which model does what</h2><p class=muted>{how}</p>'
            f'<table><tr><th>Task</th><th>Model</th><th>Effort</th></tr>{rows}</table></div>')


def page_connect(app: Dashboard, flash: str) -> bytes:
    """Connect Claude: your Claude Code sign-in (your plan), or an API key."""
    if app.backend == "AnthropicAPI":
        body = f"""<h1>Connect Claude</h1>
    <div class=card><h2><span class="dot on" style="display:inline-block;margin-right:8px"></span>Connected with an
    Anthropic API key</h2><p class=muted>Billed per token by Anthropic, with a
    spending cap of ${app.settings.spend_cap_usd:.2f} per run (<code>SPEND_CAP_USD</code>). To use your Claude plan
    instead, set <code>MODEL_BACKEND=claude-code</code> in <code>.env</code> and restart.</p></div>
    {models_card(app)}"""
        return layout(app, "/connect", body, flash)
    info = claude_login.status(fresh=True)
    login = claude_login.current()
    if not info["installed"]:
        state = """<h2><span class="dot off" style="display:inline-block;margin-right:8px"></span>Claude Code isn't
        installed</h2><p class=muted>Job Agent reaches Claude through Claude Code, signed in with your own Claude
        account. Install it (<a href="https://code.claude.com/docs/en/setup" target=_blank>instructions</a>), then
        reload this page.</p>"""
    elif info["loggedIn"]:
        state = (f'<h2><span class="dot on" style="display:inline-block;margin-right:8px"></span>Connected to '
                 f'{esc(claude_login.describe(info))}</h2><p class=muted>Each model request runs Claude Code headless '
                 f'with your sign-in: a fresh, unsaved session with its own tools off. Usage counts against your '
                 f'Claude plan; nothing is billed per token.</p>'
                 + form(app, "/connect/logout", "Sign out of Claude Code"))
    else:
        waiting = login is not None and login.running and login.url
        step2 = (f'<li>Approve on the Claude page (<a href="{esc(login.url)}" target=_blank>open it again</a>) '
                 f'and copy the code it shows.</li><li>Paste the code here:'
                 f'<form method=post action="/connect/code" class="row code">'
                 f'<input type=hidden name=token value="{app.token}">'
                 f'<input type=text name=code required autofocus placeholder="Code from the Claude page" '
                 f'style="max-width:420px"><button class=primary>Connect</button></form></li>'
                 if waiting else
                 '<li>Approve on the Claude page and copy the code it shows.</li><li>Paste the code here.</li>')
        state = (f'<h2><span class="dot off" style="display:inline-block;margin-right:8px"></span>Not signed in'
                 f'</h2><p class=muted>Sign in with your Claude account (Pro or Max) to use your plan. No API key.'
                 f'</p><ol class=steps><li class="{"done" if waiting else ""}">'
                 + (form(app, "/connect/start", "Sign in with Claude", primary=True) if not waiting else
                    "A Claude sign-in page opened in your browser.")
                 + f'</li>{step2}</ol>')
    body = f"""<h1>Connect Claude</h1>
    <div class=card>{state}</div>
    {models_card(app)}
    <div class=card><h2>Prefer an API key?</h2><p class=muted style="margin:0">Put
    <code>MODEL_BACKEND=anthropic</code> and <code>ANTHROPIC_API_KEY=…</code> in <code>.env</code> (or sign in with
    Anthropic's <code>ant auth login</code>) and restart. Billed per token, with <code>SPEND_CAP_USD</code> per run.
    Job Agent is a personal tool: use your own account or key on your own computer.</p></div>"""
    return layout(app, "/connect", body, flash)

def page_today(app: Dashboard, flash: str) -> bytes:
    jobs = app.tracker.all()
    counts = {s: 0 for s in ORDER}
    for job in jobs:
        counts[job["status"]] = counts.get(job["status"], 0) + 1
    stats = "".join(f'<a class="stat st-{s}" href="/jobs?status={s}" style="text-decoration:none;color:inherit">'
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
            reply = (f'<div class="card" style="margin:8px 0 0"><b>{esc(task["question"])}</b>'
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
                    f'<td class="muted">{esc((task["result"] or "")[:160])}</td></tr>')
    body = f"""
    <h1>Today</h1>
    <div class="stats">{stats}</div>
    <div class="card" style="margin-top:16px"><div class="row">
      {form(app, "/refresh", "Refresh job lists", primary=True)}
      {form(app, "/prepare-top", "Prepare top matches", extra='<input type=number name=count value=3 min=1 max=20 style="width:70px;margin-right:6px">')}
      <span class="muted">Submitted today: {app.tracker.submitted_today()} of {app.settings.daily_submit_limit}
      · {esc(app.connection)}</span>
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
    tabs = "".join(f'<a class="tab{" on" if key == status else ""}" href="/jobs?status={key}">{name}</a>'
                   for key, name in views)
    wanted = ACTIVE if status == "active" else tuple(ORDER) if status == "all" else (status,)
    jobs = app.tracker.with_status(*wanted)

    jobs.sort(key=matching.rank_key)
    unscored = len(matching.stale(app.tracker, profile, config.read_master_resume())) \
        if (profile.get("preferences", {}).get("description") or profile.get("preferences", {}).get("roles")) else None
    rows = []
    for job in jobs[:500]:
        score, kind = matching.ranking_score(job)
        why = json.loads(job["details"] or "{}").get("match", {}).get("why", "") if kind == "match" else ""
        if job["status"] == "found":
            act = prepare_button(app, job)
        elif job["status"] in ("failed", "rejected", "skipped", "closed", "filtered_out"):
            act = form(app, "/requeue", "Back to found", {"job_id": job["id"]})
        else:
            act = f'<a href="/job/{job["id"]}">open</a>'
        below = score is not None and kind != "keyword" and score < threshold
        band = ("" if score is None or kind == "keyword" else
                "b-high" if score >= 80 else "b-mid" if score >= threshold else "b-low")
        badge = (f'<span class="score {band}">{score}</span><span class=kind>{kind}</span>' if score is not None
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
        batch = min(unscored, matching.SHORTLIST)
        calls = -(-batch // matching.BATCH)
        scoring = (form(app, "/score", f"Rank the top {batch} with the model", primary=True)
                   + f' <span class=muted>{calls} model request(s), the best keyword matches first; '
                   f'{unscored} not yet ranked by the model</span>')
    else:
        scoring = '<span class=muted>Every job is ranked against your current description.</span>'
    body = f"""<h1>Ranked jobs</h1>
    <div class=card><div class=row>{scoring}</div>
    <p class=muted style="margin:10px 0 0">Score = <b>fit</b> (read the whole posting, when prepared),
    <b>match</b> (the model on title, company and location against your description) or
    <b>keyword</b> (free and instant: title words against your role titles and description; ranked below
    model scores). Greyed rows are below your
    threshold of <b>{threshold}</b> (<a href="/search">change</a>); jobs whose fit falls below it aren't prepared.</p></div>
    <div class="tabs">{tabs}</div>
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


def prepare_button(app: Dashboard, job: dict) -> str:
    """Coded systems prepare directly; others are filled by the browser agent (slower)."""
    if job["ats"] in systems.SUPPORTED:
        return form(app, "/prepare", "Prepare", {"job_id": job["id"]})
    return form(app, "/prepare", "Prepare (agent fills the form)", {"job_id": job["id"]})


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
        if job["status"] == "found":
            note += prepare_button(app, job)
        elif job["status"] in ("failed", "rejected", "skipped", "closed", "filtered_out"):
            note += form(app, "/requeue", "Back to found", {"job_id": job_id}) + " "
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
    needs = (f'<div class="flash warn"><b>{len(problems)} question(s) need your answer</b>'
             f'<ul style="margin:6px 0 0;padding-left:20px">'
             + "".join(f"<li>{esc(a['label'])}</li>" for a in problems) + "</ul></div>") if problems else ""
    warn += [esc(c) for c in packets.consistency(packet, config.load_profile())]
    if checks.get("resume_pages", 1) > 1:
        warn.append(f"The resume is {checks['resume_pages']} pages.")
    if checks.get("invented_numbers"):
        warn.append(f"Tailoring invented {esc(checks['invented_numbers'])}; your resume was used unchanged.")
    warn += [esc(n) for n in packet.get("form_notes", [])]
    noticed = ('<div class="card"><h2>What Claude noticed in the posting</h2><ul style="margin:0;padding-left:20px">'
               + "".join(f"<li>{esc(flag)}</li>" for flag in fit["flags"]) + "</ul></div>") if fit.get("flags") else ""
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
    {"".join(f'<div class="flash bad">{w}</div>' for w in warn)}{needs}
    <div class="row" style="margin-bottom:16px">{" ".join(buttons)} {docs}</div>
    {noticed}
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
            f'<div class="muted src">{esc(re.sub(r"[*`]+", "", v.get("notes") or ""))}</div>'
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
    {f"<pre>{esc(master)}</pre>" if master else '<p class=muted>None yet. Upload your resume above and accept the proposal.</p>'}</div>"""
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
    return layout(app, "/applications", body, flash)


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
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def host_ok(self) -> bool:
            return self.headers.get("Host", "") in allowed_hosts

        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == "/healthz":
                return self.send(b"ok", ctype="text/plain")
            if not self.host_ok():
                return self.send(b"Forbidden", 403, "text/plain")
            if url.path == "/style.css":
                return self.send(CSS.encode(), ctype="text/css; charset=utf-8")
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
            if url.path == "/connect":
                return self.send(page_connect(app, flash))
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
            if not self.host_ok() or (sent_origin and sent_origin not in (origin, f"http://localhost:{app.port}")):
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
            if path == "/connect/start":
                login = claude_login.start()
                if not login.url:
                    raise ValueError("Claude Code didn't start its sign-in: " + " ".join(login.output)[-200:])
                return "A Claude sign-in page opened in your browser. Approve it, then paste the code here.", "/connect"
            if path == "/connect/code":
                info = claude_login.finish(one.get("code", ""))
                return f"Connected: {claude_login.describe(info)}.", "/connect"
            if path == "/connect/logout":
                claude_login.logout()
                return "Signed out of Claude Code on this computer.", "/connect"
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
                found.sort(key=matching.rank_key)
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
                matching.keyword_rank(tracker, config.load_profile())
                # A lower threshold brings back jobs whose fit was below the old one.
                for job in tracker.with_status("filtered_out"):
                    if (job["reason"] or "").startswith("Fit ") and (job["fit_score"] or 0) >= threshold:
                        tracker.update(job["id"], status="found", reason="Back: above your new threshold.")
                if not tracker.with_status(*ORDER):  # Nothing fetched yet.
                    app.ensure_worker()
                    tracker.add_task("refresh")
                    return ("Saved. No jobs yet, so the job lists are being refreshed with these filters; "
                            "follow it on Today."), "/"
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
                return f"Submitting {len(ids)}. Watch the Chrome window and this page.", "/"
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
    config.ensure_profile()
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
