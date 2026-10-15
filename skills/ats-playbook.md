## Applicant tracking system playbook

Identify the system from the URL or page branding, then follow its notes.
After every page, take the snapshot's validation errors seriously: a red
"required" message means the click to continue did not go through.

### General
- Look for "Autofill with resume" / "Apply with resume" options. Upload the
  tailored resume there first, then correct every prefilled field; parsers
  often misplace dates, titles and schools.
- Custom dropdowns (role=combobox) usually need: click the field, type part of
  the answer with browser_type, then click the matching option or press Enter.
- Date fields: try the format shown in the placeholder. If none, MM/DD/YYYY.
- "Are you legally authorized..." and sponsorship questions come from
  work_authorization in the profile. Never guess these.
- If a question has no truthful answer in the profile, ask_user.

### Workday (*.myworkdayjobs.com)
- Requires an account per company tenant. "Create Account" with the profile
  email, browser_fill_password for both password fields, accept the terms
  checkbox. Expect an email verification link: ask_user to click it or paste it.
- Steps: My Information → My Experience → Application Questions → Voluntary
  Disclosures → Self Identify → Review. Use "Save and Continue" between steps.
- My Experience: upload the resume in the "Resume/CV" drop zone first; Workday
  parses it into work history entries. Fix or delete wrong entries.
- "How Did You Hear About Us?" is a nested multi-select prompt: click it,
  choose a category, then an option.

### Greenhouse (boards.greenhouse.io, job-boards.greenhouse.io)
- Single page, no account. Often embedded in an iframe on the company site.
- Resume and cover letter each offer "Attach" (file) or "Enter manually"
  (paste). Prefer attaching the PDF.
- Location fields are autocomplete; type the city and pick a suggestion.

### Lever (jobs.lever.co)
- Single page, no account. "Resume/CV" upload autofills name, email, phone.
- "Additional information" textarea can take a short cover note.

### Ashby (jobs.ashbyhq.com)
- Single page, no account. Upload the resume first; it autofills.
- Yes/No questions are button pairs, not radios. Click the right one.

### iCIMS (*.icims.com) and Taleo (*.taleo.net)
- Account required, many pages, slow loads. Expect iframes.
- If a page seems empty, take another snapshot after a moment; content
  inside frames loads late.

### LinkedIn Easy Apply
- A modal with several steps: contact info, resume choice, questions, review.
- Choose "Upload resume" and attach the tailored PDF rather than an old one.
- The last step's button is "Submit application": use submit_application.
- Uncheck "Follow company" only if the user's profile says so.

### Handshake
- "Apply" may open an in-app form (resume selection, documents) or an
  external link. In-app: upload the tailored resume as a new document.
