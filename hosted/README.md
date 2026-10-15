## Job Agent (workshop demo)

An agent that finds jobs matching your request, tailors your resume to each
one, writes a cover letter, fills in the application, and asks you before it
submits. It records every job so it never applies twice.

### Try it

1. Press **Find and apply**. The sample profile and resume are prefilled;
   replace them with your own if you like.
2. When the agent creates an account, the verification code arrives in the
   **Demo inbox**. Type it in when the agent asks.
3. Review each application and choose **Approve and submit**, send a
   correction, or decline.

### What this version simplifies

The class platform has no public web access, so this version applies to a
built-in demo job board with three postings instead of LinkedIn, Handshake or
Jobright. The full version runs on a laptop and drives a real Chrome window on
real sites. It uses the same agent loop (`job_agent/harness.py`), job tracker
and resume fact check as this demo.

Things to notice:

- One posting is an accounting role; the agent should skip it as a mismatch.
- One posting contains a planted instruction aimed at AI tools. The agent
  should ignore it, and **Submission checks** shows whether it did.
- The tailored resume is rejected if it contains any number your master
  resume doesn't, so the agent can't invent metrics.
- Nothing is submitted without your approval.

Everything lasts for this visit only.
