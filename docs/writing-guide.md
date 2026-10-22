# Writing guide: what the agent writes for employers

The rules every generated document follows, and the checklist used to grade
them. Built from career-office guides, hiring-manager advice, field
experiments and studies of AI-written text (sources at the end). Where the
evidence is weak or sources disagree, it says so.

Five buckets: **A** resume content, **B** resume format and ATS, **C** cover
letter, **D** application answers and essays, **E** voice (applies to all).
**F** covers the packet as a whole.

---

## What the evidence says, in brief

- **A person decides, after a fast skim.** Recruiters spend about 7 seconds on
  a first pass (Ladders eye-tracking, 2018), mostly on titles, employers and
  dates. Clean layouts, bold titles and bulleted results did best; clutter,
  columns and long sentences did worst.
- **ATS is a database plus search, not usually an auto-rejecter.** Recruiters
  search and filter by keyword (Greenhouse has full-text resume search; Jobscan
  reports 99.7% of recruiters use filters). Evidence of automatic rejection by
  keyword score is thin; the "75% rejected by ATS" figure traces to a 2012
  sales pitch. Knock-out questions do reject automatically. So: the posting's
  real terms must appear *as extractable text*, and a human must like what
  they read.
- **Tailoring pays; generic letters barely do.** In ResumeGo's field
  experiment (7,287 applications), callbacks were 10.7% with no letter, 12.5%
  with a generic letter, and 16.4% with a tailored one: most of the gain comes
  from tailoring.
- **AI text has lost its signal; specifics and editing still carry it.** After an
  AI cover-letter tool launched on a large freelance platform, letters matched
  postings better but the link between that match and callbacks fell 51%.
  Employers moved to signals AI can't fake, like work history (Cui, Dias & Ye,
  2025). More time spent editing the AI draft still predicted hiring. Writing
  help on resumes raised hires 8% in a field experiment (Wiles, Munyikwa &
  Horton, *Management Science* 2025). Help is fine; sameness is the problem.
- **Generic AI style gets noticed and penalized.** Surveys of hiring managers:
  about 1 in 5 would reject an application that looks fully AI-written
  (TopResume 2025, n=600); 62% would reject AI resumes that lack
  personalization (Resume Now 2025). These come from resume vendors, so the
  exact figures are soft, but the direction is consistent. The tells are
  clusters of stock words and uniform structure, not any single word.
- **Some employers restrict AI help.** Anthropic asked applicants not to use AI
  (early 2025), then changed to "write your own first draft, then polish with
  AI". Other forms carry similar clauses. When a form says so, the agent must
  not write that answer.

---

## A. Resume content

**Truth and source**
1. Every claim traces to the master resume. No new skills, tools, numbers,
   dates, titles, scope or outcomes. Rewording may not raise the claim
   ("helped with" never becomes "led").
2. Prefer the candidate's own sentence. Edit it only to (a) use the posting's
   term for something the candidate truly did ("LLMs" → "large language
   models (LLMs)" if the posting says that), (b) tighten, or (c) bring the
   relevant half of a bullet first. Rewriting everything into one house style
   is what makes resumes sound alike.

**Selection and order: the main lever**
3. Reorder before rewording (career-office consensus). The most relevant
   entries and bullets come first in each section. Bullets that map to the
   posting's repeated requirements (named more than once) come first.
4. Cut what doesn't help this job; never cut education. A role that stays
   keeps at least one bullet.
5. Section order follows the candidate's strongest evidence for this job:
   - Students and new grads: Education near the top.
   - Industry roles: Experience before Projects, unless projects are the
     stronger evidence.
   - Research roles: Research Experience and Publications high.
   - Engineering, product or data roles: publications low, as "Selected
     Publications" with 3–6 of the most relevant.

**Bullets**
6. One line, two at most. Start with a strong, specific past-tense verb (present
   tense for a current role). No "I", no "responsible for", no "helped" when the
   candidate did it. Vary the opening verbs.
7. Each bullet says what was done, on what, and what came of it. That's
   Bock's "accomplished X as measured by Y by doing Z" in spirit, not as a
   template. Numbers only from the master; a bullet without one is fine. Not
   every bullet needs a metric, and uniform "…, resulting in X%" endings read as
   AI.
8. Show scope and ownership honestly (team size, users, data size, what the
   candidate owned) when the master states it. Hiring managers read for the
   trajectory and for "what did you actually do".
9. Bold at most one short phrase per bullet, and only if the master's own style
   bolds; never whole clauses.

**Summary**
10. No summary by default. Add one only when the master has one or the
    candidate is changing fields. Then: 1–2 lines, specific to this role and
    supported by the content below, no adjectives about character. A summary
    that restates the education line and the bullets wastes the top of the page.

**Skills**
11. Grouped by category (3–5 lines). Within each line, the posting's terms
    first. Only skills the master lists; no ratings; each skill once.
    Spell out an acronym once if the posting does.

**Publications (when present)**
12. One line each: authors (candidate bolded), title, venue, year, as in the
    master. For research roles, list them all, or up to about 8 most relevant.
    For other roles, 3–6 "Selected Publications", or none for roles where they
    don't count.

**Length**
13. One page for students and early career; two only when the candidate set
    two and has the material. Fill the page; don't pad. Reviewers rate fuller
    resumes higher only when the content is substantive (ResumeGo 2018,
    vendor study).

## B. Resume format and ATS

14. Single column, standard headings (Education, Experience, Research
    Experience, Projects, Publications, Skills), no tables for content, nothing
    in headers or footers.
15. **Text must extract cleanly.** Checked by pulling the text back out of the
    PDF:
    - no ligature characters (ﬁ, ﬂ);
    - no icon glyphs standing in for contact details;
    - no letter-spaced headings ("E D U C A T I O N");
    - every skill word findable.
16. Dates in one format everywhere, month and year (e.g., "Jun 2025 –
    Aug 2025", "Present"). Avoid "Summer 2025" alone.
17. Contact line as plain text: email, phone, location, LinkedIn, GitHub or site.
    Work authorization only if the master states it.

## C. Cover letter

**Purpose:** say what the resume can't. That means why this role and this
company, and one or two experiences in more depth, connected to what the job
needs. Ask a Manager's most common failure is a resume summary in prose;
MIT's guide says the same. HBR's three failures are a restated resume, a
form letter, and an overly personal story.

18. 200–320 words (fits in under a minute); 3–4 short paragraphs.
19. **Opening:** no "I am writing to apply/express my interest". The first
    sentence carries the most weight. Lead with the specific link between the
    candidate and this job: a thing they did that the job needs, or a concrete
    reason this team's work matters to them.
20. **Body:** one or two experiences told with detail the resume lacks:
    - the problem, what the candidate decided and did, what happened;
    - then connect them to a named need in the posting.
    Depth over coverage: no lists of every skill.
21. **Company:** at least one specific, true reason for this company or team.
    It comes from the posting or verified company facts. Never praise of the
    "innovative leader" kind, and never a fact that isn't in the material.
22. **Close:** one or two sentences, plain. Interest in talking, and thanks.
    No "I am confident that I would be a valuable asset".
23. Greeting: "Dear <Name>," when the hiring manager or recruiter is known.
    Otherwise "Dear Hiring Manager," or "Dear <Team> team,". A survey found
    "To Whom It May Concern" mostly harmless, but the alternatives are
    better.
24. Same facts as the resume, never contradicting it, and not restating more
    than one or two of its lines.

## D. Application answers and essays

25. Answer the exact question. Its verb sets the task ("describe", "give an
    example", "why"). Cover every part of a multi-part question.
26. Respect length limits: a stated word or character limit is a hard cap.
    Aim for 70–100% of a stated word limit; with no limit, 80–200 words for an
    essay and one sentence for a short text field.
27. **"Why this company/role"**: the strongest material is
    - one or two specific things about the company or team (from facts or
      the posting), each tied to something the candidate has done or wants
      to do next;
    - no flattery and no mission-statement paraphrase.
28. **Behavioral ("tell us about a time")**: use STAR, kept lean. Most of the
    words go on what the candidate did ("I", not "we"), and the result is
    stated plainly. Draw the story from the story bank or resume.
29. **Project or technical questions:** the goal, the candidate's own decisions
    and trade-offs, and the outcome. Concrete nouns and real tool names.
30. **Additional information** ("anything else?"): leave empty unless there's
    something useful that's not elsewhere. Examples: an availability
    constraint the candidate stated, or a portfolio link.
31. Never answer what the material doesn't support: leave it for the
    candidate.
32. **AI-use policy:** if the form or posting says not to use AI for the
    application or for a question, the agent does not draft it. It leaves it
    for the candidate with a short outline of material to draw on (their own
    facts, no prose).
33. Don't reuse one story across answers and the letter. When the same
    story comes up twice, tell it from a different angle.

## E. Voice: sounding like a person (all documents)

Plain, specific, a little informal: how the candidate would write a careful
email. The test: could a different applicant have written this sentence?
If yes, cut it or make it specific.

34. **Words to avoid**, which signal AI in clusters (Wikipedia's "Signs of AI
    writing"; Juzek & Ward 2024; recruiter reports):
    - stock vocabulary: delve, pivotal, crucial, robust, seamless, tapestry,
      testament, landscape, realm, intricate, underscore, showcase, foster,
      leverage/leveraged, spearheaded, synergy, cutting-edge,
      innovative/innovation-driven, dynamic, passionate, thrilled, excited to,
      eager to, align/alignment;
    - phrases: "I am confident", "valuable asset", "unique blend", "proven
      track record", "hit the ground running", "make a meaningful impact",
      "I hope this finds you well".
35. **Structures to avoid:**
    - "not just X, but Y" / "it's not X, it's Y";
    - stacked triplets ("fast, reliable, and scalable" everywhere);
    - trailing "-ing" clauses that add significance ("…, highlighting my
      ability to…");
    - "serves as" and "stands as" in place of "is";
    - sentence-initial "Additionally" or "Moreover";
    - a closing sentence that summarizes the paragraph;
    - every paragraph or bullet the same length or shape.
36. **Punctuation:** no em dashes in prose (use commas, periods or
    parentheses); a hyphen or en dash in date ranges is fine. No exclamation
    marks. No Markdown in prose fields (no ** or #), except where the
    document format uses it.
37. **Specific over abstract:** name the dataset, the system, the course, the
    paper, the tool. Prefer verbs to nouns ("cut the runtime" over "achieved a
    runtime reduction").
38. **Match the candidate's writing sample** when there is one (sentence
    length, formality). Without one: short sentences, first person, contractions
    allowed in answers and outreach, not in the resume.

## F. The packet as a whole

39. Resume, letter and answers state the same facts (dates, degree, titles,
    authorization).
40. Each document has a job: the resume shows evidence, the letter shows fit
    and motive, and the answers respond to their questions. The less they
    repeat each other, the better.

---

## Grading checklist (for evals)

Each item is pass / partial / fail; a fail on a starred item is disqualifying.

**Resume**
- R1* Invents nothing (claims, numbers, tools, scope, inflated verbs).
- R2 The most relevant evidence for this posting is on the page, high up.
- R3 Section order fits the role and career stage.
- R4 Bullets: concise, varied verbs, results where the master has them, no
  uniform template.
- R5 Stays close to the candidate's wording; no stock vocabulary (rule 34).
- R6 Skills: grouped, posting terms first, nothing new, ≤5 lines.
- R7 Summary only when warranted, and specific.
- R8 Publications handled for the role (rule 12).
- R9* Extracted PDF text is clean (rule 15) and fits the page limit.
- R10 The page is filled (≥85% of the last page) without padding.

**Cover letter**
- C1* Invents nothing about the candidate or the company.
- C2 The opening is specific; no stock opener.
- C3 Adds what the resume can't (motive, depth); doesn't recap it.
- C4 At least one true, specific company or team reason.
- C5 Connects the candidate's evidence to named needs in the posting.
- C6 200–320 words; plain close.
- C7 Voice: none of rules 34–36 broken; reads like a person.
- C8 The "another applicant could send this" test: under 2 such sentences.

**Answers**
- Q1* Supported by the material, or left for the candidate.
- Q2 Answers the exact question, all parts.
- Q3 Within the length limit and right-sized.
- Q4 Specific (names, things, own actions); STAR for behavioral.
- Q5 Voice (rules 34–38).
- Q6 No story repeated across answers and the letter.
- Q7* Honors any AI-use restriction.

---

## Sources

Evidence on screening, ATS and tailoring
- Ladders eye-tracking study (2018), summarized by HR Dive:
  https://www.hrdive.com/news/eye-tracking-study-shows-recruiters-look-at-resumes-for-7-seconds/541582/
- Greenhouse: Search resumes for keywords:
  https://support.greenhouse.io/hc/en-us/articles/115004600186-Search-resumes-for-keywords
- Jobscan, ATS keywords and recruiter filtering (vendor):
  https://www.jobscan.co/blog/top-resume-keywords-boost-resume/
- On the "75% rejected" myth: https://relocateme.substack.com/p/an-ats-rejected-my-resume-is-it-true
- ResumeGo cover letter field experiment: https://www.resumego.net/research/cover-letters/
- ResumeGo one vs two pages (2018): https://www.resumego.net/research/one-or-two-page-resumes/
- Cui, Dias & Ye (2025), Signaling in the Age of AI: Evidence from Cover Letters:
  https://arxiv.org/abs/2509.25054
- Wiles, Munyikwa & Horton (2025), Algorithmic writing assistance on jobseekers'
  resumes increases hires, *Management Science*:
  https://john-joseph-horton.com/papers/algorithmic-writing-assistance-on-jobseekers-resumes-increases-hires/

Recruiter views of AI-written material
- TopResume hiring-manager survey (2025): https://topresume.com/career-advice/ai-in-hiring-survey
- Jobscan, Can ATS detect AI resumes: https://www.jobscan.co/blog/can-ats-detect-ai-resume/
- Recruiter AI-resume tells (vendor blog):
  https://www.resumepolished.com/blog/using-ai-for-resumes
- Anthropic's application AI policy and its reversal:
  https://fortune.com/2025/07/21/billion-dollar-giant-anthropic-ai-ban-hiring-policy-change-job-seekers-interview-process
- University of Kentucky, What companies mean by "don't use AI":
  https://pigmancareers.uky.edu/blog/2025/04/16/what-companies-mean-when-they-say-dont-use-ai-in-your-job-applications/

Resume content
- Harvard Office of Career Services resume and cover letter guide:
  https://careerservices.fas.harvard.edu/resources/hes-create-impactful-resumes-and-cover-letters/
- Laszlo Bock's X-Y-Z formula (Government Executive):
  https://govexec.com/management/2016/03/google-just-revealed-incredibly-simple-formula-killer-resumes/126536
- Swizec Teller, What makes your resume stand out: https://swizec.com/blog/what-makes-your-resume-stand-out/
- Formation, Software engineer resume guide:
  https://formation.dev/blog/a-complete-guide-to-a-software-engineer-resume
- University career-center bullet guides:
  - Memphis: https://memphis.edu/professional/docs/cpcd-resume-packet-8-23-2024.pdf
  - Geneseo: https://www.geneseo.edu/sites/default/files/documents/Resume%20Guide%20PDF.pdf
  - Weber State: https://www.weber.edu/wsuimages/goddard/Default_2017/CAREER%20SERVICES/BulletPOintsGuide.pdf
- Skills sections: https://www.techinterview.org/post/3233474612/skills-section-engineering-resume/
- PhD to industry resumes and publications: https://rendercv.com/blog/phd-to-industry-resume
- Summary and objective statements:
  https://simplify.jobs/blog/do-you-need-a-resume-summary

Format and ATS parsing
- Jobscan / Santa Clara University, ATS formatting mistakes:
  https://www.scu.edu/careercenter/toolkit/job-scan-common-ats-resume-formatting-mistakes/
- LaTeX resumes and text extraction (ligatures, icons):
  https://atsverification.com/blog/latex-resume-ats-friendly/
- Verified locally: our own PDFs extracted with pdftotext and pypdf (2026-10-21).

Cover letters
- MIT CAPD, How to write an effective cover letter:
  https://capd.mit.edu/resources/how-to-write-an-effective-cover-letter/
- Ask a Manager, How to write a cover letter that will help you get an interview:
  https://www.askamanager.org/2017/06/how-to-write-a-cover-letter-that-will-help-you-get-an-interview.html
- HBR (Amy Gallo) on cover letters, via Fox Business:
  https://www.foxbusiness.com/features/how-to-write-a-great-cover-letter-necessary-or-not
- The Muse, Opening lines to drop:
  https://themuse.com/advice/5-opening-lines-that-are-straight-up-killing-your-cover-letter
- "To Whom It May Concern" survey (Inomics):
  https://inomics.com/advice/survey-is-to-whom-it-may-concern-acceptable-on-a-cover-letter-1473274

Application answers
- Get My First Job, Answer the question that was actually asked:
  https://core.getmyfirstjob.co.uk/advice/applying/application-form-answers
- Brighton & Hove, Shortlisting questions guidance (STAR):
  https://www.brighton-hove.gov.uk/jobs/open-vacancies-and-how-apply-brighton-hove-city-council/supporting-your-job-application/shortlisting-questions-guidance
- Built In, Why do you want to work here: https://builtin.com/job-interview/why-do-you-want-work-here

Voice
- Wikipedia: Signs of AI writing: https://en.wikipedia.org/wiki/Wikipedia:Signs_of_AI_writing
- Juzek & Ward, Why does ChatGPT "delve" so much?: https://arxiv.org/abs/2412.11385
- Kobak et al., Delving into ChatGPT usage in academic writing through excess
  vocabulary: https://arxiv.org/abs/2406.07016
- Paul Graham, Write Simply: https://paulgraham.com/simply.html
