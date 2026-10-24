"""Your profile's facts come from your master resume, not from typing them twice.

When a master resume is accepted, name, contact details, links, education and
work authorization are read from it into profile/resume-profile.yaml (written
by the agent, never by hand). profile.yaml holds only what a resume doesn't
say (preferences, demographic answers, standard answers) and can override any
field if you ever need to.

Every value must appear in the resume text, or it is dropped. Work
authorization is derived in code from the status the resume states, by fixed
rules, and the rule used is recorded.
"""

import re
from datetime import date

import yaml

from . import config

PATH_NAME = "resume-profile.yaml"
URL = re.compile(r"https?://[^\s·|,)]+")

PROMPT = """
You read a resume and copy out facts for job application forms. Copy values
exactly as they appear (you may fix ALL-CAPS names to normal capitalization).
Use "" for anything the resume doesn't state; never guess. Education entries
most recent first. degree: the degree as written (e.g. "Master of Science").
major: the field (e.g. "Statistics"). start/graduation: month and year as
written (e.g. "May 2027"), or "" if absent. work_authorization_text: the
resume's own sentence about visa or work status, or "".
""".strip()

SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "email": {"type": "string"},
        "phone": {"type": "string"},
        "location": {"type": "string"},
        "work_authorization_text": {"type": "string"},
        "education": {"type": "array", "items": {
            "type": "object",
            "properties": {k: {"type": "string"} for k in
                           ("school", "location", "degree", "major", "start", "graduation", "gpa")},
            "required": ["school", "location", "degree", "major", "start", "graduation", "gpa"],
            "additionalProperties": False}},
    },
    "required": ["name", "email", "phone", "location", "work_authorization_text", "education"],
    "additionalProperties": False,
}

# Status stated on a resume → how application questions are answered.
# (pattern, authorized now, needs sponsorship now, needs sponsorship later, rule)
STATUS_RULES = [
    (r"u\.?s\.?a?\.? citizen|american citizen|citizen of the united states|united states citizen",
     True, False, False, "U.S. citizen"),
    (r"green card|permanent resident", True, False, False, "U.S. permanent resident"),
    (r"\b(f-?1|cpt|opt|stem opt)\b", True, False, True,
     "F-1 student: may work through CPT/OPT now; long-term employment needs sponsorship (H-1B)"),
    (r"\b(on|hold\w*|current\w*)\s+(an?\s+)?h-?1b\b|\bh-?1b\s+(visa\s+)?holder",
     True, True, True, "H-1B: a new employer must sponsor a transfer"),
    (r"\b(on|hold\w*|current\w*)\s+(an?\s+)?j-?1\b|\bj-?1\s+(visa\s+)?holder|\bj-?1 (student|scholar|visa)\b",
     True, False, True, "J-1: work tied to the program; later employment needs sponsorship"),
    (r"\b(ead|work permit|authorized to work)\b", True, False, False, "Stated as authorized to work"),
]


def path():
    return config.PROFILE_DIR / PATH_NAME


def squash(text: str) -> str:
    """Letters and digits only, so an added comma or space isn't a new fact."""
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


# A status is only stated if it isn't negated just before it ("not a U.S. citizen").
NEGATED = re.compile(r"\b(not|non|no longer|isn'?t|aren'?t|never)\b[\s-]*(an?\s+)?$", re.I)
# Sentences about sponsorship itself outrank what the status implies.
NO_SPONSOR = re.compile(r"\b(no|not|without|never|don'?t|do not|does not|won'?t|will not)\s+(need|require)\w*\s+"
                        r"(any\s+)?(visa\s+|employer\s+|h-?1b\s+)?sponsorship|sponsorship\s+(is\s+)?not\s+(needed|required)",
                        re.I)
NEEDS_SPONSOR = re.compile(r"\b(need|require)\w*\s+(visa\s+|employer\s+|h-?1b\s+)?sponsorship", re.I)


def stated(pattern: str, text: str) -> bool:
    return any(not NEGATED.search(text[:m.start()]) for m in re.finditer(pattern, text, re.I))


def work_authorization(text: str, country: str) -> dict:
    """Answers implied by the resume's own status sentence, by fixed rules. A sentence
    about sponsorship outranks the status; anything unclear assumes nothing (the forms'
    questions then come to you)."""
    if not text:
        return {"status": "", "rule": "No status on the resume; nothing assumed."}
    out = {"status": text}
    for pattern, authorized, now, later, rule in STATUS_RULES:
        if stated(pattern, text):
            out.update(authorized_in_us=authorized, requires_sponsorship_now=now,
                       requires_sponsorship_future=later, rule=rule)
            break
    if NO_SPONSOR.search(text):
        out.update(requires_sponsorship_now=False, requires_sponsorship_future=False)
        out["rule"] = (out.get("rule", "Status") + "; the resume says no sponsorship is needed")
    elif NEEDS_SPONSOR.search(text):
        out["requires_sponsorship_future"] = True  # "Now" stays as the status implies, or unknown.
        if re.search(r"\b(now|currently|immediately|to start)\b", text, re.I):
            out["requires_sponsorship_now"] = True
        out["rule"] = (out.get("rule", "Status") + "; the resume says sponsorship is needed")
    if "rule" not in out:
        out["rule"] = "Status not recognized; nothing assumed."
    return out


def links(resume: str) -> dict:
    found = {"linkedin": "", "github": "", "scholar": "", "website": ""}
    first_lines = "\n".join(resume.splitlines()[:4])  # the contact block
    for url in URL.findall(first_lines):
        host = url.lower()
        key = ("linkedin" if "linkedin.com" in host else "github" if "github.com" in host
               else "scholar" if "scholar.google" in host else "website")
        found[key] = found[key] or url
    return found


def derive(llm, resume: str) -> tuple[dict, list[str]]:
    """Profile facts from the resume, and notes on anything dropped or inferred."""
    reply = llm.json(PROMPT, resume, SCHEMA, "resume_facts")
    text, notes = squash(resume), []

    def keep(label: str, value: str) -> str:
        value = (value or "").strip()
        if value and squash(value) not in text:
            notes.append(f"Dropped {label} {value!r}: not in the resume.")
            return ""
        return value

    education = []
    for entry in reply["education"]:
        kept = {k: keep(f"education {k}", v) for k, v in entry.items()}
        if kept["school"]:
            education.append({k: v for k, v in kept.items() if v})
    location = keep("location", reply["location"])
    if not location and education:
        location = education[0].get("location", "")
    country = "United States" if re.search(r"\b(usa|united states|u\.s\.)\b|, [A-Z]{2}$",
                                           location or "", re.I) else ""
    # Read in code where the resume says them plainly: one model wrote "mohammed" for "mohammad".
    block = "\n".join(resume.splitlines()[:4])
    email = re.search(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", block)
    phone = re.search(r"\+?\d?[\s.-]?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b", block)
    title = re.match(r"#\s+(.+)", resume.strip())
    contact = {"name": keep("name", reply["name"]) or (title.group(1).strip() if title else ""),
               "email": email.group(0) if email else keep("email", reply["email"]),
               "phone": phone.group(0).strip() if phone else keep("phone", reply["phone"]), "location": location,
               "country": country, **links(resume)}
    status = keep("work authorization", reply["work_authorization_text"])
    authorization = work_authorization(status, country)
    notes.append(f"Work authorization: {authorization['rule']}")
    for field in ("phone", "location"):
        if not contact[field]:
            notes.append(f"Your resume has no {field}; questions asking for it will come to you.")
    profile = {
        "contact": {k: v for k, v in contact.items() if v},
        "education": education,
        "work_authorization": authorization,
        "derived": {"from": "profile/resume.md", "on": date.today().isoformat()},
    }
    return profile, notes


def sync(llm, log=print) -> list[str]:
    resume = config.read_master_resume()
    profile, notes = derive(llm, resume)
    header = ("# Written by the agent from profile/resume.md. Don't edit: it's replaced\n"
              "# whenever you accept a new master resume. To override a field, set it in\n"
              "# profile.yaml.\n")
    path().write_text(header + yaml.safe_dump(profile, sort_keys=False, allow_unicode=True),
                      encoding="utf-8")
    for note in notes:
        log(f"  {note}")
    return notes


def merge(base: dict, override: dict) -> dict:
    """profile.yaml on top of the resume-derived profile, key by key."""
    merged = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge(merged[key], value)
        elif value not in (None, "", [], {}):
            merged[key] = value
    return merged
