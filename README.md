## Job Agent

A local agent that finds jobs matching what you ask for, tailors your resume
to each one, writes a cover letter when needed, fills in the application and
submits it, with you approving each submit. It tracks every job so it never
applies twice.

It runs on your laptop in a visible Chrome window. You can watch it work and
step in at any time.

### Setup (once)

Requires [uv](https://docs.astral.sh/uv/), Google Chrome, and for resumes in
your LaTeX style, [Tectonic](https://tectonic-typesetting.github.io/)
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
3. Put your OpenRouter key and a model ID in `.env`. Free models work for
   testing (for example `google/gemma-4-31b-it:free`), within their daily
   request limits.
4. Optional: `profile/story-bank.md` (your real examples) and
   `profile/writing-sample.md` help with essay questions.

`profile/`, `data/` and `.env` are gitignored and stay on your machine.
Profile and resume content is sent to your chosen model while applying.

### Everyday use: the dashboard

```sh
uv run job-agent dashboard        # http://127.0.0.1:8770
```

0. **Search**: describe in your own words what you're looking for, set the
   filters (internship/full-time, locations, seasons, posting age,
   exclusions) and the fit threshold. Saved to `profile/search.yaml`.
1. **Refresh** pulls Simplify's internship and new-grad lists (AI/ML/Data)
   and the Greenhouse, Lever and Ashby boards of companies seen there. Hard filters from
   `preferences` in your profile run in code; the Jobs page shows why each
   filtered job is out.
   **Ranked jobs** lists them by score: the quick *match* score (title,
   company and location against your description, ~60 jobs per model
   request) until a job is prepared, then its *fit* score from the full
   posting. Jobs below your threshold are greyed.
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
| `SPEND_CAP_USD` | `1.00` | A run stops once model spending reaches this. |
| `OPENROUTER_DATA_COLLECTION` | `deny` | Skip providers that keep your data. Some free models need `allow`. |
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
| `job_agent/llm.py` | Model clients (OpenRouter, the class broker): JSON replies, retries, spending cap |
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
| `job_agent/hosted.py`, `netrelay.py` | The class app store version (below) |

Full step-by-step logs of each run are in `data/logs/`.

### Class app store version

`python -m job_agent.hosted` runs this same app on the class platform:
everything lives in the session's workspace, models come from the class
broker, Chromium is unpacked at build time (`scripts/hosted_build.py`), and
`netrelay.py` carries the browser's and LaTeX's traffic through the platform
proxy. Sessions keep nothing, so the dashboard offers a download of
everything. Build the upload (no personal data goes in):

```sh
uv run python scripts/package_app.py --output ../job-agent-app.zip
```

`appstore/` holds its manifest, README and dependency lock;
`appstore/bin/tectonic` is the Linux Tectonic 0.17.0 binary (not in git).

### Tests

```sh
uv run pytest
```

The tests run the real browser tools against the demo site (no model, no
cost): signup, emailed code, a form inside an iframe, upload, submit gate and
the hidden-trap checks.

### Roadmap

See [PLAN.md](PLAN.md).
