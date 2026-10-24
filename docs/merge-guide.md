# Merge guide: what a master resume built from several versions should be

The app builds one master resume from every resume PDF you upload (and your
current master, so edits made by hand survive). Every tailored resume, letter
and answer is drawn from it, so its mistakes repeat in every application. This
is the standard the merge is held to, and the checklist its eval grades against
([merge-eval.md](merge-eval.md)).

## What the evidence says, in brief

- **A master resume is a complete private archive.** Career centers describe it
  as the record of everything, with no page limit, from which each tailored
  resume is cut: list every job, internship and volunteer role and never delete
  one, since an old role may matter for a future job (Drexel, Northwestern,
  University of Southern Maine). So a merge loses nothing.
- **One bullet per accomplishment.** The same advice keeps one bullet for each
  accomplishment or responsibility (Drexel). Two wordings of one result are a
  duplicate, and in a master they do harm: tailoring picks bullets from it, so a
  tailored resume can say the same thing twice.
- **Keep the numbers.** Quantified outcomes are what a tailored resume needs
  most (Rezi, Northwestern); when two wordings of one result differ, the master
  keeps the one that says more.
- **Exact matching first, judgment second.** Systems that merge candidate
  records match on exact fields first and use probabilistic (AI-assisted)
  matching only for near-duplicates; paraphrases ("boosted sales by 20%",
  "increased revenue by one-fifth") can't be found by comparing words, and
  generic phrases make word overlap a poor signal on its own.
- **There is no public benchmark** for merging resume versions, so the eval
  uses its own answer keys (as the writing eval did).
- **Order:** contact details, then a summary, then the main sections; students
  lead with education, experienced candidates with experience. A merge keeps
  the order of your fullest, newest version and puts summaries first.

## Rules

1. **Nothing lost.** Every role, degree, publication, award, skill, link and
   contact detail in any version is in the master.
2. **Nothing twice.** Each accomplishment, skill, publication and section
   appears once, however differently the versions word it.
3. **Nothing invented.** Every line is a line from a version (formatting aside).
   The merge selects; it never writes.
4. **The fuller wording wins.** Of two wordings of one accomplishment, keep the
   one with more facts (numbers, names, scope); if neither says more, the newest.
   A wording with a fact the kept one lacks is not dropped.
5. **Newest wins conflicts.** Dates ("Present" → an end date), titles, contact
   details and work authorization come from the newest version that states them.
6. **Distinct things stay distinct.** Two roles at different organizations, or
   two bullets about different work that share words ("Taught algebra…",
   "Taught geometry…"), are not merged.
7. **Readable structure.** Summaries first; then the newest fullest version's
   sections in its order; a role found only in an older version goes into the
   matching section in date order; one skills section with each skill once.
8. **Shown, not hidden.** The review lists what was combined and what came only
   from older versions, so you can check it before accepting.

## Grading checklist (for evals)

For each case, an answer key lists every fact (with each version's wording) and
the expected result:

- [ ] Completeness: every fact in the key appears.
- [ ] No repeats: every fact appears exactly once, every skill once.
- [ ] Fuller wording: where wordings differ in detail, the fuller one is kept.
- [ ] Distinct kept apart: every "looks alike but different" pair is both present.
- [ ] Conflicts: the newest value is present and the older one isn't.
- [ ] No invention: no word or number that's in no version.
- [ ] Order: summaries first; sections in the base version's order; no empty sections.
- [ ] Clean text: no extraction artifacts (split words, spaced-out letters).
- [ ] Review: combined and kept lines are listed.

## Sources

- Drexel University, "The master resume":
  https://drexel.edu/graduatecollege/professional-development/blog/2016/july/master-resume
- Northwestern University, PhD and postdoc careers, resumes:
  https://www.northwestern.edu/phd-postdoc-careers/your-job-search/developing-materials/resume.html
- University of Southern Maine, Resume resource guide (master and tailored):
  https://usm.maine.edu/career-employment-hub/wp-content/uploads/sites/241/2025/08/Resume_Resource-Guide_Master_Tailored_Final_Su2025.pdf
- Rezi, "Master resume": https://www.rezi.ai/posts/master-resume
- Zoho Recruit, resume parsing and duplicate prevention: https://prezohoweb.zoho.com/recruit/parse-resumes.html
- RecruitBPM, resume parsers (merging updated resumes): https://recruitbpm.com/blog/resume-parser-recruiters
- Zety, resume section order: https://zety.com/blog/resume-order
