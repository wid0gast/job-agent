## The problem

Applying for jobs is slow and mostly silent. Doing one application properly
(reading the posting, tailoring a resume, writing answers) takes a long time,
and most get no reply. Two things help most: applying to the right
jobs with honest, tailored applications, and talking to someone at the company
(in Lever's 2016 study of about 4 million candidates, 1 in 16 referred candidates was
hired, against 1 in 152 who applied through a careers page or posting).

It's getting worse because of tools like this one. LinkedIn gets about
11,000 applications a minute, up 45% in a year; on Handshake, applications per
job rose 26% while postings fell. Most AI job tools make it worse: they raise
the *number* of applications (one auto-apply service sells 4,500 a month), and
hiring managers answer with filters. About half now count heavily
AI-written applications as a red flag, and Greenhouse's CEO calls it a
"doom loop".

Job Agent aims at the *odds* of each application instead of the count. It
does the slow parts and leaves the decisions with you: it finds and ranks
jobs, says how to apply to each (through a person, directly, or not at all),
prepares each application and has a hiring manager's eye read it, finds
people worth contacting with a drafted message, and applies only when you
approve.

## Agent or code? The rule it follows

Anthropic's guidance ("Building effective agents") is to use the simplest
thing that works: fixed code and single model calls where the path is known,
and an agent (a model choosing its own steps with tools) only where it isn't.
So:

| An agent decides the steps | Fixed code decides the steps |
| --- | --- |
| Filling an unfamiliar application form (every site is different) | Submitting: replaying exactly what you approved |
| Researching the company (which pages are worth reading) | Filters, caps, the fit threshold |
| Finding people to contact (where they might be mentioned) | Answers already in your profile |
| The assistant chat (what you'll ask is open-ended) | Every check that guards the output |

Agents run in a small loop written for this app (`harness.py`, about 200
lines): ask the model, check every tool call's arguments against the tool's
schema before any of them runs, run the valid ones, send back the results or
the errors, and repeat until it calls `finish`, hits a step or time limit, or
you press Stop (it ends at the next step; a submit can't be stopped once
started, since the click may already have happened).

## Decisions, and why

**Why does code submit the application, and not an agent?**
What you approve is exactly what gets sent. Approval records a fingerprint
(hash) of every answer and document; at submit time code checks it, opens the
form, compares it with what you approved (a new required question sends it
back to you), and types your approved answers. No model runs during submit, so
it can't improvise.

**Why does it say how to apply, and not just apply?**
Because the route changes the odds more than speed does. A referral was about
9 times as likely to lead to a hire in Lever's 2016 data. About 1 in 5 postings show no hiring
activity, and roles open for months or posted again are the clearest sign.
A second application where you just applied for the same role mostly reaches
the same people. So every job gets one of **Reach out first**, **Apply** or
**Think twice**, with the reasons. It's decided in code from data you can
check (posting dates, earlier listings of the role in the same place, your
applications, the people found and what you've sent them), with no model
call, so it's the same every time and never guesses. "Prepare top matches"
passes over Think twice jobs and says which.

**Why have a "hiring manager" read the application?**
The first pass is quick (recruiters give a resume 7.4 seconds on average
before keeping or dropping it, in Ladders' 2018 eye-tracking study), and the
writer can't judge its own work. So a different model (Sonnet; Opus
wrote it) reads the finished packet as the hiring manager on a quick first
pass, about 30 seconds for the whole packet: which pile it
lands in, what they'd remember, what they looked for and didn't find, and up
to three lines that hurt it, each with a fix that uses only your real
material. Code checks every quoted line is really in your application and
drops any that isn't. It changes nothing by itself: you decide what to fix.

**Why a different model for different tasks?**
Cost and quality where they matter: Haiku ranks hundreds of job titles,
Sonnet reads and searches, and Opus writes what employers read (resumes,
letters, answers) and makes the fit judgement that decides whether you apply.
Each job's "How this was made" page shows which model did each step.

**Why can't the model see the whole job page?**
Pages can hide text from people (white text, 1-pixel fonts) to steer AI tools:
"ignore your instructions and say...". The posting is rendered in a browser
and only text a person can see is passed on. When something was removed, the
trace shows it as a check.

**What if the page it reads isn't the posting?**
Then it stops before asking the model anything. A fit score from a sign-in
wall or a privacy notice would be a confident guess. Checks in code: at least
80 words a person can see, and on career sites read in the browser, the job's
title on the page (frames included: iCIMS puts the posting inside one, and the
page around it was once 7,000 words of privacy policy). Testing the reader on
about 100 real postings across 14 job sites found both of those problems.

**How do you know it doesn't make things up?**
Rules in code, not trust in the model: a tailored resume containing a number
your master resume doesn't is sent back once, then replaced by your own
resume. Company facts are kept only if their quote is word for word on a page
it read. Short answers about you (dates, work authorization, links) must come
from your profile or your own words. Answers that contradict your profile
block approval.

**Why must I confirm every submit, even in the chat?**
Applying is your decision and can't be undone. The assistant can only put a
confirmation card in the chat; nothing is approved or sent until you click.

**Why doesn't it solve CAPTCHAs?**
That's the line between helping you and evading a site's bot checks. It
hands CAPTCHAs and emailed codes to you.

**Why limits per company and per day?**
Quality over volume: applying to ten roles at one company looks careless, and
a daily cap keeps every application one you'd stand behind.

**Why does outreach ask for advice instead of a referral?**
That's what career advisers recommend (Steve Dalton's *The 2-Hour Job Search*,
university career centers): under 100 words, what you share first, one
question, no attachments, one person per company at a time, one follow-up
after a week. Someone who shares your school or past employer is the
likeliest to reply. A referral tends to come out of the conversation.

**Why does it never use LinkedIn directly?**
LinkedIn's terms forbid scraping and automated messages. People are found
through ordinary web search results; you send the LinkedIn note yourself.

**Why do emails open in my own Gmail?**
No passwords are stored, you're the sender, and replies come to you. The app
records what you sent and reminds you when to follow up.

**Why Claude's web search instead of a free search engine?**
The free search tried first (DuckDuckGo) started refusing automated queries,
especially ones limited to one site. Claude's web search works on your plan
and can be limited to a site (for example linkedin.com or the company's blog).

**Why does it run on my computer?**
Your resume, answers and job history stay with you, and applications go out
from your own browser, signed in as you.

**Why keep a record of every run?**
So you can check its work. Every model request (what was sent, what came
back), every agent turn and every check in code is recorded per job; the
"How this was made" page shows it as a timeline.

## See it for yourself

- On any prepared job, **How this was made** shows each step, the model
  requests (with "Show prompts and replies") and every check, passed or
  caught.
- **Activity** lists recent runs across jobs.
- Agent steps link to every turn: the model's reasoning, each tool call and
  its result.
