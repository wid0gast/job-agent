"""A packet is one prepared application: everything you approve, in one file.

data/applications/<id>-<slug>/packet.json holds the job, fit, every question
with its drafted answer and source, the documents and the checks. Approval
records a hash of the answers and documents; submit refuses if they changed
since.
"""

import hashlib
import json
import re
from pathlib import Path

from . import config
from .answers import from_profile
from .forms import norm

SCHEMA_VERSION = 1


def directory(job: dict) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", f"{job['company']}-{job['title']}".lower()).strip("-")
    return config.DATA_DIR / "applications" / f"{job['id']}-{slug[:60]}"


def save(packet: dict, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "packet.json"
    # Written whole, then swapped in: a crash mid-write never leaves half a packet.
    partial = folder / "packet.json.partial"
    partial.write_text(json.dumps(packet, indent=1, ensure_ascii=False), encoding="utf-8")
    partial.replace(path)
    return path


def load(job: dict) -> dict | None:
    folder = Path(job["packet_dir"]) if job.get("packet_dir") else directory(job)
    path = folder / "packet.json"
    if not path.exists():
        return None
    packet = json.loads(path.read_text(encoding="utf-8"))
    packet["_dir"] = str(folder)
    return packet


def store(packet: dict) -> None:
    folder = Path(packet.pop("_dir"))
    save(packet, folder)
    packet["_dir"] = str(folder)


def update(job: dict, change) -> dict:
    """Apply `change` to the packet as it is on disk now, and store it. A step that spends
    minutes on the model works out its part first and changes only that here, so what you
    edited or approved meanwhile stays."""
    packet = load(job)
    if packet is None:
        raise ValueError(f"Job {job['id']} has no packet.")
    change(packet)
    store(packet)
    return packet


def basis(inputs) -> str:
    """A short fingerprint of everything a step read. Stored with its result, it says whether
    that result still holds: a redo reuses what nothing it read has changed since."""
    return hashlib.sha1(json.dumps(inputs, sort_keys=True, default=str).encode()).hexdigest()[:12]


def content_hash(packet: dict) -> str:
    """What approval covers: every answer value plus the exact document files."""
    digest = hashlib.sha256()
    for answer in sorted(packet["answers"], key=lambda a: a["key"]):
        digest.update(json.dumps([answer["key"], answer.get("value")]).encode())
    for document in (packet.get("documents") or {}).values():
        if document and Path(document["pdf"]).exists():
            digest.update(Path(document["pdf"]).read_bytes())
    return digest.hexdigest()


def missing(packet: dict) -> list[dict]:
    """Required questions without an answer: these need you before approval."""
    return [a for a in packet["answers"] if a["required"] and a.get("value") in (None, "", [])]


def files(packet: dict) -> dict:
    return {kind: Path(doc["pdf"]) for kind, doc in (packet.get("documents") or {}).items() if doc}


CONSISTENT = re.compile(
    r"authori[sz]ed to work|sponsor|graduat|e-?mail|phone|first name|last name|gpa", re.I)


def consistency(packet: dict, profile: dict) -> list[str]:
    """Answers that contradict your profile or resume. These block approval."""
    problems = []
    for answer in packet["answers"]:
        if answer.get("value") in (None, "", []) or not CONSISTENT.search(answer["label"]):
            continue
        expected = from_profile({**answer}, profile, packet["job"])
        if expected and norm(str(expected[0])) != norm(str(answer["value"])):
            problems.append(f"“{answer['label']}” is {answer['value']!r}, "
                            f"but your profile says {expected[0]!r}.")
    resume = (packet.get("documents") or {}).get("resume")
    email = profile.get("contact", {}).get("email")
    if resume and email and email in config.read_master_resume():
        text = Path(resume["markdown"]).read_text(encoding="utf-8")
        if email not in text:
            problems.append("The tailored resume doesn't show your profile email.")
    return problems
