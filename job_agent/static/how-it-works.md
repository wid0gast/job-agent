## The problem

Applying for jobs is slow and mostly silent. Doing one application properly
(reading the posting, tailoring a resume, writing answers) takes a long time,
and most get no reply. Two things help most: applying to the right
jobs with honest, tailored applications, and talking to someone at the company
(in Lever's data on about 4 million candidates, 1 in 16 referred candidates was
hired, against 1 in 152 who applied through a careers page or posting).

Job Agent does the slow parts and leaves the decisions with you: it finds and
ranks jobs, prepares each application, finds people worth contacting with a
drafted message, and applies only when you approve.

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

Agents run in a small loop written for this app (`harness.py`, about 150
lines): ask the model, run the tools it asks for, send back the results or the
error, repeat until it calls `finish` or hits a step or time limit.

## Decisions, and why

**Why does code submit the application, and not an agent?**
What you approve is exactly what gets sent. Approval records a fingerprint
(hash) of every answer and document; at submit time code checks it, opens the
form, compares it with what you approved (a new required question sends it
back to you), and types your approved answers. No model runs during submit, so
it can't improvise.

**Why one model, with a different effort per step?**
The class platform gives each visit one model, so every step uses it, with
more reasoning effort where it matters: what employers read (resumes,
letters, answers) and the fit judgement that decides whether you apply. The
standalone version picks a model per step (Haiku, Sonnet, Opus). Each job's
"How this was made" page shows the model and effort for every step.

**Why can't the model see the whole job page?**
Pages can hide text from people (white text, 1-pixel fonts) to steer AI tools:
"ignore your instructions and say...". The posting is rendered in a browser
and only text a person can see is passed on. When something was removed, the
trace shows it as a check.

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

**How does it search the web here?**
In code, not by the model: a search API (Brave, with a key) or a free search
engine. The free one refuses queries limited to one site, so the app names
the site in the query and keeps only that site's results. The model only
reads the results it's given.

**Where does my data go?**
On the class platform, everything lives in your session's workspace and is
gone when the session ends; the dashboard offers a download of it all. The
standalone version runs on your own computer.

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
