"""Local dashboard at http://127.0.0.1:<port>: today, the assistant chat, approval queue, jobs.

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

from . import actions, config, history, matching, packets, people, resumes, sources, systems, trace
from . import llm as llm_models
from .chat import ChatStore, Turns
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
*{box-sizing:border-box}[hidden]{display:none!important}
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
.chat{display:grid;grid-template-columns:230px minmax(0,1fr);gap:22px;align-items:start}
.chats{position:sticky;top:76px;display:flex;flex-direction:column;gap:2px;max-height:calc(100vh - 100px);overflow:auto}
.chats a.c{display:flex;align-items:center;gap:6px;padding:8px 10px;border-radius:9px;color:var(--text);font-weight:500;
font-size:13px}.chats a.c span{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chats a.c:hover{background:var(--soft);text-decoration:none}.chats a.c.on{background:var(--accent-soft);color:var(--accent)}
.chats a.c button{visibility:hidden;padding:0 6px;border:0;background:none;color:var(--muted);font-size:15px}
.chats a.c:hover button{visibility:visible}.chats .new{margin-bottom:10px;text-align:center}
.convo{max-width:820px;width:100%;margin:0 auto;display:flex;flex-direction:column;min-height:calc(100vh - 140px)}
#log{flex:1}
.item{margin:0 0 14px}.item.user{display:flex;justify-content:flex-end}
.bubble{max-width:78%;padding:10px 14px;border-radius:16px 16px 4px 16px;background:var(--accent-soft);
color:var(--text);white-space:normal;overflow-wrap:anywhere}
.md{line-height:1.6;overflow-wrap:anywhere}.md p{margin:0 0 10px}.md ul,.md ol{margin:0 0 10px;padding-left:22px}
.md h4{margin:14px 0 6px;font-size:14.5px}.md td a{color:var(--accent);font-weight:500}.md table{margin:0 0 12px;font-size:13px}.md td,.md th{padding:7px 8px}
.item.tool{margin:0 0 6px;font-size:13px}.item.tool summary{cursor:pointer;color:var(--text);list-style:none}
.item.tool summary::-webkit-details-marker{display:none}.item.tool pre{max-height:260px;margin:6px 0}
.state{display:inline-grid;place-items:center;width:18px;color:var(--good)}.state.error{color:var(--warn)}
.spin{display:inline-block;width:11px;height:11px;border:2px solid var(--line);border-top-color:var(--accent);
border-radius:50%;animation:spin .8s linear infinite;vertical-align:-1px}@keyframes spin{to{transform:rotate(360deg)}}
.card.ask{border-color:color-mix(in srgb,var(--accent) 45%,var(--line));margin:4px 0 14px}.card.ask ul{margin:8px 0 12px}
.undo{display:flex;gap:10px;align-items:center;font-size:13px;color:var(--muted)}.undo button{padding:3px 10px;font-size:12.5px}
.note{text-align:center;color:var(--muted);font-size:12.5px}
.stats-line{color:var(--muted);font-size:11.5px;margin:-8px 0 0;opacity:.8}
.thinking span{display:inline-block;width:7px;height:7px;margin-right:4px;border-radius:50%;background:var(--muted);
animation:blink 1.2s infinite}.thinking span:nth-child(2){animation-delay:.2s}.thinking span:nth-child(3){animation-delay:.4s}
@keyframes blink{0%,80%,100%{opacity:.25}40%{opacity:1}}
.composer{position:sticky;bottom:0;padding:14px 0 18px;background:linear-gradient(transparent,var(--bg) 28%)}
.composer .box{padding:10px;border:1px solid var(--line);border-radius:16px;background:var(--card);box-shadow:var(--shadow)}
.composer textarea{min-height:44px;max-height:220px;border:0;box-shadow:none;resize:none;padding:6px 8px}
.composer textarea:focus{box-shadow:none}.composer .row{justify-content:space-between}
.composer select{width:auto;padding:5px 8px;font-size:12.5px}
#tasks{position:sticky;top:64px;z-index:3}.task{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:8px 12px;margin:0 0 8px;
border:1px solid var(--line);border-radius:12px;background:var(--card);font-size:13px}
.task.waiting{background:var(--warn-soft);border-color:color-mix(in srgb,var(--warn) 35%,transparent)}
.task form{flex-basis:100%}.task input{max-width:320px}
.prompts{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px;margin-top:18px}
.prompts button{text-align:left;font-weight:500;padding:12px 14px}
@media (max-width:860px){.chat{grid-template-columns:1fr}.chats{position:static;max-height:none}}
.person{border-top:1px solid var(--line);padding:16px 0 4px}.person:first-of-type{border-top:0;padding-top:4px}
.person .draft{display:grid;gap:8px;margin:10px 0}.person .draft label{font-size:12px;color:var(--muted);font-weight:600}
.person textarea.short{min-height:60px}.person .sent{color:var(--good);font-weight:600}
.strip{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 16px}.strip a{display:flex;flex-direction:column;padding:8px 11px;
border-radius:10px;border:1px solid var(--line);background:var(--card);color:var(--text);font-size:12.5px;font-weight:600;min-width:96px}
.strip a span{font-weight:500;color:var(--muted);font-size:11.5px}.strip a:hover{text-decoration:none;border-color:var(--accent)}
.strip a.who-code{border-top:3px solid var(--good)}.strip a.who-model,.strip a.who-agent{border-top:3px solid var(--accent)}
.strip a.who-you{border-top:3px solid var(--warn)}.strip a.ok{border-top:3px solid var(--good)}.strip a.caught{border-top:3px solid var(--warn)}.strip a.stopped{border-top:3px solid var(--bad)}
.tl{list-style:none;margin:0;padding:0}.tl .tl{margin:6px 0 0 14px;padding-left:12px;border-left:2px solid var(--line)}
.tl li{margin:0 0 8px}.tl .head{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.tl .head b{font-size:14px}.tl .when{margin-left:auto;color:var(--muted);font-size:12px;white-space:nowrap}
.ev{font-size:13px;padding:5px 10px;border-radius:9px;background:var(--soft)}
.ev.pass{background:var(--good-soft)}.ev.caught{background:var(--warn-soft)}.ev.err{background:var(--bad-soft)}
.ev .tag{font-weight:700;margin-right:6px}.ev.pass .tag{color:var(--good)}.ev.caught .tag{color:var(--warn)}
.ev.model .tag{color:var(--accent)}.raw{display:none}.show-raw .raw{display:block}.raw pre{max-height:360px;margin:6px 0}
.only-checks .tl li.plain{display:none}.only-checks .tl li.step>.head{opacity:.6}
.totals{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:0 0 16px}
.totals div{padding:12px 14px;border:1px solid var(--line);border-radius:12px;background:var(--card)}
.totals b{display:block;font-size:20px}.totals span{font-size:12px;color:var(--muted)}
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
        from .llm import client

        llm = client(settings)
        self.backend = None if llm is None else type(llm).__name__  # Broker (class app store) or OpenRouter
        self.model = settings.model
        self.plan = llm.plan() if llm else {}  # Task -> (model, effort)
        self._missing: bool | None = None  # Tests set llm_missing directly.
        self.chats = ChatStore(DB_PATH)
        self.chats.recover()
        self.turns = Turns(self.chats, self.tracker, settings, lambda: self.ensure_worker())

    @property
    def llm_missing(self) -> bool:
        if self._missing is not None:
            return self._missing
        return self.backend is None

    @llm_missing.setter
    def llm_missing(self, value: bool) -> None:
        self._missing = value

    @property
    def connection(self) -> str:
        if self.backend is None:
            return "No model connected"
        name = self.model.split(",")[0].split("/")[-1]
        return ("Class models · " if self.backend == "Broker" else "OpenRouter · ") + name

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
    links = [("/", "Today"), ("/assistant", "Assistant"), ("/search", "Search"), ("/jobs", "Ranked jobs"),
             ("/queue", "Approval queue"), ("/applications", "Applications"),
             ("/resumes", "Resumes"), ("/profile", "Profile"), ("/activity", "Activity"), ("/how", "How it works")]
    nav = "".join(f'<a href="{h}" class="{"on" if h == active else ""}">{t}</a>' for h, t in links)
    chip = (f'<a class="chip" href="/connect"><span class="dot {"off" if app.llm_missing else "on"}"></span>'
            f'{esc(app.connection)}</a>')
    auto = '<meta http-equiv="refresh" content="4">' if refresh else ""
    banner = ('<div class="flash warn">Auto-submit is on.</div>'
              if app.settings.submit_mode == "auto" else "")
    if config.HOSTED:
        banner += hosted_banner()
    banner += start_here(app, active)
    note = f'<div class="flash">{esc(flash)}</div>' if flash else ""
    return (f"<!doctype html><html><head><meta charset=utf-8><meta name=viewport "
            f"content='width=device-width,initial-scale=1'>{auto}<title>Job Agent</title>"
            f"<link rel=stylesheet href=/style.css>{SCRIPT if config.HOSTED else ''}</head><body><header>"
            f'<a class="brand" href="/"><span class="logo">{LOGO}</span>Job Agent</a><nav>{nav}</nav>{chip}</header>'
            f"<main>{banner}{note}{body}</main></body></html>").encode()


def start_here(app: Dashboard, active: str) -> str:
    """First-run guidance: a model, then a resume and the jobs you want."""
    parts = []
    if app.llm_missing and active != "/connect":
        how = ("The class models aren't available this visit." if config.HOSTED else
               'Set <code>OPENROUTER_API_KEY</code> and <code>OPENROUTER_MODEL</code> in <code>.env</code> and restart.')
        parts.append(f'<div class="flash bad"><b>No model connected.</b> {how} '
                     '<a href="/connect">Details</a>.</div>')
    if not (config.PROFILE_DIR / "resume.md").exists() and active not in ("/resumes",):
        parts.append('<div class="flash warn">Start here: <a href="/resumes">upload your resume PDF(s)</a>, '
                     'accept the merged master resume, then describe the jobs you want on '
                     '<a href="/search">Search</a>. Optional: import your job sheet on '
                     '<a href="/applications">Applications</a>.</div>')
    return "".join(parts)


def hosted_banner() -> str:
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




def page_connect(app: Dashboard, flash: str) -> bytes:
    """Which model the app uses: the class broker's on the app store, OpenRouter on a laptop."""
    model = esc(app.model.split(",")[0]) if app.model else ""
    if app.backend == "Broker":
        state = (f'<h2><span class="dot on" style="display:inline-block;margin-right:8px"></span>Class models: '
                 f'{model}</h2><p class=muted>Requests go through the class model broker, with a budget of '
                 f'${app.settings.spend_cap_usd:.2f} for this visit. The platform allows about 20 model requests '
                 'per visit, two at a time, so ranking scores only the best keyword matches and each assistant '
                 'reply uses a few.</p>')
    elif app.backend == "OpenRouter":
        state = (f'<h2><span class="dot on" style="display:inline-block;margin-right:8px"></span>OpenRouter: '
                 f'{model}</h2><p class=muted>Set by <code>OPENROUTER_MODEL</code> in <code>.env</code> (a '
                 f'comma-separated list tries each in turn). Spending cap ${app.settings.spend_cap_usd:.2f} per run '
                 f'(<code>SPEND_CAP_USD</code>); providers that train on your data are '
                 f'{"excluded" if app.settings.data_collection == "deny" else "allowed"}.</p>')
    elif config.HOSTED:
        state = ('<h2><span class="dot off" style="display:inline-block;margin-right:8px"></span>No class model '
                 'this visit</h2><p class=muted>The class broker didn\'t give this visit a model. Reopen the app '
                 'from the class portal.</p>')
    else:
        state = ('<h2><span class="dot off" style="display:inline-block;margin-right:8px"></span>No model '
                 'connected</h2><p class=muted>Put <code>OPENROUTER_API_KEY</code> and <code>OPENROUTER_MODEL</code> '
                 'in <code>.env</code> (see <code>.env.example</code>) and restart.</p>')
    body = f"""<h1>Model</h1><div class=card>{state}<p class=muted style="margin:0">One model does every step:
    ranking, fit checks, tailored resumes, cover letters, answers, the agents and the assistant chat.</p></div>"""
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
            from .worker import LIVE_SCREENSHOT

            shot = (f' <a href="/file?path={quote(str(LIVE_SCREENSHOT))}" target=_blank>See the page</a>'
                    if config.HOSTED and LIVE_SCREENSHOT.exists() else "")
            reply = (f'<div class="card" style="margin:8px 0 0"><b>{esc(task["question"])}</b>{shot}'
                     + form(app, "/reply", "Send", {"task_id": task["id"]}, True,
                            '<input type=text name=answer autofocus style="max-width:260px;margin-right:8px">')
                     + "</div>")
        job = f' <a href="/job/{task["job_id"]}">job {task["job_id"]}</a>' if task["job_id"] else ""
        rows.append(f'<tr><td>{task["id"]}</td><td>{esc(task["kind"])}{job}</td>'
                    f'<td><span class="pill {esc(task["status"])}">{esc(task["status"])}</span>{reply}</td>'
                    f'<td class="muted">{esc((task["result"] or "")[:2000 if task["kind"] == "warmup" else 160])}</td></tr>')
    body = f"""
    <h1>Today</h1>
    <div class="stats">{stats}</div>
    <div class="card" style="margin-top:16px"><div class="row">
      {form(app, "/refresh", "Refresh job lists", primary=True)}
      {form(app, "/discover", "Discover companies")}
      {form(app, "/prepare-top", "Prepare top matches", extra='<input type=number name=count value=3 min=1 max=20 style="width:70px;margin-right:6px">')}
      <span class="muted">Submitted today: {app.tracker.submitted_today()} of {app.settings.daily_submit_limit}
      · model {esc(app.settings.model or "not set")}</span>
    </div></div>
    {outreach_card(app)}
    <div class="card"><h2>Ask the assistant</h2>
    <form method=get action="/assistant"><textarea name=q rows=2 required
    placeholder="Find my 5 best matches in NYC or remote, one per company, and prepare them."></textarea>
    <div class=row style="margin-top:8px"><button class=primary>Open in the assistant</button>
    <span class=muted>It can find, prepare and fix applications, edit your resume and profile, and search the web.
    Nothing is submitted until you confirm.</span></div></form></div>
    <div class="card"><h2>Tasks</h2><table><tr><th>#</th><th>Task</th><th>Status</th><th>Result</th></tr>
    {"".join(rows) or '<tr><td colspan=4 class=muted>Nothing yet. Refresh the job lists to start.</td></tr>'}
    </table></div>"""
    return layout(app, "/", body, flash, refresh=running)


def outreach_card(app: Dashboard) -> str:
    """Who to follow up with, or try next, from the people you emailed."""
    rows = []
    for due in people.due(app.tracker):
        job, person = due["job"], due["person"]
        where = f'<a href="/job/{job["id"]}#people">{esc(job["title"])}</a> at {esc(job["company"])}'
        if due["action"] == "follow_up":
            what = (f'Follow up with <b>{esc(person["name"])}</b>: emailed {due["days"]} days ago, no reply yet.')
            buttons = send_buttons(app, job["id"], person["id"], follow_up=True)
        else:
            what = (f'No reply in {due["days"]} days: try <b>{esc(person["name"])}</b> '
                    f'<span class=muted>({esc(people.KINDS[person["kind"]].lower())})</span>.')
            buttons = send_buttons(app, job["id"], person["id"])
        rows.append(f'<tr><td>{what}<div class=src>{where}</div></td><td><div class=row>{" ".join(buttons)}'
                    f'</div></td></tr>')
    if not rows:
        return ""
    return (f'<div class=card><h2>People to follow up with</h2><table>{"".join(rows)}</table>'
            f'<p class=muted>Sent today: {app.tracker.contacted_today()} of {people.daily_limit()}. Mark replies on '
            f'each job\'s page to stop the reminders.</p></div><script src="/people.js"></script>')


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
    unscored = len(matching.stale(app.tracker, profile, config.read_optional("resume.md") or "")) \
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
    checks += (f'<label class=row style="gap:6px"><input type=checkbox name=include_senior value=1'
               f'{" checked" if prefs.get("include_senior") else ""}> Include senior roles '
               f'<span class=muted>(senior, staff, principal, manager, director)</span></label>')
    chosen = sources.chosen_fields({"preferences": prefs})
    field_boxes = "".join(
        f'<label class=row style="gap:6px"><input type=checkbox name=fields value="{key}"'
        f'{" checked" if key in chosen else ""}> {label}</label>'
        for key, label in [*((k, v[0]) for k, v in sources.FIELDS.items()),
                           (sources.OTHER, "Other roles <span class=muted>(sales, operations, finance…)</span>")])
    body = f"""<h1>Search</h1>
    <form method=post action="/search"><input type=hidden name=token value="{app.token}">
    <div class=card><h2>What are you looking for?</h2>
    <p class=muted>In your own words: roles, fields, kinds of teams or companies, anything that makes a job
    right or wrong for you. Used to rank jobs and to judge fit when preparing.</p>
    <textarea name=description style="min-height:130px" placeholder="e.g. Backend or full-stack engineering roles at product-focused startups, mostly Python or Go. Data engineering is fine too. Not interested in consulting or sales roles.">{esc(prefs.get("description", ""))}</textarea>
    <p style="margin:12px 0 4px"><b>Role titles</b> <span class=muted>(comma-separated)</span></p>
    <input type=text name=roles value="{listed('roles')}"></div>

    <div class=card><h2>Filters</h2><p class=muted>Applied in code before any model call; filtered jobs show the reason.</p>
    <p style="margin:0 0 4px"><b>Fields</b> <span class=muted>(the job lists to pull; changing them refreshes the lists)</span></p>
    <div class=row style="gap:18px;margin-bottom:12px">{field_boxes}</div>
    <p style="margin:0 0 4px"><b>Job types</b></p>
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
    {people_settings(prefs)}
    <button class=primary>Save</button></form>"""
    return layout(app, "/search", body, flash)


PEOPLE_HINTS = {"background": "same school or past employer: the likeliest to reply",
                "peer": "someone in this role or on the team",
                "recruiter": "best contacted once you've applied",
                "hiring_manager": "advice is mixed: some welcome it, some pass it to HR"}


def people_settings(prefs: dict) -> str:
    kinds, count = people.wanted({"preferences": prefs})
    boxes = "".join(f'<label class=row style="gap:6px"><input type=checkbox name=people_kinds value="{k}"'
                    f'{" checked" if k in kinds else ""}> {label} <span class=muted>({PEOPLE_HINTS[k]})</span></label>'
                    for k, label in people.KINDS.items())
    options = "".join(f'<option{" selected" if n == count else ""}>{n}</option>'
                      for n in range(1, people.MAX_PER_JOB + 1))
    return f"""<div class=card><h2>People to contact</h2>
    <p class=muted>Each prepared application can include a few people at the company, with a short email and
    LinkedIn note for each. Career advisers' advice: ask for their advice rather than a job, under 100 words, one
    person per company at a time, and one follow-up after a week. Nothing is sent until you open the draft in your
    own email and send it. Untick all to turn this off.</p>
    <div style="display:grid;gap:6px">{boxes}</div>
    <p style="margin:12px 0 4px"><b>People per job</b></p>
    <select name=people_per_job style="width:auto">{options}</select></div>"""


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
    noticed = ('<div class="card"><h2>What the model noticed in the posting</h2><ul style="margin:0;padding-left:20px">'
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
    if trace.runs(job_id):
        docs = f'<a class="button primary" href="/job/{job_id}/trace">How this was made</a> ' + docs
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
    <div class=card><h2>Answers</h2><table>{rows}</table></div>
    {people_card(app, job, packet)}"""
    return layout(app, "/queue", body, flash)


EMAIL_STATUS = {"published": "shown on a page", "yours": "you entered it",
                "guessed": "guessed from the company's email format: it may bounce, so send the LinkedIn note too"}


def people_card(app: Dashboard, job: dict, packet: dict) -> str:
    """People at the company, each with drafts you edit and send from your own email."""
    found = packet.get("people") or {}
    listed = [p for p in found.get("people", []) if not p.get("removed")]
    finding = any(t["kind"] == "people" and t["job_id"] == job["id"] and t["status"] in ("queued", "running")
                  for t in app.tracker.tasks(30))
    again = ('<span class=muted>Finding people…</span>' if finding else
             form(app, "/people/find", "Find people again" if found else "Find people to contact", {"job_id": job["id"]}))
    if not listed:
        summary = esc(found.get("summary") or "Not looked up for this job.")
        return (f'<div class=card id=people><h2>People to contact</h2><p class=muted>{summary} '
                f'<a href="/search">Choose who to find</a>.</p>{again}</div>')
    applied = job["status"] == "submitted"
    blocks = [person_block(app, job, p, applied) for p in listed]
    trail = (f' <a href="/runs/{people.trace_name(job["id"])}">How it searched</a>'
             if (config.DATA_DIR / "runs" / f"{people.trace_name(job['id'])}.jsonl").exists() else "")
    return (f'<div class=card id=people><h2>People to contact</h2><p class=muted>{esc(found.get("summary") or "")}'
            f'{trail}</p><p class=muted>Advice: one person at a time; if there\'s no reply in '
            f'{people.SECOND_PERSON_DAYS} days, try the next; follow up once after a week. LinkedIn notes you send '
            f'yourself (the app never uses LinkedIn).</p>{"".join(blocks)}<div class=row>{again}</div></div>'
            f'<script src="/people.js"></script>')


def person_block(app: Dashboard, job: dict, person: dict, applied: bool) -> str:
    job_id, pid = job["id"], person["id"]
    links = [f'<a href="{esc(person["source"])}" target=_blank rel=noopener>source</a>']
    if person.get("linkedin"):
        links.append(f'<a href="{esc(person["linkedin"])}" target=_blank rel=noopener>LinkedIn</a>')
    why = esc(person.get("why") or "")
    email = person.get("email")
    address = (f'<b>{esc(email)}</b> <span class=muted>({EMAIL_STATUS.get(person.get("email_status"), "")})</span>'
               if email else '<span class=muted>No email found: add one below, or use the LinkedIn note.</span>')
    if person["kind"] in ("recruiter", "hiring_manager") and not applied:
        timing = "Advisers suggest sending this after you've applied."
    elif person["kind"] in ("background", "peer") and not applied:
        timing = "Good to send before you apply: a referral helps most then."
    else:
        timing = ""
    hint = f'<p class=muted style="margin:4px 0">{timing}</p>' if timing and not person.get("sent_at") else ""
    message = person.get("draft") or {}
    checks = "".join(f'<div class="flash warn">Check: {esc(c)}</div>' for c in person.get("checks") or [])
    state, buttons = "", []
    if person.get("replied_at"):
        state = f'<span class=sent>Replied {person["replied_at"][:10]}</span>'
    elif person.get("sent_at"):
        state = f'<span class=sent>Emailed {person["sent_at"][:10]}</span>'
        if person.get("followed_up_at"):
            state += f' <span class=sent>· followed up {person["followed_up_at"][:10]}</span>'
        buttons.append(form(app, "/people/state", "They replied", {"job_id": job_id, "person": pid, "state": "replied"}))
        if not person.get("followed_up_at") and message.get("follow_up"):
            buttons += send_buttons(app, job_id, pid, follow_up=True)
        buttons.append(form(app, "/people/unsend", "Undo: I didn't send it", {"job_id": job_id, "person": pid}))
    elif email and message:
        buttons += send_buttons(app, job_id, pid)
    if message.get("linkedin_note") and not person.get("replied_at"):
        buttons.append(f'<button type=button data-copy="note-{esc(pid)}">Copy LinkedIn note</button>')
        if person.get("linkedin"):
            buttons.append(f'<a class=button href="{esc(person["linkedin"])}" target=_blank rel=noopener>Open LinkedIn</a>')
    if not person.get("sent_at"):
        buttons.append(form(app, "/people/state", "Remove", {"job_id": job_id, "person": pid, "state": "removed"}))
    draft = ""
    if message:
        disabled = " disabled" if person.get("sent_at") and person.get("followed_up_at") else ""
        draft = (f'<details{"" if person.get("sent_at") else " open"}><summary class=muted>Drafts</summary>'
                 f'<form method=post action="/people/edit" class=draft><input type=hidden name=token value="{app.token}">'
                 f'<input type=hidden name=job_id value="{job_id}"><input type=hidden name=person value="{esc(pid)}">'
                 f'<label>To</label><input type=email name=email value="{esc(email or "")}" placeholder="name@company.com">'
                 f'<label>Subject</label><input type=text name=subject value="{esc(message["subject"])}">'
                 f'<label>Email ({len(message["body"].split())} words)</label><textarea name=body rows=7>{esc(message["body"])}</textarea>'
                 f'<label>LinkedIn connection note ({len(message["linkedin_note"])}/{people.NOTE_CHARS} characters): '
                 f'copy it, open their profile, Connect, Add a note</label>'
                 f'<textarea class=short name=linkedin_note id="note-{esc(pid)}">{esc(message["linkedin_note"])}</textarea>'
                 f'<label>Follow-up (one week later, if there\'s no reply)</label>'
                 f'<textarea class=short name=follow_up>{esc(message["follow_up"])}</textarea>'
                 f'<div class=row><button{disabled}>Save drafts</button></div></form></details>')
    return (f'<div class=person><div class=row><b>{esc(person["name"])}</b>'
            f'<span class=pill>{esc(people.KINDS[person["kind"]])}</span><span class=muted>{esc(person["title"])}</span>'
            f'{state}</div><p class=muted style="margin:4px 0">{why} · {" · ".join(links)}</p>'
            f'<p style="margin:4px 0">{address}</p>'
            f'{hint}'
            f'{checks}{draft}<div class=row>{" ".join(buttons)}</div></div>')


def send_buttons(app: Dashboard, job_id: int, pid: str, follow_up: bool = False) -> list[str]:
    """Open the draft in Gmail or your mail app; it's recorded as sent (undo if you don't send it)."""
    label = "follow-up" if follow_up else "email"
    out = []
    for via, name, target in (("gmail", "Gmail", " target=_blank"), ("mail", "your mail app", "")):
        out.append(f'<form method=post action="/people/send" class=send style="display:inline"{target}>'
                   f'<input type=hidden name=token value="{app.token}"><input type=hidden name=job_id value="{job_id}">'
                   f'<input type=hidden name=person value="{esc(pid)}"><input type=hidden name=via value="{via}">'
                   f'<input type=hidden name=follow_up value="{1 if follow_up else ""}">'
                   f'<button class="{"primary" if via == "gmail" else ""}">Send {label} in {name}</button></form>')
    return out


PEOPLE_JS = """document.querySelectorAll("form.send").forEach(f => f.addEventListener("submit", () =>
  setTimeout(() => location.reload(), 1500)));
document.querySelectorAll("button[data-copy]").forEach(b => b.addEventListener("click", () => {
  navigator.clipboard.writeText(document.getElementById(b.dataset.copy).value).then(() => {
    b.textContent = "Copied"; setTimeout(() => b.textContent = "Copy LinkedIn note", 1500); });
}));"""


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
    <div class=card><h2>Your master resume</h2>
    <p class=muted>{"Everything true you might ever include; each application gets a tailored cut of it. "
                    "Edit it any time: the previous version goes to profile/resume-history/, and your "
                    '<a href="/profile">profile</a> facts are updated from it.' if master else
                    "None yet. Upload your resume above and accept the proposal, or write it here in Markdown "
                    "(# Name, ## Sections, ### Roles, - bullets)."}</p>
    <form method=post action="/resumes/master"><input type=hidden name=token value="{app.token}">
    <textarea name=markdown style="min-height:520px;font:12.5px/1.55 ui-monospace,monospace"
    placeholder="# Your Name&#10;City · email · phone · links&#10;&#10;## Experience&#10;### Role — Company&#10;- What you did and its result">{esc(master or "")}</textarea>
    <div class=row style="margin-top:10px"><button class=primary>Save master resume</button></div></form></div>"""
    running = any(t["status"] in ("queued", "running") and t["kind"] in ("import_resume", "merge_resumes")
                  for t in app.tracker.tasks(10))
    return layout(app, "/resumes", body, flash, refresh=running)


def shown(path) -> str:
    """A path as you'd recognise it: relative to the app's folder when it's inside it."""
    return str(path.relative_to(config.HOME)) if path.is_relative_to(config.HOME) else path.name


PROFILE_SECTIONS = [
    ("profile.yaml", "Profile settings",
     "Preferences, standard answers (start date, how you heard about the job, salary), work authorization and "
     "voluntary demographic answers. Anything here also overrides a fact from your resume, e.g. "
     "<code>contact: {phone: \"+1 555 0100\"}</code>. The Search page's settings win over these preferences."),
    ("answers.yaml", "Saved answers",
     "Answers you ticked \"remember\" on, reused for the same question on later applications."),
    ("story-bank.md", "Story bank",
     "Real examples from your work, for essay questions (\"Tell us about a time…\"). Only these are used."),
    ("writing-sample.md", "Writing sample",
     "A few paragraphs you wrote, so cover letters sound like you."),
]


def page_profile(app: Dashboard, flash: str) -> bytes:
    """Everything about you, editable any time; facts from the resume shown as the base."""
    derived = config.PROFILE_DIR / "resume-profile.yaml"
    facts = (f'<div class=card><h2>From your resume</h2><p class=muted>Your name, contact details, links, education '
             f'and work authorization, read from your <a href="/resumes">master resume</a> each time it changes. '
             f'To change one, edit the resume, or override it in Profile settings below.</p>'
             f'<pre>{esc(derived.read_text(encoding="utf-8"))}</pre></div>') if derived.exists() else (
        '<div class=card><h2>From your resume</h2><p class=muted>Nothing yet: add your '
        '<a href="/resumes">master resume</a> and your details are read from it.</p></div>')
    cards = []
    for name, title, help_text in PROFILE_SECTIONS:
        text = config.read_optional(name) or ""
        mono = config.EDITABLE[name] == "yaml"
        cards.append(
            f'<div class=card><h2>{title} <span class=muted style="font-weight:500">profile/{name}</span></h2>'
            f'<p class=muted>{help_text}</p><form method=post action="/profile/save">'
            f'<input type=hidden name=token value="{app.token}"><input type=hidden name=file value="{name}">'
            f'<textarea name=content style="min-height:{360 if mono else 200}px;'
            f'{"font:12.5px/1.55 ui-monospace,monospace" if mono else ""}">{esc(text)}</textarea>'
            f'<div class=row style="margin-top:10px"><button class=primary>Save</button></div></form></div>')
    body = f"""<h1>Profile</h1>
    <p class=muted style="margin-top:-8px">Stays on your computer; sent to the model only to tailor resumes and answer
    application questions.</p>
    {facts}{"".join(cards)}"""
    return layout(app, "/profile", body, flash)


OUTCOME_NAMES = {"applied": "applied, no answer yet", "oa": "online assessment", "interview": "interview",
                 "offer": "offer", "rejected": "rejected", "withdrawn": "withdrawn",
                 "no_response": "no response", "contacted": "emailed someone",
                 "followed_up": "followed up"}


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


# Labels for a run's model requests (one model does every step here).
TASK_LABELS = {
    "match_scores": "Ranking job titles", "resume_markdown": "Reading resume PDFs",
    "resume_facts": "Profile facts from your resume", "research": "Company research (agent)",
    "assistant": "Assistant chat", "fit": "Fit check on the full posting", "tailored_resume": "Tailored resume",
    "cover_letter": "Cover letter", "form_answers": "Application answers and essays",
    "explore": "Filling unfamiliar forms (agent)", "people": "Finding people to contact (agent)",
    "outreach": "Emails and LinkedIn notes to them", "discover": "Discovering companies on job sites",
}
MODEL_NAMES: dict[str, str] = {}


def seconds_text(value) -> str:
    value = float(value or 0)
    return f"{value:.1f} s" if value < 60 else f"{int(value // 60)} min {int(value % 60)} s"


def tokens_text(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def pretty_json(text: str) -> str:
    """JSON indented, with line breaks inside strings shown as line breaks (for reading, not parsing)."""
    try:
        return json.dumps(json.loads(text), indent=1, ensure_ascii=False).replace("\\n", "\n")
    except (ValueError, TypeError):
        return text


def node_state(node: dict) -> str:
    """ok, caught (a check stopped something) or stopped (an error ended it)."""
    if node.get("error"):
        return "stopped"
    flat = []

    def walk(n):
        for child in n["children"]:
            flat.append(child)
            walk(child)

    walk(node)
    return "caught" if any(c["type"] == "check" and not c["passed"] for c in flat) else "ok"


def timeline(nodes: list[dict]) -> str:
    """Steps with what happened inside each: model calls, agent runs, checks, notes."""
    out = []
    turns = [n for n in nodes if n["type"] == "model" and n.get("turn")]
    for node in nodes:
        kind = node["type"]
        if kind == "step":
            summary = node.get("error") or node.get("summary") or ""
            inside = f'<ul class=tl>{timeline(node["children"])}</ul>' if node["children"] else ""
            out.append(f'<li class="step" id="s{node["n"]}"><div class=head><b>{esc(node["name"])}</b>'
                       f'<span class="{"bad" if node.get("error") else "muted"}">{esc(summary)}</span>'
                       f'<span class=when>{seconds_text(node.get("seconds"))}</span></div>{inside}</li>')
        elif kind == "check":
            css, tag = ("pass", "✓ Check") if node["passed"] else ("caught", "⚑ Caught")
            out.append(f'<li><div class="ev {css}"><span class=tag>{tag}</span>{esc(node["name"])}'
                       f'{" — " + esc(node["detail"]) if node.get("detail") else ""}</div></li>')
        elif kind == "model" and not node.get("turn"):
            usage = node.get("usage") or {}
            name = MODEL_NAMES.get(node["model"], node["model"])
            line = (f'{esc(TASK_LABELS.get(node["task"], node["task"]))} · <b>{esc(name)}</b> · {esc(node["effort"])} '
                    f'effort · {seconds_text(node["seconds"])}'
                    + (f' · {tokens_text(usage.get("input", 0))} in / {tokens_text(usage.get("output", 0))} out'
                       if usage else ""))
            raw = (f'<div class=raw><details open><summary class=muted>Instructions (system prompt)</summary>'
                   f'<pre>{esc(node.get("system"))}</pre></details><details open><summary class=muted>What was sent'
                   f'</summary><pre>{esc(pretty_json(node.get("prompt", "")))}</pre></details><details open>'
                   f'<summary class=muted>What came back</summary><pre>{esc(pretty_json(node.get("reply") or node.get("error", "")))}'
                   f'</pre></details></div>')
            error = f' <span class=bad>{esc(node["error"][:200])}</span>' if node.get("error") else ""
            out.append(f'<li class=plain><div class="ev model{" err" if node.get("error") else ""}">'
                       f'<span class=tag>◆ Model</span>{line}{error}{raw}</div></li>')
        elif kind == "agent":
            used = turns
            spent = sum(t["seconds"] for t in used)
            model = MODEL_NAMES.get(used[0]["model"], used[0]["model"]) if used else ""
            out.append(f'<li class=plain><div class="ev model"><span class=tag>◆ Agent</span>{node["steps"]} turns'
                       f'{f" with {esc(model)}" if model else ""}{f" · {seconds_text(spent)}" if used else ""} · '
                       f'<a href="/runs/{esc(node["name"])}?from={node["start"]}">every turn, tool call and result</a>'
                       f'<div class=muted style="margin-top:4px">Report: {esc(node["report"][:600])}</div></div></li>')
        elif kind == "note":
            out.append(f'<li class=plain><div class=ev><span class=tag>•</span>{esc(node["text"])}</div></li>')
    return "".join(out)


def page_trace(app: Dashboard, job_id: int, run_id: str) -> bytes:
    """How a job's packet (or a later run on it) was made, step by step."""
    job = app.tracker.get(job_id)
    paths = trace.runs(job_id)
    if job is None or not paths:
        return layout(app, "/queue", "<h1>No runs recorded for this job yet</h1>")
    path = next((p for p in paths if p.stem == run_id), paths[0])
    shown = trace.tree(trace.read(path))
    run, end, totals = shown["run"], shown["end"], shown["totals"]
    tabs = "".join(f'<a class="tab{" on" if p == path else ""}" href="/job/{job_id}/trace?run={p.stem}">'
                   f'{esc(trace.KINDS.get(p.stem.split("-", 2)[2], p.stem))} · {p.stem[4:6]}/{p.stem[6:8]} '
                   f'{p.stem[9:11]}:{p.stem[11:13]}</a>' for p in paths)
    steps = [n for n in shown["root"]["children"] if n["type"] == "step"]
    strip = "".join(f'<a class="{node_state(n)}" href="#s{n["n"]}">{esc(n["name"])}'
                    f'<span>{seconds_text(n.get("seconds"))}</span></a>' for n in steps)
    status = (end.get("status") or "running…").replace("_", " ")
    cost = (f'<div><b>${totals["cost"]:.2f}</b><span>'
            f'{"model cost"}'
            f'</span></div>') if totals["cost"] else ""
    live = not end
    body = f"""<h1>How this was made</h1>
    <p class=muted><a href="/job/{job_id}">{esc(job["title"])} at {esc(job["company"])}</a> ·
    {esc(trace.KINDS.get(run.get("kind"), run.get("kind", "")))} · {esc(status)}
    {("· " + esc(end.get("detail"))) if end.get("detail") else ""} · {seconds_text(end.get("seconds"))} in all</p>
    <div class=tabs>{tabs}</div>
    <div class=totals><div><b>{len(steps)}</b><span>steps</span></div>
    <div><b>{totals["calls"]}</b><span>model requests{f" + {totals['turns']} agent turns" if totals["turns"] else ""}</span></div>
    <div><b>{tokens_text(totals["input"])} / {tokens_text(totals["output"])}</b><span>tokens in / out</span></div>{cost}
    <div><b>{totals["passed"]} · {totals["caught"]}</b><span>checks passed · caught something</span></div></div>
    <div class=strip>{strip}</div>
    <div class=card><div class=row style="margin-bottom:12px">
    <label class=row style="gap:6px"><input type=checkbox id=show-raw> Show prompts and replies</label>
    <label class=row style="gap:6px"><input type=checkbox id=only-checks> Only the checks</label>
    <span class=muted>Checks are rules in code, not the model: ✓ passed, ⚑ caught something and acted on it.</span></div>
    <ul class=tl id=timeline>{timeline(shown["root"]["children"])}</ul></div>
    <script src="/trace.js"></script>"""
    return layout(app, "/queue", body, refresh=live)


HOW_MD = Path(__file__).parent / "static" / "how-it-works.md"
# The journey of one application: (stage, who does it, what happens, where in the code).
JOURNEY = [
    ("Find", "code", "Job lists and company boards, read in a few minutes; Discover searches job sites for companies "
     "you haven't heard of", "sources.py, discover.py"),
    ("Rank", "code + model", "Free keyword scores for every job, then the model scores the best ones against your words",
     "matching.py"),
    ("Filter", "code", "Location, season, degree, seniority, sponsorship, company and daily limits", "filters.py, companies.py"),
    ("Read", "code", "The posting rendered in a browser; only text a person can see is kept", "prepare.py, browser.py"),
    ("Fit", "model", "The model checks every requirement against your resume and scores the fit", "prepare.py"),
    ("Form", "code", "The application's questions, read from Greenhouse, Lever, Ashby or Workday", "greenhouse.py, …"),
    ("Answers", "code + model", "Profile and saved answers in code; the model drafts the rest from your material",
     "answers.py"),
    ("Resume", "model + code", "The model tailors it; code rejects invented numbers and fits it to the page", "prepare.py, latex.py"),
    ("Research", "agent", "Searches and reads company pages; keeps facts quoted from them", "research.py"),
    ("People", "agent + model", "Finds people to contact; the model drafts a short email and LinkedIn note", "people.py"),
    ("Approve", "you", "You review everything; approval records a fingerprint of it", "actions.py"),
    ("Submit", "code", "Checks nothing changed, types your approved answers, saves a screenshot", "submit.py"),
]


def page_how(app: Dashboard) -> bytes:
    import markdown

    stages = "".join(f'<a class="who-{who.split()[0]}" title="{esc(what)}">'
                     f'{esc(stage)}<span>{esc(who)}</span></a>' for stage, who, what, _ in JOURNEY)
    rows = "".join(f'<tr><td><b>{esc(stage)}</b></td><td><span class=pill>{esc(who)}</span></td><td>{esc(what)}</td>'
                   f'<td class=muted><code>{esc(where)}</code></td></tr>' for stage, who, what, where in JOURNEY)
    example = next((int(p.parent.name) for p in trace.recent(1)), None)
    link = (f' <a href="/job/{example}/trace">See a real one, step by step.</a>' if example else "")
    text = markdown.markdown(HOW_MD.read_text(encoding="utf-8"), extensions=["tables"])
    body = f"""<h1>How it works</h1>
    <div class=card><h2>The journey of one application</h2>
    <p class=muted>Who does each step: <b>code</b> (fixed rules), a <b>model</b> (one request), an <b>agent</b> (a model
    choosing its own steps with tools), or <b>you</b>.{link}</p>
    <div class=strip>{stages}</div>
    <table><tr><th>Step</th><th>Who</th><th>What happens</th><th>Code</th></tr>{rows}</table>
    <p class=muted>The <a href="/assistant">assistant</a> uses the same steps as tools, so a chat request follows the
    same path and the same checks.</p></div>
    <div class="card md">{text}</div>"""
    return layout(app, "/how", body)


TRACE_JS = """const box = document.getElementById("timeline").parentElement;
for (const [id, cls] of [["show-raw", "show-raw"], ["only-checks", "only-checks"]]) {
  const input = document.getElementById(id);
  try { input.checked = localStorage.getItem(id) === "1"; } catch (_) {}
  const apply = () => { box.classList.toggle(cls, input.checked);
    try { localStorage.setItem(id, input.checked ? "1" : "0"); } catch (_) {} };
  input.addEventListener("change", apply); apply();
}"""


def page_run(app: Dashboard, name: str, start: int = 0) -> bytes:
    """How an exploration, research or people-finding agent worked on a job, step by step."""
    from .harness import read_trace, trace_path

    kind, job_id = name.split("-")
    job = app.tracker.get(int(job_id))
    entries = read_trace(trace_path(name))[start:]
    doing = {"explore": "Fill in the application", "research": "Research the company",
             "people": "Find people to contact"}[kind]
    goal = f"{doing} for {job['title']} at {job['company']}" if job else ""
    rows = []
    for entry in entries:
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
    back = f' · <a href="/job/{job_id}/trace">back to how this was made</a>' if job else ""
    body = (f'<h1>{esc(doing)}</h1><p class=muted>{esc(goal)}{back}</p>'
            + ("".join(rows) or "<p class=muted>No steps recorded.</p>"))
    return layout(app, "/", body)


CHAT_JS = Path(__file__).parent / "static" / "chat.js"
SUGGESTIONS = [
    "Find my 5 best matches, one per company, and prepare them.",
    "What's waiting for me, and what should I look at first?",
    "Add my new project to my resume.",
    "Look up what this company works on before I apply.",
]


def page_assistant(app: Dashboard, chat_id: int | None, draft: str = "") -> bytes:
    chat = app.chats.chat(chat_id) if chat_id else None
    model = chat["model"] if chat else app.turns.default_model()
    # One model per visit on the class app store; a laptop's OPENROUTER_MODEL list offers each.
    names = {m: m.split("/")[-1] for m in [model, *llm_models.choices()] if m}
    options = "".join(f'<option value="{esc(m)}"{" selected" if m == model else ""}>{esc(n)}</option>'
                      for m, n in names.items())
    chats = "".join(f'<a class="c{" on" if chat and c["id"] == chat["id"] else ""}" href="/assistant?chat={c["id"]}">'
                    f'<span>{esc(c["title"])}</span><button data-delete="{c["id"]}" title="Delete chat">×</button></a>'
                    for c in app.chats.chats())
    hello = "" if chat else (
        '<div id=empty><h1>What can I do for you?</h1><p class=muted>I can do anything this app does: find and '
        'prepare applications, fix a packet, edit your resume, profile or search, and look things up on the web. '
        'You confirm every submission.</p><div class=prompts>'
        + "".join(f'<button data-prompt="{esc(t)}">{esc(t)}</button>' for t in SUGGESTIONS) + "</div></div>")
    body = f"""<div class=chat id=chat data-token="{app.token}" data-chat="{chat["id"] if chat else ""}">
    <nav class=chats><a class="button new" href="/assistant">+ New chat</a>{chats}</nav>
    <section class=convo><div id=tasks></div>{hello}<div id=log><div id=live></div></div>
    <div class=composer><div class=box><textarea id=message rows=1 placeholder="Message the assistant">{esc(draft)}</textarea>
    <div class=row><select id=model title="Model for this chat">{options}</select>
    <span><button id=stop hidden>Stop</button> <button id=send class=primary>Send</button></span></div></div></div>
    </section></div><script src="/assistant/chat.js"></script>"""
    return layout(app, "/assistant", body)


def chat_poll(app: Dashboard, chat_id: int | None, since: int) -> dict:
    items = app.chats.items(chat_id, since) if chat_id else []
    rev = max([since] + [i["rev"] for i in items])
    active = [t for t in app.tracker.tasks(30) if t["status"] in ("queued", "running", "waiting")]
    from .assistant import task_brief

    partial = app.turns.partial(chat_id) if chat_id else None
    return {"rev": rev, "items": [{"id": i["id"], "kind": i["kind"], "data": public(i)} for i in items],
            "running": partial is not None, "partial": partial or "", "tasks": [task_brief(t) for t in active]}


def public(item: dict) -> dict:
    """An item as the page sees it (an undo card's saved text stays on the server)."""
    return {k: v for k, v in item["data"].items() if k != "before"}


def chat_card(app: Dashboard, item_id: int, choice: str) -> None:
    """Your click on a card: confirm or cancel a submission, or undo an edit."""
    item = app.chats.item(item_id)
    if item is None or item["data"].get("state") != "pending":
        raise ValueError("That's already been handled.")
    data, chat_id = item["data"], item["chat_id"]
    if item["kind"] == "confirm" and choice == "yes":
        ids = [j["id"] for j in data["jobs"]]
        ready, problems = actions.ready_to_submit(app.tracker, app.settings, ids)
        if ready:
            app.ensure_worker()
        tasks = [app.tracker.add_task("submit", job_id) for job_id in ready]
        result = (f"Submitting {len(ready)}: watch the Chrome window." if ready else "Nothing was submitted.") + (
            " " + " ".join(problems) if problems else "")
        app.chats.change(item_id, state="done", result=result)
        app.chats.add(chat_id, "note", {"text": f"The user confirmed. Submit tasks {tasks} for jobs {ready}."
                                        + (f" Not submitted: {' '.join(problems)}" if problems else ""),
                                        "shown": f"You confirmed. {result}", "delivered": False})
    elif item["kind"] == "confirm" and choice == "no":
        app.chats.change(item_id, state="cancelled", result="Not submitted.")
        app.chats.add(chat_id, "note", {"text": f"The user chose not to submit jobs {[j['id'] for j in data['jobs']]} "
                                        "for now.", "shown": "You chose not to submit for now.", "delivered": False})
    elif item["kind"] == "outreach" and choice in ("sent", "no"):
        follow_up = data.get("follow_up")
        if choice == "sent":
            try:
                people.mark_sent(app.tracker, data["job_id"], data["person"], follow_up=bool(follow_up))
                result, note = "Opened in your email.", (f"The user opened the {'follow-up' if follow_up else 'email'} "
                                                         f"to {data['name']} in their email to send.")
            except ValueError as error:
                result, note = str(error), f"Not recorded as sent: {error}"
        else:
            result, note = "Not sent.", f"The user chose not to email {data['name']} for now."
        app.chats.change(item_id, state="done" if choice == "sent" else "cancelled", result=result)
        app.chats.add(chat_id, "note", {"text": note, "shown": result if choice == "no" else
                                        f"You opened the email to {data['name']}.", "delivered": False})
    elif item["kind"] == "undo" and choice == "undo":
        if data["file"] == "resume.md":
            resumes.save_master(data["before"])
            app.ensure_worker()
            app.tracker.add_task("sync_profile")
        else:
            actions.save_profile(app.tracker, data["file"], data["before"])
        app.chats.change(item_id, state="undone")
        app.chats.add(chat_id, "note", {"text": f"The user undid your edit to {data['file']}.",
                                        "shown": f"You undid the edit to {data['file']}.", "delivered": False})
    else:
        raise ValueError("Unknown choice.")


def page_activity(app: Dashboard) -> bytes:
    """Recent runs on jobs (each opens its timeline), then the worker's raw log."""
    text = LOG_PATH.read_text(encoding="utf-8")[-20000:] if LOG_PATH.exists() else "No activity yet."
    running = app.worker is not None and app.worker.poll() is None
    rows = []
    for path in trace.recent(40):
        job_id = int(path.parent.name)
        job = app.tracker.get(job_id)
        shown = trace.tree(trace.read(path))
        end, totals = shown["end"], shown["totals"]
        status = (end.get("status") or "running").replace("_", " ")
        rows.append(f'<tr><td class=muted style="white-space:nowrap">{path.stem[4:6]}/{path.stem[6:8]} '
                    f'{path.stem[9:11]}:{path.stem[11:13]}</td>'
                    f'<td><a href="/job/{job_id}/trace?run={path.stem}">{esc(trace.KINDS.get(shown["run"].get("kind"), ""))}'
                    f'</a></td><td>{esc(job["title"] if job else job_id)}<div class=src>{esc(job["company"] if job else "")}'
                    f'</div></td><td><span class="pill {esc(end.get("status") or "running")}">{esc(status)}</span></td>'
                    f'<td class=muted>{seconds_text(end.get("seconds"))}</td>'
                    f'<td class=muted>{totals["calls"]} model · {totals["passed"]} ✓ · {totals["caught"]} ⚑</td></tr>')
    runs = (f'<div class=card><h2>Recent runs</h2><p class=muted>Every run on a job, step by step: open one to see '
            f'what the model was asked, what it answered and what the checks in code decided.</p>'
            f'<table><tr><th>When</th><th>Run</th><th>Job</th><th>Result</th><th>Took</th><th>Inside</th></tr>'
            f'{"".join(rows)}</table></div>') if rows else ""
    body = (f'<h1>Activity</h1>{runs}<div class=card><details{" open" if not rows else ""}><summary><b>Worker log</b> '
            f'<span class=muted>(worker {"running" if running else "not running"}; full logs in data/logs/)</span>'
            f'</summary><pre>{esc(text)}</pre></details></div>')
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
            self.safely(self.get)

        def do_POST(self):
            self.safely(self.post)

        def safely(self, handle):
            """A failing page shows its error instead of never answering (SystemExit included)."""
            try:
                handle()
            except (Exception, SystemExit) as error:  # noqa: BLE001 - shown to you, and logged.
                import traceback

                traceback.print_exc()
                try:
                    self.send(layout(app, "/", f'<h1>Something went wrong</h1><div class="flash bad">'
                                     f'{esc(type(error).__name__)}: {esc(error)}</div><p><a href="/">Back to '
                                     f'Today</a></p>'), 500)
                except Exception:  # noqa: BLE001 - the response may have started already.
                    pass

        def get(self):
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
            if url.path == "/assistant":
                chat = query.get("chat", "")
                return self.send(page_assistant(app, int(chat) if chat.isdigit() else None, query.get("q", "")))
            if url.path == "/people.js":
                return self.send(PEOPLE_JS.encode(), ctype="text/javascript; charset=utf-8")
            if url.path == "/assistant/chat.js":
                return self.send(CHAT_JS.read_bytes(), ctype="text/javascript; charset=utf-8")
            if url.path == "/assistant/api/poll":
                chat, since = query.get("chat", ""), query.get("since", "0")
                state = chat_poll(app, int(chat) if chat.isdigit() else None, int(since) if since.isdigit() else 0)
                return self.send(json.dumps(state, default=str).encode(), ctype="application/json")
            if url.path == "/queue":
                return self.send(page_queue(app, flash))
            if url.path == "/jobs":
                return self.send(page_jobs(app, query.get("status", "active"), flash))
            if url.path == "/activity":
                return self.send(page_activity(app))
            if url.path == "/how":
                return self.send(page_how(app))
            if url.path == "/search":
                return self.send(page_search(app, flash))
            if url.path == "/profile":
                return self.send(page_profile(app, flash))
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
            if parts[0] == "runs" and len(parts) == 2 and re.fullmatch(r"(explore|research|people)-\d+", parts[1]):
                start = query.get("from", "0")
                return self.send(page_run(app, parts[1], int(start) if start.isdigit() else 0))
            if parts[0] == "job" and len(parts) == 3 and parts[1].isdigit() and parts[2] == "trace":
                return self.send(page_trace(app, int(parts[1]), query.get("run", "")))
            if url.path == "/trace.js":
                return self.send(TRACE_JS.encode(), ctype="text/javascript; charset=utf-8")
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

        def post(self):
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
            if urlsplit(self.path).path.startswith("/assistant/api/"):
                try:
                    reply = self.chat_api(urlsplit(self.path).path, one)
                except ValueError as error:
                    reply = {"error": str(error)}
                return self.send(json.dumps(reply).encode(), ctype="application/json")
            if urlsplit(self.path).path == "/people/send":
                try:
                    person = people.mark_sent(app.tracker, int(one["job_id"]), one["person"],
                                              follow_up=bool(one.get("follow_up")))
                    target = people.compose_url(person, one.get("via", "gmail"), bool(one.get("follow_up")))
                except ValueError as error:
                    target = f"/job/{one.get('job_id', '')}?msg={quote(str(error)[:500])}#people"
                return self.send(b"", 303, headers={"Location": target})
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

        def chat_api(self, path: str, one: dict) -> dict:
            chat = int(one["chat"]) if one.get("chat", "").isdigit() else None
            if one.get("model") and one["model"] not in {*llm_models.choices(), app.turns.default_model()}:
                raise ValueError("Pick a model from the list.")
            if path == "/assistant/api/send":
                if app.llm_missing:
                    raise ValueError("No model is connected (see the Model page).")
                return {"chat": app.turns.send(chat, one.get("text", ""), one.get("model", ""))}
            if path == "/assistant/api/stop" and chat:
                app.turns.stop(chat)
                return {"ok": True}
            if path == "/assistant/api/model" and chat:
                app.chats.update(chat, model=one.get("model", "") or app.turns.default_model())
                return {"ok": True}
            if path == "/assistant/api/delete" and chat:
                app.turns.stop(chat)
                app.chats.delete(chat)
                return {"ok": True}
            if path == "/assistant/api/card":
                chat_card(app, int(one.get("item", 0)), one.get("choice", ""))
                return {"ok": True}
            if path == "/assistant/api/reply":
                task = app.tracker.task(int(one.get("task_id", 0)))
                if task is None or task["status"] != "waiting":
                    raise ValueError("That task isn't waiting any more.")
                app.tracker.set_task(task["id"], answer=one.get("answer", ""))
                return {"ok": True}
            raise ValueError("Unknown action.")

        def act(self, path: str, one: dict, fields: dict, back: str) -> tuple[str, str]:
            tracker = app.tracker
            ids = [int(i) for raw in fields.get("ids", []) for i in raw.split(",") if i.strip().isdigit()]
            if path == "/discover":
                app.ensure_worker()
                tracker.add_task("discover")
                return ("Searching job sites (Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Workable, "
                        "BambooHR, Jobvite, JazzHR, iCIMS) for your roles. Companies found are read on every "
                        "refresh from now on."), "/"
            if path == "/refresh":
                app.ensure_worker()
                tracker.add_task("refresh")
                return "Refreshing the job lists.", "/"
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
                message, refresh = actions.save_search(tracker, app.settings, {
                    "description": one.get("description", "").strip(),
                    "roles": words("roles"),
                    # Every field ticked is the same as none: no field filter.
                    "fields": [] if set(fields.get("fields", [])) >= set(sources.ALL_FIELDS) else fields.get("fields", []),
                    "job_types": fields.get("job_types", []),
                    "include_senior": "include_senior" in fields,
                    "locations": [l.strip() for l in one.get("locations", "").splitlines() if l.strip()],
                    "terms": words("terms"),
                    "max_posting_age_days": int(age) if age.isdigit() else None,
                    "exclude_companies": words("exclude_companies"),
                    "exclude_title_words": words("exclude_title_words"),
                    "fit_threshold": max(0, min(100, threshold)),
                    "resume_pages": 2 if one.get("resume_pages") == "2" else 1,
                    "people_kinds": [k for k in fields.get("people_kinds", []) if k in people.KINDS],
                    "people_per_job": int(one.get("people_per_job") or people.DEFAULT_PER_JOB),
                })
                if refresh:
                    app.ensure_worker()
                    tracker.add_task("refresh")
                    return message + " Follow it on Today.", "/"
                return message + " Rank them on the Ranked jobs page.", "/search"
            if path == "/people/find":
                app.ensure_worker()
                tracker.add_task("people", int(one["job_id"]))
                return "Looking for people to contact. They'll appear on this page.", back
            if path == "/people/edit":
                people.edit(tracker, int(one["job_id"]), one["person"],
                            {k: one.get(k, "") for k in ("email", "subject", "body", "linkedin_note", "follow_up")})
                return "Saved.", back
            if path == "/people/unsend":
                people.unmark(tracker, int(one["job_id"]), one["person"])
                return "Marked as not sent.", back
            if path == "/people/state":
                people.set_state(tracker, int(one["job_id"]), one["person"], one.get("state", ""))
                return "Saved.", back
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
                        + (f" The previous one is in {shown(backup)}." if backup else "")), "/resumes"
            if path == "/resumes/edit":
                resumes.edit_proposal(one.get("markdown", ""))
                return "Saved your edits to the proposal.", "/resumes"
            if path == "/resumes/master":
                backup = resumes.save_master(one.get("markdown", ""))
                message = "Saved your master resume."
                if backup:
                    message += f" The previous one is in {shown(backup)}."
                if not app.llm_missing:
                    app.ensure_worker()
                    tracker.add_task("sync_profile")
                    message += " Updating your profile facts from it."
                return message, "/resumes"
            if path == "/profile/save":
                name = one.get("file", "")
                actions.save_profile(tracker, name, one.get("content", ""))
                return f"Saved {name}.", "/profile"
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
    try:  # Filters and the already-applied check may have changed since the last run.
        sources.apply_filters(app.tracker, config.load_profile())
    except SystemExit:
        pass  # No profile yet.
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
