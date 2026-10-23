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

1. Describe the jobs you want on the dashboard's **Search** page: in your own
   words, plus fields, job types, seniority and other filters. Your name,
   contact details, links, education and work authorization are read from your
   master resume (step 2) into `profile/resume-profile.yaml`; you don't type
   them. If your resume doesn't state your work authorization, choose it once
   on the **Profile** page, so forms' authorization and sponsorship questions
   are answered in code instead of left for you. The Profile page also lets
   you edit everything else (preferences, standard answers, saved answers,
   story bank, writing sample) at any time.
2. Build your **master resume** (everything true you might ever include) in
   one of two ways. Either upload your resume PDFs on the dashboard's
   Resumes page (or `uv run job-agent resumes import a.pdf b.pdf`): each is
   read, then all are merged, newest winning where they disagree, and you
   review the result before it replaces `profile/resume.md`. Or write it
   yourself in the editor on the Resumes page, which also lets you change it
   any time (the previous version goes to `profile/resume-history/`, and your
   profile facts are re-read from it). The agent cuts and reorders it per job
   but never adds facts. Optional: a writing sample on the Profile page, so
   cover letters sound like you.
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
the assistant chat (you can pick another model per chat), and Opus 5.5 checks fit and writes the tailored resume, cover
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
1. **Refresh** pulls Simplify's internship and new-grad lists for the fields
   you tick on Search (Software, Data & AI, Hardware, Quant, Product; all by
   default) and the Greenhouse, Lever and Ashby boards of companies seen
   there. Boards list every role at every level, so their jobs are matched to
   your fields by title; "Other roles" keeps the rest (sales, operations,
   finance...). Senior, staff and manager roles are filtered out unless you
   tick "Include senior roles". Hard filters from
   `preferences` in your profile run in code; the Jobs page shows why each
   filtered job is out.
   **Discover companies** (Today) finds companies the lists don't have: Claude
   runs up to 10 web searches for your role titles and locations, limited to
   the sites companies hire through (Greenhouse, Lever, Ashby, Workday,
   SmartRecruiters, Workable, BambooHR, Jobvite, JazzHR, iCIMS). Each company
   board found joins `companies.csv` and is read on every refresh from then
   on; Workday postings, which have no board feed, are added directly. One
   Sonnet request per run; with an API key, web searches cost $10 per 1,000.
   **One job, once.** A posting is the same job when it has the same system
   job ID, or the same company and role on another system ("SWE Intern" on
   Workday and "Software Engineer Intern - Summer 2027" on Greenhouse).
   Shorthand is expanded and season, year and degree tags are ignored, but an
   internship and a full-time role stay different jobs. A job that looks like
   one you already applied to at that company is filtered out with the reason
   ("Looks like your application to "MLE""); "Back to found" keeps it.
   Postings for a season that has passed ("Summer 2026") and remote roles
   outside the US ("Remote Spain", when you chose Remote) are filtered out too.
   **Ranked jobs** lists them by score. Every job first gets a free, instant
   *keyword* score (its title against your role titles and description;
   words you rule out, like "not marketing", count against it). "Rank the
   top 120 with the model" then gives the best keyword matches a *match*
   score (title, company and location against your description, ~60 jobs per
   request, two requests at a time); click again for the next 120. Once a
   job is prepared it has a *fit* score from the full posting. Model scores
   rank above keyword-only ones, and jobs below your threshold are greyed.
   Ranking runs alongside other tasks, so you can prepare while it works.
   **How to apply.** Every job you haven't applied to gets *Reach out first*,
   *Apply* or *Think twice*, with its reasons, decided in code (no model):
   *Think twice* when the role was posted again in the same place 21+ days
   later, has been open 45+ days (90 for internships), you've applied there
   for the same role, or the company limit is full; *Reach out first* when a
   person found at the company shares your background or works in the role
   and hasn't heard from you (or you're waiting on them, or they replied).
   It shows on Ranked jobs, the Approval queue and each job's page.
2. **Prepare** (per job, or "Prepare top matches", which takes the best-ranked,
   one per company and none at a company already at its limit, and passes over
   Think twice jobs, saying which) reads the posting and the
   application form without signing in or clicking anything, scores fit,
   tailors your resume, and drafts every answer, recording where each came
   from: your profile, saved answers, answers you approved before, or the
   model. Nothing is submitted. The job's page shows each step as it
   finishes. A job whose fit is below your threshold is set aside: its page
   shows the reason and the full fit check, and **Prepare anyway** goes ahead
   with that same fit check. Ranked jobs lists what you asked to prepare and
   how each went.
   **A hiring manager's first read.** After the packet is built, a different
   model (Sonnet; Opus wrote it) reads it as the hiring manager would on a
   quick first pass: which pile it lands in, what they'd remember, what they
   looked for and didn't find, and up to three lines that hurt it, each with a
   fix from your real material. Each quoted line is checked to be in the
   application. It changes nothing; "Read it again" after your edits.
3. **Approval queue.** Each job's page starts with the decision at a glance
   (how to apply, the hiring manager's read, fit, whether it's ready) and
   tabs for Review, Application, Fit and research, and People. Review a
   packet, edit any answer, then approve.
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

### The assistant

The **Assistant** tab is a chat that can do everything the dashboard does, so
you can use it as your only way into the app: "find my 5 best matches in NYC,
one per company, and prepare them", "add my new project to my resume", "make
the Stripe cover letter shorter", "what's waiting for me?". It also searches
and reads the web.

- **Tools, not guesses.** About 25 tools cover jobs (find, open, refresh,
  discover, rank, reject, restore, record outcomes), applications (prepare or
  redo with a note, read a packet, edit an answer, edit the tailored resume or
  cover letter and typeset it again, find people to contact and edit or send
  their drafts), your files (master resume, profile,
  saved answers, story bank, writing sample), search settings and background
  tasks. Each tool call shows in the chat as one line you can expand.
- **Slow work stays in the worker.** Preparing and submitting run as tasks;
  the chat shows them live and Claude can wait for them, so one request can go
  from finding jobs to a ready application. If a task needs you (an emailed
  code, a CAPTCHA), the question appears at the top of the chat.
- **You decide what can't be undone.** Claude can only *ask* to submit: a card
  lists the applications, and nothing is approved or sent until you click
  "Approve and submit". Caps still apply. Edits to your resume or profile get
  an Undo button. A short answer about you (dates, authorization, links,
  choices) is saved only if it's in your own words, in the chat or your
  profile; numbers in a resume or letter must come from your master resume.
  Web pages and postings are treated as data, never as instructions.
- **Conversations are kept.** Chats are listed on the left and continue where
  you left off. Each message tells Claude what you did since (your clicks on
  cards, tasks that finished) and the app's current state.
- **How it connects.** With your Claude plan, Job Agent runs Claude Code
  through the Claude Agent SDK, with the app's tools as an in-process tool
  server plus Claude Code's own web search and fetch. Your Claude Code
  settings, connectors and skills are left out. With an API key, it uses the
  Messages API with the same tools, server-side web search and fetch, and
  Claude's thinking kept between turns. Sonnet 5.5 by default; the picker
  under the message box switches a chat to Haiku or Opus.

### People to contact

Each prepared application can include a few people at the company, each with
a short email, a LinkedIn connection note and a follow-up, drafted for you.
Choose who on the **Search** page (or untick all to turn it off):

| Who | Why |
| --- | --- |
| **Shares your background** (same school or past employer) | The likeliest to reply, and a conversation with someone inside is how referrals happen. |
| **In this role or team** | Can tell you what the work is really like. |
| **Recruiter** | Fine to contact once you've applied. |
| **Hiring manager** (off by default) | Advice is mixed: some welcome a specific note, some pass it to HR. |

This follows what career advisers recommend (Steve Dalton's *The 2-Hour Job
Search*, university career centers, Ask a Manager): ask for their advice, not
a job or a referral; under 100 words; what you share first; one clear
question; no attachments; one person per company at a time, the next after 3
days without a reply; one follow-up after a week, then stop.

- **Finding people.** An agent searches the web the way you would (LinkedIn
  results, the company's blog and team pages). Code keeps a person only if a
  page or search result it saw names them at the company (former employees are
  skipped), a LinkedIn link only as a search result gave it, and a shared
  school only if a page says so. LinkedIn itself is never opened or automated.
  The job page links to how it searched.
- **Email addresses.** Only addresses shown on a page, or guessed from the
  company's format when a page shows a named employee's address (checked, and
  marked "guessed": these can bounce). Search results rarely show addresses,
  so many people come with a LinkedIn link and note only; paste an address in
  if you have one.
- **Drafts.** Written from your resume, the posting and verified company facts.
  Code checks the length, the question and that no number is invented. Edit
  any of it on the job's page, or ask the assistant.
- **Sending is yours.** "Send email in Gmail" (or your mail app) opens the
  draft in your own email, and you press Send. The app records it (undo if you
  didn't send it), keeps to `DAILY_OUTREACH_LIMIT`, and reminds you on Today
  when to try the next person or follow up. Mark "They replied" to stop the
  reminders. LinkedIn notes: Copy, open their profile, Connect, Add a note.
- **Postings that say not to contact** anyone are respected, and a redo of a
  packet keeps the people you've already emailed.

### The agents inside the pipeline

Most of the app is a fixed pipeline (find → rank → prepare → approve →
submit). Three agents handle the parts with no fixed path. Neither can approve
or submit for you, change caps, or invent an answer; those checks are code.

- **Explorer** (systems without a coded filler: iCIMS, custom career pages):
  during prepare it fills the form in the browser, creating an account and
  asking you for emailed codes if needed, and stops at the final submit.
  Every value comes from the answer pipeline, your replies or your documents.
  After you approve, code replays its recorded steps; the job page shows them.
- **Researcher** (for cover letters and "why us" answers): searches and reads
  the company's pages and keeps a fact only if its quote is visibly on a page
  it read. Letters and answers may only use those facts.
- **People finder** (see People to contact): searches for people at the
  company; code checks each one against what the pages and results showed.

### Seeing how it works

- **How this was made.** Every run on a job (prepare, finding people,
  rewriting messages, typesetting, submit) is recorded as a timeline, opened
  from the job's page or from **Activity**. Each step shows how long it took
  and what came of it. Inside are the model requests (which model, what
  effort, time and tokens; "Show prompts and replies" opens exactly what was
  sent and what came back), agent runs (linking to every turn and tool call)
  and the **checks**: rules in code that passed (✓) or caught something and
  acted on it (⚑), such as hidden text removed from a posting, a resume draft
  that invented a number, or a person kept only because a page names them at
  the company. "Only the checks" shows just those. Traces live in
  `data/traces/<job id>/`, the last 8 runs per job.
- **How it works** (a tab): the journey of one application, step by step and
  who does each (code, a model, an agent, or you), and the reasons behind the
  main design choices. Its text is `job_agent/static/how-it-works.md`.
- **The chat** shows under each reply which model answered, how many requests
  it took, the time and the tokens. A running task links to its live timeline.
- **Rehearsing a demo.** `uv run job-agent demo save` snapshots a demo home
  with made-up data (`.demo-readme/` by default); `uv run job-agent demo`
  restores it and opens its dashboard on port 8771, separate from yours.

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
| `DAILY_SUBMIT_LIMIT` | `15` | Hard cap on submissions per day. |
| `BROWSER_CHANNEL` | `chrome` | `chromium` uses Playwright's browser (run `uv run playwright install chromium`). |
| `COMPANY_CAP`, `COMPANY_WINDOW_DAYS` | `3`, `30` | Default per-company limit (rolling). Known limits are in `company-rules.yaml`. |
| `FIT_THRESHOLD` | `60` | Jobs scoring below this aren't prepared. |
| `MODEL_BACKEND` | `auto` | `claude-code` (your Claude sign-in), `anthropic` (API key), `auto` (the first available), or `off`. |
| `CLAUDE_MODEL` | (empty) | One model for every task. Empty: a model per task (Haiku 5.5 to rank titles, Sonnet 5.5 for reading and research, Opus 5.5 for fit, resumes, letters and answers; see `TASKS` in `job_agent/llm.py`). |
| `CLAUDE_MODEL_LIGHT`, `_STANDARD`, `_BEST` | (empty) | Swap the model for a tier (Haiku, Sonnet, Opus by default). Use full model IDs. |
| `ANTHROPIC_API_KEY` | (empty) | For the `anthropic` backend (or use `ant auth login`). |
| `SPEND_CAP_USD` | `1.00` | With an API key, a run stops once spending reaches this. Claude Code usage comes from your plan. |
| `COVER_LETTER` | `always` | Write a cover letter whenever the form has a place for one (`always`), only when it's required, or `never`. The Search page's choice overrides this. |
| `RESEARCH` | `auto` | Research the company for letters and "why us" answers, or `off`. |
| `DAILY_OUTREACH_LIMIT` | `10` | Emails to people at companies per day (first emails and follow-ups). |
| `BRAVE_API_KEY` | (empty) | A search API for research and finding people. Without it, Claude's own web search is used. |

### Design principles

- **Truthful.** Every answer comes from your profile, resume or what you've
  told it. A tailored resume is rejected if it contains a number your master
  resume doesn't.
- **Can't be steered by the page.** The model only sees text and fields a
  person could see. Hidden instructions and hidden trap fields never reach it,
  and it treats page text as information, not orders.
- **No guessing from a page it couldn't read.** Before any model call, code
  checks the posting: at least 80 words a person can see, and, on career
  sites read through the browser, the job's title on the page (postings inside
  frames are read too). A sign-in wall, a privacy notice or a site that turns
  away automated readers stops the prepare with the reason, instead of a fit
  score and resume tailored to the wrong text.
- **Human checks stay human.** It never tries to solve or get around
  CAPTCHAs, and asks you for one-time codes. Account passwords are generated
  and stored in the macOS Keychain (`job-agent:<site>`). The model never sees
  them.
- **Quality over volume.** Hiring managers discount generic and AI-sounding
  applications (about half count heavily AI-written ones as a red flag), and
  sites restrict accounts that behave like bots. Hence the daily cap, the
  per-company cap and the review step.

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
| `job_agent/actions.py` | Approve, edit, reject, resolve, save search and profile (shared by the dashboard and the assistant) |
| `job_agent/submit.py` | Coded submit with the "form changed" check |
| `job_agent/worker.py` | The single process that owns the browser and runs tasks |
| `job_agent/dashboard.py` | The local dashboard, including the Assistant tab |
| `job_agent/profile_sync.py` | Profile facts from the master resume, work authorization rules |
| `job_agent/history.py` | Application record: import a job sheet, export, outcome stats |
| `job_agent/resumes.py` | Resume PDFs → Markdown → merged master, with truth checks |
| `job_agent/llm.py` | Claude clients (Claude Code headless, Anthropic API): JSON replies, agent turns, spending cap |
| `job_agent/matching.py` | Ranks jobs against the description on the Search page |
| `job_agent/latex.py` | Resumes in your LaTeX template, fitted to one or two pages |
| `job_agent/ats.py` | Which application system a URL belongs to, and its job ID |
| `job_agent/prompts.py` | Instructions for each model step and each agent |
| `job_agent/harness.py` | The agent loop: tool calls, errors back to the model, limits, traces |
| `job_agent/trace.py` | Each run's timeline: steps, model requests, agent runs and checks |
| `job_agent/assistant.py` | The assistant's tools and their guards |
| `job_agent/chat.py` | Chats, running a turn, and the two ways to Claude (Agent SDK, Messages API) |
| `job_agent/static/chat.js` | The chat page: sending, live updates, cards |
| `job_agent/web.py`, `pageview.py` | The explorer agent, its page view, and the coded replay |
| `job_agent/research.py` | The research agent: search, read, quote-checked facts |
| `job_agent/people.py` | People to contact: the finder agent and its checks, drafts, sending and reminders |
| `job_agent/strategy.py` | How to apply: Reach out first, Apply or Think twice, from signals in code |
| `job_agent/first_read.py` | A hiring manager's first read of each packet, quotes checked in code |
| `job_agent/style.py` | Writing checks: stock words and openers, em dashes, length limits |
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
