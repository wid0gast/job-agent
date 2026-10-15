# Job Agent: Plan

Status: Phase 1 slice built (2026-10-13). Items marked **(exists)** are
built; the rest is planned. Next: the agents (A1 → A2 → A3) below.

## Goal

An agent that finds data science, ML and AI jobs (internships and full-time),
prepares a tailored, truthful application for each one, and submits only
what you approve, while respecting each company's application limits. Later
it grows into networking and interview prep.

## Principles

1. **You approve every application by default.** An auto-submit mode exists
   but is off unless you turn it on.
2. **Truthful.** Every resume line and answer comes from your profile, base
   resumes, story bank or answers you've given. No invented experience or
   numbers. **(exists, for numbers)**
3. **Quality over volume.** Tailored applications get several times the
   response rate of mass ones. Recruiters flag bursts of applications, generic
   answers and answers that don't match the resume.
4. **Respect company limits.** Never exceed a company's cap, never reapply
   inside a cooldown, and stay under 3 applications per company per rolling
   30 days by default.
5. **The page can't steer it.** Hidden text and hidden fields never reach the
   model, and page text is treated as information, not instructions.
   **(exists)** Page text can only make the agent more cautious (pause a
   company, send a packet back), never less.
6. **Human checks stay human.** CAPTCHAs, SMS codes and logins go to you. It
   never tries to get around them. **(exists)**
7. **Private by default.** Your data stays on your laptop, except what goes
   to the model while preparing an application. Model requests use OpenRouter's
   `data_collection: "deny"` so providers that keep data are skipped. Email
   access is off unless you opt in.

## What exists today

- Command-line agent: search on configured sites, then a fresh agent per job
  that tailors a resume, writes a cover letter, fills forms in a real Chrome
  window and submits after terminal approval.
- Job tracker with duplicate detection across sites, a daily submission cap,
  and passwords kept in the Keychain.
- Visible-text-only page reading, an invented-number check on tailored
  resumes, and an applicant tracking system playbook.
- Demo job site with traps, 8 passing tests, and a class platform version
  (`hosted/`) that runs on the demo board.

## Where the jobs are

Active AI/ML/Data postings in Simplify's lists (checked 2026-10-13), by
application system:

| System | Internships (1,672) | New grad (1,090) |
| --- | --- | --- |
| Workday | 40% | 20% |
| Greenhouse | 9% | 12% |
| iCIMS | 8% | 9% |
| Oracle | 7% | 5% |
| Ashby | 5% | 6% |
| Lever | 2% | 10% |
| Other (TikTok/ByteDance, Google, Meta sites, …) | 25% | 31% |

Workday is the main case, not an edge case. It needs an account per company,
an emailed code and multi-page forms, so it is planned for in Phase 1.

## How an application flows (new)

```
master list ─► filters + fit score ─► company rules check ─► PREPARE
                                                               │
                         (unattended: tailor resume, cover letter, get every
                          question, draft answers, save a packet)
                                                               ▼
                                              APPROVAL QUEUE (dashboard)
                                       you: approve / edit / reject, in bulk
                                                               ▼
                                                            SUBMIT
                          (code, not an agent: replays the approved answers,
                           submits, saves a screenshot as proof)
```

- **Packets.** Each prepared application is saved as `packet.json` plus
  documents: job, fit reasons, tailored resume, cover letter, every question
  with its drafted answer and where the answer came from.
- **Prepare depends on the system:**
  - Greenhouse: questions and options from the public API
    (`.../jobs/{id}?questions=true`), plus a headless read of the form, which
    adds fields the API leaves out (phone country, city, education). Never
    signs in or clicks anything. **(exists)**
  - Lever: posting text from the public API; the form read from the page
    (fields keyed by input name; the resume goes first because Lever pre-fills
    from it). **(exists)**
  - Ashby: the form schema (types, required, options) from the job page's
    public GraphQL API, widgets from the page (yes/no buttons, radios,
    location, education block). **(exists)**
  - Workday: questions are spread over pages that appear one after another
    (many sites show the first before sign-in). Prepare drafts what's visible
    plus Workday's standard fields. Submit walks the pages: it fills approved
    answers (and ones your profile, saved or earlier-approved answers cover),
    and a required question with none sends the job back to you with the
    question added; the model can draft it as a separate step. The final
    click happens only when the footer button says Submit. **(exists)**
- **Submit is code, not a fresh agent.** **(exists, Greenhouse)** It replays the approved
  field → value list. The model may help find a field on the page but never
  chooses a value. This makes submit more reliable, and once you approve, the
  page can't change what gets sent (the "plan-then-execute" defence against
  prompt injection).
- **Approval is per answer, keyed by question wording.** **(exists)** At submit, a
  question you already approved, or one with a saved answer, is filled
  without stopping. Only a new *required* question without an approved
  answer, changed options on an approved question, a CAPTCHA or a code sends
  the packet back to the queue marked "needs you".
- **Auto-submit mode (opt-in, off by default).** When turned on in
  Settings, packets that pass every check go out without waiting in the
  queue. Guardrails still apply: fit score above a threshold you set,
  company rules, the consistency check, the daily cap, and anything
  unexpected goes back to the queue. It always shows a clear "auto-submit is
  on" banner. I recommend leaving LinkedIn out of auto-submit, since it
  restricts accounts for automated activity.

## Engineering foundations

- **Explicit states in SQLite** **(exists)**: found → filtered → prepared → approved →
  submitting → submitted / needs_you / failed / rejected, with the allowed
  moves enforced in code. `submitting` is written *before* the click. A job
  left in `submitting` (crash, closed window) is never retried
  automatically, because a double application is what company limits punish.
- **Caps are reserved at approval.** **(exists)** Approving a packet takes a slot against
  the company cap and the daily cap. Bulk-approving 5 jobs at a company
  capped at 3 is refused at approval, not discovered at submit.
- **One company ID with aliases** in `companies.csv` (for example TikTok and
  ByteDance; Amazon and AWS). **(exists)** Caps count by company ID. Duplicate
  detection prefers the source's own job ID (Simplify `id`, ATS job ID)
  over matching company and title text.
- **One worker owns the browser.** **(exists)** Playwright's sync API isn't thread-safe.
  The dashboard writes tasks to a SQLite table (WAL mode) and a single worker
  process runs them. A stopped task records where and why, so it can pick up
  from there.
- **Dashboard safety.** **(exists)** Any website you visit can send requests to
  `127.0.0.1`. The dashboard checks the Host and Origin headers and requires
  a per-session token on every change.
- **Times in UTC, with day boundaries in your time zone.** **(exists)**
- **Posting text is rendered headlessly** and only visible text reaches the
  model, so hidden instructions in a posting's HTML are dropped. **(exists)**
- **Model calls** **(exists)**: structured outputs (`response_format: json_schema`) for
  fit scores and checks; a cheap model for fit scoring, a strong one for
  tailoring and answers; the per-run spending cap sums `usage.cost` from each
  response.
- **Harness stays ours** (`harness.py`, about 180 lines). Browser-use,
  Stagehand and agent frameworks are not adopted: the hidden-text filter and
  submit gate are core safety features they don't provide, and coded submit
  shrinks the model-driven part further.

## Phase 1: Core

Built as one end-to-end slice first, then widened:

1. **Slice:** Simplify JSON → hard filters → Greenhouse-only prepare →
   packet → approve in a minimal dashboard → coded submit → screenshot.
   **(exists)** Tested end to end on the demo Greenhouse; filling tested on
   real Greenhouse forms without submitting.
2. Lever and Ashby, with their board feeds in refresh. **(exists)** Filling
   tested on real forms without submitting.
3. Workday: accounts (Keychain password per site, verification emails via
   the dashboard), sign-in, the page-by-page walk. **(exists)** Tested end
   to end on a demo copy; real first pages read and filled without
   submitting. Not yet: Workday's own job feed in refresh (Simplify lists
   Workday jobs), work-experience and education "Add" sections.

### 1.1 Master job list

Sources, refreshed daily:

| Source | Covers |
| --- | --- |
| [SimplifyJobs/Summer2027-Internships](https://github.com/SimplifyJobs/Summer2027-Internships) | Internships. Updated hourly. |
| [SimplifyJobs/New-Grad-Positions](https://github.com/SimplifyJobs/New-Grad-Positions) | Full-time new grad roles |
| Company job boards (below) | Every open role at known companies, including full-time roles beyond new grad |
| [JobSpy](https://github.com/speedyapply/JobSpy) (MIT) | LinkedIn, Indeed, Glassdoor search without a model. Low volume on LinkedIn. |

- Read Simplify's `.github/scripts/listings.json` (fields: `company_name`,
  `title`, `url`, `locations`, `active`, `category`, `degrees`, `terms`,
  `date_posted`), not the README tables. Filter on `category: "AI/ML/Data"`.
  Fetch with ETag so unchanged files aren't re-downloaded. Inactive postings
  drop out. **(exists)**
- speedyapply has no data file and overlaps with Simplify, so it is left out
  of v1.
- **Company list grows itself.** When a listed job's apply link goes to a
  known system, record that company's board ID and read its whole board:
  - Greenhouse: `boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true`
  - Lever: `api.lever.co/v0/postings/{slug}?mode=json` (EU tenants use the EU host)
  - Ashby: `api.ashbyhq.com/posting-api/job-board/{name}?includeCompensation=true`
  - Workday: `POST {tenant}.wdN.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs`
    (undocumented; back off on 429/503 and expect it to change)
- **New fields on request.** If someone asks for a field the list doesn't
  cover, the agent finds lists for that field and adds those companies, with
  field tags.
- LinkedIn, Handshake and Jobright stay as extra places to discover jobs.
  Companies found there are added to the company list too.

Storage:

- `companies.csv` in the repo (public information): company ID, aliases,
  name, website, application system, board ID, field tags, source, date added.
- `company-rules.yaml` in the repo (public information), described in 1.3.
- Jobs, packets, tasks and learned rules in `data/` (private, gitignored).
  **(tracker exists)**

### 1.2 Filters and fit score

- **Hard filters in code, before the model** **(exists)**: location, remote/onsite,
  internship vs full-time, seniority, graduation window, excluded companies,
  posting age. Sponsorship and work authorization come from the posting
  text: Simplify's `sponsorship` field is "Other" on over 99% of postings.
- **Fit score (model, structured output)** **(exists)**: must-haves vs nice-to-haves from
  the posting, compared with your profile and resumes. Shows reasons, for
  example "matches Python and PyTorch; asks for 2 years of experience, you
  have 1."
- **Posting quality flags** (LinkedIn, Handshake and Jobright only; postings
  from a company's own board pass by definition):
  - Scam signs: asks for payment, personal email domain, no real company
    website, vague description, pay that seems too good.
  - Ghost job signs: old or repeatedly reposted, no salary, not on the
    company's own careers page.
  - Flagged jobs are shown with the reason and are never prepared
    automatically.

### 1.3 Company rules list

`company-rules.yaml`, one entry per company ID:

```yaml
google:
  max_applications: 3
  window_days: 30          # rolling
  reapply_same_job_days: 90
  source: https://support.google.com/googlecareers/answer/6095391
  confidence: official
amazon:
  max_active_applications: 10
  source: forum reports
  confidence: unverified
```

- Default for every company without a rule: **at most 3 applications per
  rolling 30 days**, and never the same job twice.
- Checked before preparing, reserved at approval, checked again before
  submitting.
- **Learns new rules.** When a site says something like "you have reached the
  application limit", the agent records it in `data/` (marked unverified) and
  pauses that company. You copy confirmed rules into the repo file by hand.
- **Best roles under a cap.** If a company has more matching roles than its
  cap allows, rank them by fit, and you choose in the dashboard.

### 1.4 Better applications

- **Master resume from your PDFs** **(exists)**: upload any number of resume
  PDFs; each is converted to Markdown by the model with its wording kept. The
  merge is code: the newest version (latest past dates) is the base, and an
  older line is added, in place, only when no newer line covers it. Words or
  numbers in none of the PDFs are flagged; you can edit, then accept, before
  it replaces the master. Old masters are kept.
- **Profile from the master resume** **(exists)**: contact details, links,
  education and work authorization are derived on accept (values must appear
  in the resume; work authorization by fixed rules). profile.yaml keeps
  preferences and can override.
- **Several base resumes** (for example data science, ML engineering). The
  agent starts from the closest one.
- **Tailoring report:** requirements you match, skills it emphasized, honest
  gaps, and a side-by-side view against the base resume.
- **Story bank:** your real examples, written once, used for "tell us about a
  time…" questions.
- **Saved answers** reused across applications. **(exists)**
- **Consistency check before approval** **(exists)**: dates, graduation, work
  authorization, titles and contact details match across resume, answers and
  profile. Plain code against `profile.yaml` where possible; the model only
  for free-text answers. Mismatches block approval until fixed.
- **Your voice:** writes from your writing sample and avoids stock AI
  phrases.
- **One-page check:** renders the PDF and trims if it spills onto a second
  page. (The page count is shown today; trimming is not built.)

### 1.5 Local dashboard

Runs on your laptop at `http://127.0.0.1:<port>` and replaces the terminal
for everyday use. It shares one interface with the class version, which runs
on the demo board.

| Page | Contents |
| --- | --- |
| Today | New matches since the last refresh, the approval queue count, items that need you, the auto-submit banner if on |
| Approval queue | Per packet: fit score and reasons, tailoring report, resume, cover letter, every answer with its source. Approve, edit, reject, or approve in bulk. |
| Pipeline | found → prepared → approved → submitted → response → interview → offer, with documents and the submission screenshot |
| Master list | Jobs and companies with filters (field, type, location, deadline, company limit status), refresh, CSV export |
| Profile | Profile, base resumes, story bank, saved answers |
| Settings | Sites, auto-submit (off), fit threshold, per-company default cap (3 per 30 days), daily cap, email opt-in (off), model, spending cap per run |
| Activity | Live view of the agent's steps and logs |

## Phase 2: Reliability

- **Test copies of real forms** (Greenhouse, Lever, Ashby, Workday, iCIMS) in
  the demo site, scored on completion rate and wrong fields. Run on every
  change. Real runs track the same numbers plus the "needs you" rate.
- **Model evaluation:** about 30 real postings you label (apply or not, and
  why) to score fit scoring and answers on every prompt change, using the
  existing JSONL logs.
- **Opt-in email codes:** read-only Gmail access. Google's narrowest read
  permission still covers all mail, so code limits it with a search filter
  to messages from sites it just signed up on. When off (the default), it
  asks you for codes as it does now.

## Phase 3: Tracking

- **Application record** **(exists)**: every application with its date, how
  it was made (agent or you) and its outcome (applied, OA, interview, offer,
  rejected, withdrawn, no response) with a dated history; job sheet import
  (matched to the job lists, counted toward caps; undated rows dated on
  import and flagged), CSV export, response-rate stats. Company names match
  regardless of spacing, with aliases (Amex → American Express).
- **Reply detection (opt-in email):** spot rejections and interview invites
  and update the pipeline. Without email access, you update statuses in the
  dashboard.
- **What works:** response rates by base resume, source, company type and
  role, fed back into fit scoring and tailoring.
- **Follow-up reminders.**

## Phase 4: Expansion

- **Networking:** find recruiters, hiring managers and alumni at a company,
  and draft a personal note that you send yourself at first. Referrals are
  about 7% of applicants but about 72% of interviews.
- **Interview prep:** when an invite arrives, produce a short brief on the
  company, role and likely questions.

## Agents (next: A1 → A2 → A3)

Today the app is a workflow: code picks every step and the model answers
single JSON calls. That stays for known paths (the four supported systems,
submits). Agents go where the path is unknown. **Safety stays in code**: no
agent tool approves, submits, changes caps or edits an approved packet; the
invented-number check, approval hash and CAPTCHA/code handoff apply as now.

### Shared foundation

- **Harness:** restore `harness.py` from commit `8c173bb` (loop, tool errors
  returned to the model, JSONL step log, step cap) and `llm.complete()`. The
  broker client already streams tool calls.
- **Tools** wrap existing functions and return small JSON. `ask_user` uses
  the worker's question box (with the hosted screenshot link).
- **Trace page** (`/runs/<id>`): each step's reasoning, tool, arguments and
  result summary, refreshing live. Linked from Today and Activity.
- **Limits per run:** step cap, spend cap, wall-clock cap (hosted runs must
  finish inside a 30-minute session, one model call at a time).
- **Tests:** a scripted fake model drives each agent against the demo boards.

### A1. Planner

You give a goal on Today ("fill the queue with my 8 best ML internships,
NYC or remote"). The agent plans and acts until the queue holds them.

| Tool | Wraps |
| --- | --- |
| `refresh_jobs`, `rank_jobs` | `sources.refresh`, `matching.score` |
| `list_jobs(filters)`, `read_job(id)` | tracker queries, posting text |
| `check_caps(company)` | `tracker` cap check |
| `prepare(id)`, `read_packet(id)` | `prepare.prepare`, packet summary (fit, gaps, needs-you items) |
| `redo(id, note)` | re-prepare with a note (at most twice per job) |
| `ask_user(question)` | one consolidated question instead of one per packet |
| `finish(summary)` | ends the run |

Done when: on the demo boards it fills the queue to the target, skips
capped companies, asks one combined question for a shared gap, and the
trace shows why it picked and redid each job.

### A2. Unsupported sites, with learned replay

For jobs on systems without a coded adapter (iCIMS, SmartRecruiters, Taleo,
custom pages) and Workday walks that hit a new question.

- **Explore:** a browser agent (the old snapshot/ref tools, final-submit
  click blocked) fills the form from the packet tools (`answer_for(question)`
  via `answers.py`, `ask_user`, `remember_answer`) and stops at the final
  review page.
- **Record:** each step saved by field label and role (not page refs), with
  a page fingerprint. The packet shows the recording and a screenshot; the
  approval hash covers it.
- **Submit:** coded replay of the recording, checking each label exists
  first. Any mismatch stops it as `needs_you` (re-explore needs a new
  approval). The final click only ever happens in replay.
- **Learn:** recordings are kept per system/site; the next job on the same
  system replays with new values and the agent only handles new questions.
- Local first; on the app store once the hosted browser works.

Done when: iCIMS-like and custom-form copies on the demo site are explored,
approved and replayed, and a changed form stops safely.

### A3. Company research

Only for jobs being prepared that need a cover letter or a "why us" answer.

- **Tools:** `web_search`, `fetch_page` (visible text only), `read_job`,
  `save_fact(fact, url, quote)`, `finish`.
- **Truth rule, extended to the company:** code keeps a fact only if its
  quote appears in a page the agent fetched. Letters and "why us" answers may
  use only kept facts. At most about 8 fetches per job.
- **Open:** which search API (a key for Brave or Tavily locally; the app store
  needs one too).

Done when: each letter's company claims link to a kept fact, and a planted
false claim on a demo page is not used.

## Changes to existing code

- `SUBMIT_MODE` stays, still `review` by default. `auto` gains the
  guardrails above and is controlled from Settings.
- The single search-then-apply run becomes prepare → approve → submit
  stages, with packets saved in `data/` and the states above.
- The final click moves from the agent's `submit_application` tool into the
  coded submit step.
- The terminal remains for setup and scripting. The dashboard becomes the
  main interface.
- The browsing-agent `run` flow, `sites.yaml` and `skills/` were removed
  (2026-10-15); the master list and company feeds are the sources. A2
  brings browser tools back for unsupported sites only.

## Open items

- Test prepare with real (free) models: fit scores, tailoring and answers.
- First real Workday application: watch it (account creation, verification).
- Workday "Add" sections (work experience, education) when a site requires them.
- Verify Meta's and Amazon's limits from official sources when possible.

## Sources

- [SimplifyJobs repositories](https://github.com/orgs/SimplifyJobs/repositories)
- [Google Careers Help: Apply for a job](https://support.google.com/googlecareers/answer/6095391?hl=en)
- [Blind: Amazon application limit](https://www.teamblind.com/post/Amazon-application-limit-3HQQkpTi)
- [Blind: Meta cooldown and max applications](https://www.teamblind.com/post/meta-cooldown-max-of-applications-ijpodi4v)
- [Greenhouse Job Board API](https://docs.greenhouse.io/job-board.html)
- [Lever API guide 2026](https://100hires.com/lever-api.html)
- [Ashby Jobs API](https://jobspipe.dev/sources/ashby.md)
- [Workday's hidden JSON API](https://dev.to/udaninn/workday-job-boards-have-a-json-api-too-its-just-better-hidden-23fl)
- [Design Patterns for Securing LLM Agents against Prompt Injections](https://arxiv.org/pdf/2506.08837)
- [OpenRouter usage accounting](https://openrouter.ai/docs/guides/administration/usage-accounting)
- [Nearly 1 in 5 job ads may be ghost jobs](https://allwork.space/2026/08/nearly-1-in-5-job-ads-may-be-fake-ghost-jobs/)
- [Interview Guys: auto-apply math and referral data](https://blog.theinterviewguys.com/?p=18575)
- [HeroHunt: AI application flood playbook](https://www.herohunt.ai/blog/surviving-the-ai-application-flood-2026-playbook/)
