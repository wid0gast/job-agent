## Job Agent: platform probe

A temporary app that checks what the class platform supports, so the full
Job Agent can be set up to match: Python packages, headless Chromium, access
to job sites, the class models' options, disk and memory, and whether a LaTeX
engine can be downloaded.

### Use it

1. Press **Run checks** and wait until it says Idle.
2. Optionally press **Try pip install**, then **Try Chromium download**, then
   **Try Tectonic (LaTeX)**, one at a time. Each can take a few minutes.
3. Press **Copy results** and send them to the developer.

It makes a few tiny model requests and shows only the names of environment
variables, never their values.
