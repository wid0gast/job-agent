# Job Agent: the full guide

Everything in detail. The [README](../README.md) is the short version.

A local agent that finds jobs matching what you ask for, tailors your resume
to each one, writes a cover letter when needed, fills in the application and
submits it, with you approving each submit. It tracks every job so it never
applies twice.

It runs on your laptop in a visible Chrome window. You can watch it work and
step in at any time. It uses Claude, either through your own Claude Code
sign-in (your Pro or Max plan, no API key) or an Anthropic API key.

### Setup (once)

Requires [uv](https://docs.astral.sh/uv/), Google Chrome, a way to reach
Claude (below), and for resumes in your LaTeX style, [Tectonic](https://tectonic-typesetting.github.io/)
(`brew install tectonic`; without it resumes fall back to a plain HTML style).

**Resume style.** Tailored resumes and cover letters are typeset with LaTeX.
Put your own template's .tex files in `profile/resume-templates/one-page.tex`
and `two-page.tex` (only the preamble is used, so its macros must match
`job_agent/templates/`). The model picks and orders sections for each job;
code fits the result to the page by cutting the least relevant lines, and
Education is always kept.

```sh
cd job-agent
uv sync
uv run job-agent setup
```

Then:

1. Edit the job preferences in `profile/profile.yaml`. Your name, contact
   details, links, education and work authorization are read from your master
   resume (step 2) into `profile/resume-profile.yaml`; you don't type them.
2. Build your **master resume** (everything true you might ever include) in
   one of two ways. Either upload your resume PDFs on the dashboard's
   Resumes page (or `uv run job-agent resumes import a.pdf b.pdf`): each is
   read, then all are merged, newest winning where they disagree, and you
   review the result before it replaces `profile/resume.md`. Or write
   `profile/resume.md` yourself. The agent cuts and reorders it per job but
   never adds facts. Optional: `profile/writing-sample.md` (a few paragraphs you
   wrote) so cover letters sound like you.
3. Connect Claude (see **Connecting Claude** below). If Claude Code is
   installed and signed in, there's nothing to do; otherwise use the
   dashboard's **Connect Claude** page (the status chip, top right).

To try it without your own resume, upload `examples/jane-doe-resume.pdf`
(a made-up candidate) on the Resumes page.
4. Optional: `profile/story-bank.md` (your real examples) and
   `profile/writing-sample.md` help with essay questions.

`profile/`, `data/` and `.env` are gitignored and stay on your machine.
Profile and resume content is sent to Claude while applying.

### Connecting Claude

`MODEL_BACKEND=auto` (the default) uses the first of these that's available:

1. **Claude Code, with your own sign-in.** Install
   [Claude Code](https://code.claude.com/docs/en/setup) and sign in with your
   Claude account: on the dashboard's **Connect Claude** page ("Sign in with
   Claude" opens Claude's sign-in page; paste the code it shows), or by
   running `claude` once in a terminal. Job Agent then runs it in headless mode
   (`claude -p`) for each model request: a fresh, unsaved session with Claude
   Code's own tools turned off. Usage counts against your Claude plan, like
   using Claude Code yourself, and nothing is billed per token. An
   `ANTHROPIC_API_KEY` in the environment is hidden from it so your sign-in is
   what's used.
2. **An Anthropic API key.** Put `ANTHROPIC_API_KEY=` in `.env`, or sign in
   with Anthropic's `ant auth login`. Billed per token by Anthropic, with
   `SPEND_CAP_USD` enforced per run.

Either way, each task uses a model that fits it: Haiku 5.5 ranks job titles,
Sonnet 5.5 reads resumes, extracts profile facts, researches companies and runs
the planner, and Opus 5.5 checks fit and writes the tailored resume, cover
letter and answers (it also drives the form-filling agent). Models are named by
full ID, because Claude Code's short names (`haiku`, `sonnet`) still point at
older models. The Connect page lists every task's model and effort.

Set `MODEL_BACKEND=claude-code` or `anthropic` to force one. This is a
personal tool: each person runs it on their own computer with their own
Claude account or key. Anthropic doesn't allow third-party products to offer
claude.ai sign-in or plan rate limits to their users without approval, so
don't host Job Agent for other people on your plan.

### Everyday use: the dashboard

```sh
uv run job-agent dashboard        # http://127.0.0.1:8770
```

0. **Search**: describe in your own words what you're looking for, set the
   filters (internship/full-time, locations, seasons, posting age,
   exclusions) and the fit threshold. Saved to `profile/search.yaml`.
1. **Refresh** pulls Simplify's internship and new-grad lists for your
   fields (`fields` in `profile/profile.yaml`: `software`, `ai_ml_data`,
   `hardware`, `quant`, `product`; the example starts with `ai_ml_data`)
   and the Greenhouse, Lever and Ashby boards of companies seen there. Hard filters from
   `preferences` in your profile run in code; the Jobs page shows why each
   filtered job is out.
   **Ranked jobs** lists them by score. Every job first gets a free, instant
   *keyword* score (its title against your role titles and description;
   words you rule out, like "not marketing", count against it). "Rank the
   top 120 with the model" then gives the best keyword matches a *match*
   score (title, company and location against your description, ~60 jobs per
   request, two requests at a time); click again for the next 120. Once a
   job is prepared it has a *fit* score from the full posting. Model scores
   rank above keyword-only ones, and jobs below your threshold are greyed.
   Ranking runs alongside other tasks, so you can prepare while it works.
2. **Prepare** (per job, or "Prepare top matches", which takes the best-ranked) reads the posting and the
   application form without signing in or clicking anything, scores fit,
   tailors your resume, and drafts every answer, recording where each came
   from: your profile, saved answers, answers you approved before, or the
   model. Nothing is submitted.
3. **Approval queue.** Review a packet, edit any answer, then approve.
   Approval is refused while a required answer is missing, an answer
   contradicts your profile, or the company or daily cap is full. Approving
   reserves the slot. Editing after approval un-approves.
4. **Submit** opens the form in the Chrome window and types exactly the
   approved answers. No model is involved. If the live form differs (a new
   required question, changed options) it doesn't submit and the job comes
   back marked "needs you". Codes Greenhouse emails you, and any CAPTCHA, are
   asked for on the Today page. A screenshot is saved as proof.

Greenhouse, Lever, Ashby and Workday jobs can be prepared and submitted.

**Workday** works page by page. Each company's Workday site gets its own
account, created with your profile email and a generated password kept in the
macOS Keychain (`job-agent:<site>`); when Workday sends a verification email,
the Today page asks you to click its link. Questions on later pages aren't
visible in advance, so submit fills page after page and stops before anything
is sent when it meets a required question you haven't approved. The job comes
back as "needs you": answer the new questions (or click "Draft missing
answers with the model"), approve again, and it continues. It clicks the
final button only when that button says Submit. When it stops, the Chrome
window stays on that page, so you can also finish by hand and mark it done. From the terminal, `uv run job-agent refresh`
and `uv run job-agent prepare --top 3` do the same unattended steps.

### The agents

Most of the app is a fixed pipeline (find → rank → prepare → approve →
submit). Three agents handle the parts with no fixed path. None can approve
or submit for you, change caps, or invent an answer; those checks are code.

- **Planner** (Today → "Give the agent a goal"): you say what you want, e.g.
  "fill the queue with my 8 best ML internships, NYC or remote". It refreshes,
  ranks, checks caps, prepares, reviews each packet, redoes weak ones with a
  specific note, and asks you one combined question when several applications
  share a gap. Each run has a live trace (reasoning and tool calls).
- **Explorer** (systems without a coded filler: iCIMS, custom career pages):
  during prepare it fills the form in the browser, creating an account and
  asking you for emailed codes if needed, and stops at the final submit.
  Every value comes from the answer pipeline, your replies or your documents.
  After you approve, code replays its recorded steps; the job page shows them.
- **Researcher** (for cover letters and "why us" answers): searches and reads
  the company's pages and keeps a fact only if its quote is visibly on a page
  it read. Letters and answers may only use those facts.

### Your applications

The **Applications** page is your record: every job you've applied to (by the
agent or yourself), when, and what happened since (applied, online
assessment, interview, offer, rejected, withdrawn, no response), with a
history per job. Change an outcome there as news comes in; export it as CSV.

Bring in an existing job sheet with **Import** (or
`uv run job-agent history import sheet.csv`). It needs a link, company, role
and a status column; when several columns hold statuses, the one named after
you is used (or pass `--status-column`). Imported jobs are matched to the job
lists so the agent never prepares them again, and count toward company caps.
Rows without a date are dated on import and marked "?"; set the real date on
the job's page. Applied to something yourself? Open it and click "I applied
to this myself".

### Try it safely first

`uv run pytest` runs the whole flow against copies of Greenhouse, Lever,
Ashby and Workday in the demo site (traps included: hidden AI instructions, honeypots, an
emailed code).

### Settings (`.env`)

| Setting | Default | Meaning |
| --- | --- | --- |
| `SUBMIT_MODE` | `review` | `auto` submits approved-quality packets without asking. |
| `DAILY_SUBMIT_LIMIT` | `15` | Hard cap on submissions per day. |
| `BROWSER_CHANNEL` | `chrome` | `chromium` uses Playwright's browser (run `uv run playwright install chromium`). |
| `COMPANY_CAP`, `COMPANY_WINDOW_DAYS` | `3`, `30` | Default per-company limit (rolling). Known limits are in `company-rules.yaml`. |
| `FIT_THRESHOLD` | `60` | Jobs scoring below this aren't prepared. |
| `MODEL_BACKEND` | `auto` | `claude-code` (your Claude sign-in), `anthropic` (API key), `auto` (the first available), or `off`. |
| `CLAUDE_MODEL` | (empty) | One model for every task. Empty: a model per task (Haiku 5.5 to rank titles, Sonnet 5.5 for reading and research, Opus 5.5 for fit, resumes, letters and answers; see `TASKS` in `job_agent/llm.py`). |
| `CLAUDE_MODEL_LIGHT`, `_STANDARD`, `_BEST` | (empty) | Swap the model for a tier (Haiku, Sonnet, Opus by default). Use full model IDs. |
| `ANTHROPIC_API_KEY` | (empty) | For the `anthropic` backend (or use `ant auth login`). |
| `SPEND_CAP_USD` | `1.00` | With an API key, a run stops once spending reaches this. Claude Code usage comes from your plan. |
| `COVER_LETTER` | `required` | Write cover letters only when required, `always`, or `never`. |
| `RESEARCH` | `auto` | Research the company for letters and "why us" answers, or `off`. |
| `BRAVE_API_KEY` | (empty) | Search for research. Without it, a free search is tried (best effort). |

### Design principles

- **Truthful.** Every answer comes from your profile, resume or what you've
  told it. A tailored resume is rejected if it contains a number your master
  resume doesn't.
- **Can't be steered by the page.** The model only sees text and fields a
  person could see. Hidden instructions and hidden trap fields never reach it,
  and it treats page text as information, not orders.
- **Human checks stay human.** It never tries to solve or get around
  CAPTCHAs, and asks you for one-time codes. Account passwords are generated
  and stored in the macOS Keychain (`job-agent:<site>`). The model never sees
  them.
- **Quality over volume.** Tailored applications get roughly 3–4x the
  response rate of mass ones, and sites restrict accounts that behave like
  bots. Hence the daily cap and the review step.

### Code map

| File | What it does |
| --- | --- |
| `job_agent/cli.py` | Commands |
| `job_agent/sources.py` | Master list: Simplify JSON, Greenhouse boards, refresh |
| `job_agent/filters.py` | Hard filters (location, term, degree, seniority, sponsorship text) |
| `job_agent/companies.py` | Company IDs and aliases (`companies.csv`), caps (`company-rules.yaml`) |
| `job_agent/prepare.py` | Prepare: fit score, tailored resume, drafted answers → packet |
| `job_agent/answers.py` | Answers from profile, saved and approved answers, then the model |
| `job_agent/systems.py` | Which application systems are supported |
| `job_agent/forms.py` | What the fillers share: option matching, widgets, waiting for the result |
| `job_agent/greenhouse.py`, `lever.py`, `ashby.py` | Each system: posting API, form reading, coded filler, submit |
| `job_agent/workday.py` | Workday: accounts, sign-in, the page-by-page walk |
| `job_agent/packets.py` | Packet files, the approval hash, the consistency check |
| `job_agent/actions.py` | Approve, edit, reject, resolve |
| `job_agent/submit.py` | Coded submit with the "form changed" check |
| `job_agent/worker.py` | The single process that owns the browser and runs tasks |
| `job_agent/dashboard.py` | The local dashboard |
| `job_agent/profile_sync.py` | Profile facts from the master resume, work authorization rules |
| `job_agent/history.py` | Application record: import a job sheet, export, outcome stats |
| `job_agent/resumes.py` | Resume PDFs → Markdown → merged master, with truth checks |
| `job_agent/llm.py` | Claude clients (Claude Code headless, Anthropic API): JSON replies, agent turns, spending cap |
| `job_agent/matching.py` | Ranks jobs against the description on the Search page |
| `job_agent/latex.py` | Resumes in your LaTeX template, fitted to one or two pages |
| `job_agent/ats.py` | Which application system a URL belongs to, and its job ID |
| `job_agent/prompts.py` | Instructions for each model step and each agent |
| `job_agent/harness.py` | The agent loop: tool calls, errors back to the model, limits, traces |
| `job_agent/planner.py` | The planner agent and its pipeline tools |
| `job_agent/web.py`, `pageview.py` | The explorer agent, its page view, and the coded replay |
| `job_agent/research.py` | The research agent: search, read, quote-checked facts |
| `job_agent/browser.py` | The headless browser (PDFs, visible text) and the Chrome window for submits |
| `job_agent/tracker.py` | SQLite jobs with enforced status moves, caps, tasks |
| `job_agent/documents.py` | Resume/cover letter PDFs and the invented-number check |
| `job_agent/demo_site.py`, `demo_greenhouse.py`, `demo_lever.py`, `demo_ashby.py`, `demo_workday.py` | The fake job board and system copies for testing |

Full step-by-step logs of each run are in `data/logs/`.

### Class app store version

The workshop's class app store version (sandboxed Chromium behind the
platform's proxy, class model broker, 30-minute sessions) lives on the
[`class-platform`](https://github.com/wid0gast/job-agent/tree/class-platform) branch.

### Tests

```sh
uv run pytest
```

The tests run the real browser tools against the demo site (no model, no
cost): signup, emailed code, a form inside an iframe, upload, submit gate and
the hidden-trap checks.

### Roadmap

See [PLAN.md](../PLAN.md).
