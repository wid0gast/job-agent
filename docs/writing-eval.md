# Writing eval: how the prompts were tuned (2026-10-21)

The resume, cover letter and answer prompts were rewritten against
[writing-guide.md](writing-guide.md) and tested on real postings. This is the
record: method, what failed, what changed, and what's left.

## Method

Five made-up candidates, each matched to a real public posting (Greenhouse API,
fetched 2026-10-21). They were picked to differ in career stage, field, and the
questions the form asks:

| Candidate | Background | Posting | What it tests |
|---|---|---|---|
| Maya | Statistics undergrad, one internship | Perpay, Analytics Internship | Letter + 4 essays; student resume (GPA, coursework) |
| Arjun | NLP PhD, 6 papers, F-1 visa | Anthropic, Research Scientist, Interpretability | 5 long essays ("often 200-400 words"); AI-use policy on the form; publications |
| Lena | Teacher moving into data | DRW, Data Analyst, Business Intelligence | Career-change letter; writing sample to match |
| Tom | Backend engineer, 8 years | Fin, AI Infrastructure Engineer | Senior letter; "which of our values" question |
| Sofia | MS student, international | Bracebridge, Machine Learning Analyst | 4 essays incl. "a decision with incomplete information"; "answers are your own" |

Each run used the app's own code (prepare.tailored_resume, write_letter,
answers.ask_model) with Claude Opus 5.5. Company research ran once per job
and was reused, so rounds differ only in the writing. Grading:
- code checks (lint for the guide's banned words and shapes, word counts, and
  PDF text extracted with pdftotext, pdfminer and pypdf);
- a close read of every document against the guide's checklist.

## Results

| | Baseline (old prompts) | Round 2 (new prompts) |
|---|---|---|
| Resume PDF text | Broken in all 5: "ﬁ" ligatures, icon glyphs, "E D U C AT I O N" | Clean in all three extractors (icons still show as one stray character in pdfminer) |
| Resume summary | Added to 3 of the 4 that had none; one in first person | Only where the master has one |
| Resume content | First drafts used 49-78% of the page, so every run needed a retry. The student's GPA and coursework were dropped; some bullets copied the posting ("Acquired and transformed data in SQL") | All relevant material kept; GPA and coursework kept; bullets in the candidate's words and tense; no wasted retry |
| Letter opening | 3 of 4 "I'm applying for…"; one led with a weakness | All 4 open with a specific link between the candidate and the job |
| Letter body | Resume restated in prose | One story told in depth, tied to a named need in the posting |
| Essays | Capped at "2-5 sentences": Anthropic's 200-400-word questions got 136 and 185 words | Lengths follow the form (264 and 302 words) |
| Company facts | Didn't reach "Why Bracebridge?" (matching bug); "Which Fin value" left blank | Research reads what the questions name (Fin's values page) and reaches every company question |
| Stories | The same story in the letter and two answers | Each essay its own story where the material has one |
| Added facts | Durations and reasoning not in the material ("several weeks", options weighed) | Not seen in the final round |
| Model calls per packet | 4-5 | 2-3 (no page-fill retry when the master is used up) |

Example opening, Maya's letter:
- **Before:** "I'm applying for the Analytics Internship for Summer 2027. I study
  Statistics and Economics at Penn…"
- **After:** "Last summer at Brightline Credit Union, the lending team asked me why
  first-payment misses had gone up. Perpay Marketplace is built to promote
  healthy repayment behavior, so that question is why the Analytics Internship
  caught my eye."

## What changed in the app

- **Prompts** (prompts.py):
  - A shared VOICE section: the guide's banned words, shapes and punctuation,
    and "could another applicant send this sentence?".
  - TAILOR_PROMPT: selection over rewriting, the master's wording and tense, no
    summary unless the master has one, section order by career stage, and
    bolding only when the master's own style bolds.
  - COVER_LETTER_PROMPT: purpose, shape, opener, one story in depth, a company
    reason, and how to handle gaps.
  - ANSWERS_PROMPT: lengths from the form, guidance per question type, one
    story per answer, nothing beyond the material.
  - RESEARCH_PROMPT: looks up what the application questions name.
- **Code checks**:
  - style.py flags stock words, openers and shapes. A flagged letter or essay is
    rewritten once, and anything left is shown in the packet.
  - Essay length limits are read from the question.
  - Bullets reworded beyond the master's words are listed for review.
  - Letter numbers are checked against the story bank too.
  - A stated start date is no longer thrown away.
- **AI-use policy**: when a posting or form limits AI help, essays are still
  drafted (your choice) but flagged, and the packet notes the policy.
- **PDF**:
  - "fi"/"fl"/"ff" are no longer merged into one character.
  - Letter-spaced headings use tighter spacing, so they extract as words.
  - Icons are tagged as having no text.
  - Projects without an organization get no trailing comma.
  - Education shows its GPA and coursework lines.

## Outreach messages

Tested afterwards on four made-up contacts: a Penn alum, a recruiter, a paper
author on the team and a hiring manager.

- **Before:** short and polite, but it invented effort ("Scaling
  Monosemanticity is a paper I've spent a lot of time with"), asked vague
  questions ("about your experience on the team"), and opened follow-ups with
  "I'm writing again about my note from last week".
- **After:** the prompt follows the same voice rules. Each message says why
  this person, asks one thing their page shows they can answer, and claims no
  effort or familiarity the material doesn't state. Follow-ups restate the
  question or add one new real detail. The same style checks run on every
  draft, and one rewrite happens when they flag something.

## Known limits

- One model (Opus 5.5) and five postings. The checks catch mechanical
  problems; judgment problems (a small inference stated as fact) still need
  your read before approving.
- Icons in the contact line still extract as one stray character in pdfminer.
  The email and phone themselves are intact.
