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
- The models tested were Sonnet and Haiku locally; the class platform's models
  are checked live after each release.
