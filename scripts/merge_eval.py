"""Merge eval: render each case's resume versions to PDFs, run the app's own pipeline (PDF text,
the model's Markdown conversion with its checks, then the merge) and grade the merged master
against the case's answer key. See docs/merge-guide.md (rules) and docs/merge-eval.md (results).

    uv run python scripts/merge_eval.py                      # every case, the app's default models
    uv run python scripts/merge_eval.py --tier claude-haiku-5-5 --cases priya,tom
    uv run python scripts/merge_eval.py --merge-only         # reuse cached conversions; regrade the merge
    uv run python scripts/merge_eval.py --real PATH/TO/PDFS --key PATH/TO/key.yaml   # local only

Conversions are cached per (case, version, model, prompt), so changes to the merge are
regraded in seconds. Nothing here touches your profile: each case runs in a temporary one.
"""

import argparse
import hashlib
import html
import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from job_agent import config, latex, llm, resumes  # noqa: E402

CASES = ROOT / "evals" / "merge" / "cases.yaml"
CACHE = ROOT / "evals" / "merge" / ".cache"
NO_MODEL = False  # --no-model: the merge without the model's judgment (its fallback).

HTML_STYLE = """
body{font:10.5pt Helvetica,Arial,sans-serif;margin:0;color:#111}
h1{font-size:20pt;text-align:center;margin:0 0 4px;letter-spacing:1px}
.contact{text-align:center;font-size:9pt;margin-bottom:10px}
h2{font-size:10.5pt;text-transform:uppercase;letter-spacing:3px;border-bottom:1px solid #333;margin:12px 0 4px}
.role{display:flex;justify-content:space-between;font-weight:bold;margin-top:6px}
.meta{font-style:italic;font-size:9.5pt}
ul{margin:2px 0 0 16px;padding:0}li{margin:1px 0}
p{margin:2px 0}
"""


def contact_of(header: list[str], title: str) -> dict:
    line = " ".join(header)
    contact = {"name": title.lstrip("# ").title()}
    if email := re.search(r"[\w.+-]+@[\w.-]+\.\w+", line):
        contact["email"] = email.group(0)
    if phone := re.search(r"\+?\(?\d[\d ()+-]{8,}\d", line):
        contact["phone"] = phone.group(0).strip()
    for part in [p.strip() for p in line.split("·")]:
        url = part if part.startswith("http") else f"https://{part}"
        if "linkedin.com" in part:
            contact["linkedin"] = url
        elif "github.com" in part:
            contact["github"] = url
        elif re.fullmatch(r"[\w-]+\.(dev|design|io|me|com|org)", part):
            contact["website"] = url
    return contact


def render_html(markdown: str) -> str:
    """A second, different layout: spaced-out headings, dates on the role's line."""
    out, open_list = [], False
    lines = markdown.strip().splitlines()
    for index, line in enumerate(lines):
        text = line.strip()
        if open_list and not text.startswith("- "):
            out.append("</ul>")
            open_list = False
        if not text:
            continue
        if text.startswith("# "):
            out.append(f"<h1>{html.escape(text[2:])}</h1>")
        elif index == 1:
            out.append(f'<div class="contact">{html.escape(text).replace(" · ", " &nbsp;|&nbsp; ")}</div>')
        elif text.startswith("## "):
            out.append(f"<h2>{html.escape(text[3:])}</h2>")
        elif text.startswith("### "):
            title = text[4:].replace(" — ", ", ")
            dates = lines[index + 1].strip() if index + 1 < len(lines) else ""
            if dates and not dates.startswith(("-", "#")) and re.search(r"\d{4}", dates):
                when, _, where = dates.partition(" · ")
                out.append(f'<div class="role"><span>{html.escape(title)}</span><span>{html.escape(when)}</span></div>')
                if where:
                    out.append(f'<div class="meta">{html.escape(where)}</div>')
                lines[index + 1] = ""
            else:
                out.append(f'<div class="role"><span>{html.escape(title)}</span></div>')
        elif text.startswith("- "):
            if not open_list:
                out.append("<ul>")
                open_list = True
            body = html.escape(text[2:])
            body = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", body)
            out.append(f"<li>{body}</li>")
        else:
            out.append(f"<p>{html.escape(text)}</p>")
    if open_list:
        out.append("</ul>")
    return f"<!doctype html><html><head><style>{HTML_STYLE}</style></head><body>{''.join(out)}</body></html>"


def render_pdf(version: dict, output: Path, headless) -> None:
    markdown = version["markdown"]
    if version["style"] == "latex":
        doc, top = latex.from_markdown(markdown), resumes.structure(markdown)
        tex = latex.resume_tex(doc, contact_of(top["header"], top["title"]), pages=2)
        latex.compile_pdf(tex, output)
    else:
        headless.render_pdf(render_html(markdown), output)


def model_label(model) -> str:
    return "-".join(model.choose("resume_markdown")[:1]) or "default"


def run_case(name: str, case: dict, model, headless, merge_only: bool, log) -> dict:
    work = Path(tempfile.mkdtemp(prefix=f"merge-{name}-"))
    profile = work / "profile"
    (profile / "resumes").mkdir(parents=True)
    config.PROFILE_DIR = profile
    if case.get("master"):
        (profile / "resume.md").write_text(case["master"].strip() + "\n", encoding="utf-8")
    prompt = hashlib.sha1(resumes.PARSE_PROMPT.encode()).hexdigest()[:8]
    started, calls = time.time(), getattr(model, "calls", 0)
    for index, version in enumerate(case["versions"]):
        stem = f"{name}-v{index + 1}-{version['date']}"
        pdf = work / "uploads" / f"{stem}.pdf"
        pdf.parent.mkdir(exist_ok=True)
        cached = CACHE / f"{stem}-{version['style']}-{model_label(model)}-{prompt}"
        resumes.parsed_folder().mkdir(parents=True, exist_ok=True)
        if (cached / "done").exists():
            for f in cached.glob(f"{stem}.*"):
                shutil.copy(f, resumes.parsed_folder() / f.name)
            continue
        if merge_only:
            raise SystemExit(f"No cached conversion for {stem}: run once without --merge-only.")
        render_pdf(version, pdf, headless)
        shutil.copy(pdf, profile / "resumes" / pdf.name)
        details = resumes.parse(model, profile / "resumes" / pdf.name, log=lambda *a: log("   ", *a))
        meta = resumes.parsed_folder() / f"{stem}.json"
        data = json.loads(meta.read_text())
        data["latest_date"] = version["date"]  # The case's own date (PDF dates are generated today).
        meta.write_text(json.dumps(data, indent=1))
        cached.mkdir(parents=True, exist_ok=True)
        for f in resumes.parsed_folder().glob(f"{stem}.*"):
            shutil.copy(f, cached / f.name)
        (cached / "done").write_text(json.dumps(details.get("checks", {})))
    result = resumes.propose(log=lambda *a: None, llm=None if NO_MODEL else model)
    merged = (profile / "resume.proposed.md").read_text(encoding="utf-8")
    graded = grade(merged, case["key"], result.get("possible"))
    graded.update(seconds=round(time.time() - started, 1), model_calls=getattr(model, "calls", 0) - calls,
                  merged=merged, kept=result.get("kept"), changes=result.get("changes"), combined=result.get("combined"), possible=result.get("possible"),
                  conversion_checks={f.name: json.loads((f / "done").read_text()) for f in CACHE.glob(f"{name}-v*")
                                     if (f / "done").exists() and model_label(model) in f.name and prompt in f.name})
    shutil.rmtree(work, ignore_errors=True)
    return graded


def norm(text) -> str:
    text = str(text).replace("–", "-").replace("—", "-").replace("’", "'").replace("**", "")
    return re.sub(r"\s+", " ", text).strip().lower()


def section(markdown: str, name: str) -> str:
    match = re.search(rf"^## [^\n]*{name}[^\n]*\n(.*?)(?=^## |\Z)", markdown, re.M | re.S | re.I)
    return match.group(1) if match else ""


def grade(merged: str, key: dict, possible: list | None = None) -> dict:
    lines = [norm(l) for l in merged.splitlines() if l.strip()]
    flagged = {norm(t) for group in possible or [] for t in group["lines"]}
    failures = []
    for phrases in key.get("once", []):
        hits = [l for l in lines if any(norm(p) in l for p in phrases)]
        if len(hits) > 1 and all(h in flagged for h in hits):
            continue  # Kept twice on purpose (each says something) and shown to you as a possible repeat.
        if len(hits) != 1:
            failures.append(f"{'missing' if not hits else f'{len(hits)} times'}: {phrases[0]}")
    text = norm(merged)
    for phrase in key.get("keep", []):
        if norm(phrase) not in text:
            failures.append(f"lost: {phrase}")
    for phrase in key.get("absent", []):
        if norm(phrase) in text:
            failures.append(f"stale kept: {phrase}")
    skills = norm(section(merged, "skill"))
    for skill in key.get("skills", []):
        count = len(re.findall(rf"(?<![a-z0-9]){re.escape(norm(skill))}(?![a-z0-9])", skills))
        if count != 1:
            failures.append(f"skill {skill}: {count} times")
    sections = re.findall(r"^## (.+)$", merged, re.M)
    if key.get("first_section_is_summary") and not (sections and re.search(
            r"summary|profile|objective|focus|about", sections[0], re.I)):
        failures.append(f"first section is {sections[0] if sections else 'none'}, not a summary")
    if len({norm(s) for s in sections}) != len(sections):
        failures.append("a section appears twice")
    empty = [s for s in sections if not section(merged, re.escape(s)).strip()]
    if empty:
        failures.append(f"empty sections: {empty}")
    artifacts = re.findall(r"\b(?:[A-Z] ){3,}[A-Z]\b|\w -\w", merged)
    if artifacts:
        failures.append(f"artifacts: {artifacts[:3]}")
    checks = len(key.get("once", [])) + len(key.get("keep", [])) + len(key.get("absent", [])) + \
        len(key.get("skills", [])) + 4
    return {"failures": failures, "checks": checks, "passed": checks - len(failures)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", default="")
    parser.add_argument("--tier", default="", help="Model for the standard tier (conversion and merge).")
    parser.add_argument("--merge-only", action="store_true")
    parser.add_argument("--out", default="")
    parser.add_argument("--real", default="", help="A folder of your own resume PDFs (never committed).")
    parser.add_argument("--key", default="", help="The answer key for --real.")
    parser.add_argument("--show", action="store_true", help="Print each merged resume.")
    parser.add_argument("--no-model", action="store_true", help="Merge in code only (the fallback).")
    args = parser.parse_args()
    global NO_MODEL
    NO_MODEL = args.no_model
    from playwright.sync_api import sync_playwright

    from job_agent.browser import Headless

    from dataclasses import replace

    settings = config.Settings.from_env(require_model=False)
    if args.tier and hasattr(settings, "tier_models"):  # The class-platform build has one model per visit.
        settings = replace(settings, tier_models={**settings.tier_models, "standard": args.tier})
    model = llm.client(settings)
    cases = yaml.safe_load(CASES.read_text())
    if args.real:  # Your own resume: its cached conversions live next to your key, not in the repo.
        global CACHE
        CACHE = Path(args.key).resolve().parent / ".merge-cache"
        cases = {"real": {"versions": [{"style": "pdf", "date": None, "pdf": str(p)}
                                       for p in sorted(Path(args.real).glob("*.pdf"))],
                          "key": yaml.safe_load(Path(args.key).read_text())}}
    wanted = [c for c in (args.cases.split(",") if args.cases else cases)]
    results = {}
    with sync_playwright() as pw:
        headless = Headless(pw, settings.browser_channel)
        for name in wanted:
            case = cases[name]
            if args.real:
                results[name] = run_real(case, model, args.merge_only, print)
            else:
                results[name] = run_case(name, case, model, headless, args.merge_only, print)
            r = results[name]
            print(f"{name:7} {r['passed']}/{r['checks']}  {r['seconds']}s  {r['model_calls']} calls")
            for failure in r["failures"]:
                print("        -", failure)
            if args.show:
                print(r["merged"])
        headless.close()
    total = sum(r["passed"] for r in results.values()), sum(r["checks"] for r in results.values())
    print(f"TOTAL {total[0]}/{total[1]} with {model_label(model)}")
    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=1, default=str))


def run_real(case: dict, model, merge_only: bool, log) -> dict:
    """Your own PDFs, in a temporary profile; the key is yours too (both stay off git)."""
    work = Path(tempfile.mkdtemp(prefix="merge-real-"))
    config.PROFILE_DIR = work / "profile"
    (config.PROFILE_DIR / "resumes").mkdir(parents=True)
    started, calls = time.time(), getattr(model, "calls", 0)
    prompt = hashlib.sha1(resumes.PARSE_PROMPT.encode()).hexdigest()[:8]
    for version in case["versions"]:
        pdf = Path(version["pdf"])
        cached = CACHE / f"real-{pdf.stem}-{model_label(model)}-{prompt}"
        resumes.parsed_folder().mkdir(parents=True, exist_ok=True)
        if cached.exists():
            for f in cached.glob(f"{pdf.stem}.*"):
                shutil.copy(f, resumes.parsed_folder() / f.name)
            continue
        if merge_only:
            raise SystemExit(f"No cached conversion for {pdf.name}.")
        shutil.copy(pdf, config.PROFILE_DIR / "resumes" / pdf.name)
        resumes.parse(model, config.PROFILE_DIR / "resumes" / pdf.name, log=lambda *a: log("   ", *a))
        cached.mkdir(parents=True, exist_ok=True)
        for f in resumes.parsed_folder().glob(f"{pdf.stem}.*"):
            shutil.copy(f, cached / f.name)
    result = resumes.propose(log=lambda *a: None, llm=None if NO_MODEL else model)
    merged = (config.PROFILE_DIR / "resume.proposed.md").read_text(encoding="utf-8")
    graded = grade(merged, case["key"], result.get("possible"))
    graded.update(seconds=round(time.time() - started, 1), model_calls=getattr(model, "calls", 0) - calls,
                  merged=merged, kept=result.get("kept"), changes=result.get("changes"),
                  combined=result.get("combined"), possible=result.get("possible"))
    shutil.rmtree(work, ignore_errors=True)
    return graded


if __name__ == "__main__":
    main()
