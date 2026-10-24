# Merge eval: how resume merging was rebuilt (2026-10-24)

Uploading several resume PDFs builds one master resume; every tailored resume,
letter and answer is drawn from it. A real 1-page and 2-page pair showed the old
merge losing a professional summary and an advisor's name, and keeping three
accomplishments twice in different words. This is the record of rebuilding it
against [merge-guide.md](merge-guide.md): method, results, what changed, and
what's left.

## Method

Five made-up candidates ([evals/merge/cases.yaml](../evals/merge/cases.yaml)),
each with several resume versions and an answer key, plus one real pair (the
author's own 1-page and 2-page resumes, graded the same way; never committed):

| Case | Versions | What it tests |
|---|---|---|
| Priya | 1-page and 2-page cut, same dates | Paraphrased bullets with the same numbers; different skill groupings; two summaries; an advisor in a heading |
| Marcus | 2024 and 2026 resume | New role, promotion at the same company, "Present" becoming an end date, a role and project only in the old one, a changed phone, a GPA only in the old one |
| Sofia | Three versions tailored to different jobs | Different section names, bullets reworded three ways, sections only one version has, flat vs grouped skills |
| Tom | Career changer, two templates | Look-alike bullets that must stay apart ("Taught algebra…" / "Taught geometry…"), plain-line roles, volunteer work |
| Aiko | A re-upload over a master edited by hand | The hand-added bullet survives; a new role; dates updated |
| Real | A 1-page and 2-page cut of one resume, same dates | Everything above, at full size (11 sections, 8 publications) |

Each version is rendered to a PDF by one of two layouts (the app's LaTeX, and
HTML with spaced-out headings and dates on the role line), so text extraction
is as messy as real PDFs (icon glyphs, "R E S E A R C H", wrapped bullets). Then
the app's own pipeline runs: PDF text, the model's Markdown conversion with its
checks, and the merge. [scripts/merge_eval.py](../scripts/merge_eval.py) grades
the merged master against the key: every fact once (each version's wording
counts), the fuller wording kept, conflicts resolved to the newest, each skill
once, summaries first, no empty or repeated sections, no extraction artifacts.
Then each merged resume was read closely.

## Results

| | Old merge | New merge |
|---|---|---|
| Synthetic cases, Sonnet | 115/124 | **124/124** (3 runs of 3) |
| Synthetic cases, Haiku | 115/124 | **124/124** (3 runs of 3) |
| Synthetic cases, no model | (same merge) | 120/124, nothing lost |
| Real pair, Sonnet | summary lost, 3 repeats | **60/60** (3 runs of 3) |
| Real pair, Haiku | — | **60/60** (3 runs of 3) |
| Real pair re-uploaded over its old master | old repeats kept | **60/60**, old repeats gone |

The old merge's failures, all from comparing words:
- A 4-word role heading "covered" a 30-word professional summary, which was dropped.
- Paraphrases were kept twice ("Built the evaluation harness…" / "Created an offline evaluation harness…").
- Different lines were merged: "Taught geometry…" was dropped as a repeat of "Taught algebra…".
- A whole skills line was judged covered, losing skills only it had (Git, Excel); others appeared twice.
- Only the base version's contact line was kept (a LinkedIn only an older version had was lost).
- With equal dates the condensed 1-page could become the base, and its short lines "covered" the 2-page's details.
- "Machine Learning Intern — Northwind" matched "Machine Learning Research Intern — Contoso Labs" (three shared words) and vanished into it.

Close reading found what the key didn't, and each became a rule:
- Two wordings of one accomplishment where each has a fact the other lacks
  ("…across 120 regions" / "…20K+ reports a quarter, 4 report types").
  Both are kept and listed as a possible repeat.
- A model grouping that chained three lines (A like B, B like C) compared a line
  with the wrong partner. Each group is now settled on its own.
- A 1-page bullet that condensed two 2-page bullets was kept beside them. It is
  dropped when the lines it was judged alike with say all of it, but for a verb.

## Live check on the class platform's model

The real pair was uploaded to the hosted app on DeepSeek V4.1 Flash. Nothing was
lost, but its output showed what a weaker model leaves for code to do:
- Its conversion kept the PDF's kerning splits ("T owards", "PyT orch", "Y eo"),
  spaces before commas ("first author , and"), an email written as
  "https://name@…", and a link label ("Scholar") beside its address. Merging
  then kept the broken wording of a paper title as the "fuller" one.
- It didn't group two wordings of one pipeline that share "…used by 4 analytics teams through
  a shared API".

A second live run converted the same PDFs differently: schools as plain lines
with no headings (so the older version's headed schools were added again),
every paper link in the contact line, a sentence wrapped onto the next line, and
section names in capitals. Also handled in code now: an older entry already said
in its section in another shape isn't added, paper links leave the contact line
(listed in the changes), wrapped sentences are joined and capitalized section
names are title-cased.

All are now handled in code: the PDF's text is repaired before the model sees it
and again after (`rejoin`), a labeled part of the contact line ("Work
Authorization: …") becomes its own line, a bare link label goes, and two lines
from different versions sharing a six-word stretch are compared as a possible
repeat. Re-merging DeepSeek's own conversions locally:

| DeepSeek's conversions, merged by | Before | After |
|---|---|---|
| code only | 53/60 | 59/60 |
| Haiku | 56/60 | **60/60** |
| Sonnet | 56/60 | **60/60** |

The synthetic set is unchanged (124/124 Haiku and Sonnet, 120/124 code only);
the real pair is 60/60 on Sonnet and on Haiku in 4 of 5 runs (once it kept two
wordings of one feature).

## What changed in the app

- **merge.py** (new): code does the structure; the model only groups line ids.
  - Contact details from every version; the newest wins for one of a kind (phone, email, LinkedIn).
  - Roles and schools matched to the best-fitting one by heading, the line under it and start date.
  - Summaries first; a summary from another version is kept as its own section.
  - A line from a section no other version has goes to the section it fits.
  - Skills merged item by item ("orchestration (LangGraph, LangChain)" lists LangGraph).
  - Exact repeats, updated numbers ("F1 by 9%" → "12%") and lines wholly inside another, in code.
  - The model sees numbered lines per role and returns which say the same thing. It can't write a word; unknown or cross-role ids are ignored.
  - The fuller wording is kept; a wording with a number or named thing the kept one lacks is kept too and shown.
  - Without a model (or if it fails), code still catches repeats that share their numbers and keeps everything else.
- **Conversion** (resumes.py): words the PDF has but the Markdown left out (an
  advisor's name) are caught and retried; split words ("RoBERT a") are rejoined;
  a plain "Role, Org" line followed by dates is read as a role.
- **Review**: possible repeats are shown first, then lines only an older version
  had, then the repeats combined (what was kept and what was left out).
- One model call per merge (a few seconds), task `resume_merge`.

## What's left

- Of two wordings of one accomplishment, the one kept may drop a descriptive
  detail without a number or name ("with a fallback for expired
  sources"). The review lists every line left out, so it can be restored.
- Sections take the newest version's names ("Work Experience" joins
  "Experience"), except summaries: a "Research Focus" and a "Professional
  Summary" are different framings, so both stay, each as written.
- Two wordings with no shared number or long stretch ("Built Recheck, which re-verified…" /
  "added on-demand checks of each claim…") are only caught by the model; if it
  misses them, both stay.
- DeepSeek's grouping can only be checked live (no key locally); its
  conversions are re-merged locally as above.
