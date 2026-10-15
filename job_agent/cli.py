"""Command line entry point: setup, login, refresh, prepare, dashboard, worker, run, status."""

import argparse
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from . import config
from .tracker import Tracker, now
from .worker import BROWSER_PROFILE, DB_PATH, browser_lock


def cmd_setup(_args) -> None:
    if not config.PROFILE_DIR.exists():
        shutil.copytree(config.PROFILE_EXAMPLE_DIR, config.PROFILE_DIR)
        print("Created profile/ from profile.example/.")
    env = config.ROOT / ".env"
    if not env.exists():
        shutil.copy(config.ROOT / ".env.example", env)
        print("Created .env from .env.example.")
    print(
        "\nNext:\n"
        "  1. Edit profile/profile.yaml and replace profile/resume.md with your\n"
        "     full master resume (everything true you might ever include).\n"
        "     Optional: add profile/writing-sample.md so cover letters sound like you.\n"
        "  2. Put your OpenRouter key and model in .env.\n"
        "  3. uv run job-agent dashboard   (refresh, prepare, approve, submit)"
    )


def cmd_login(args) -> None:
    from .browser import Browser

    sites = config.load_sites()
    settings = config.Settings.from_env(require_model=False)
    with browser_lock():
        _login(args, sites, settings, Browser)


def _login(args, sites, settings, Browser) -> None:
    browser = Browser(BROWSER_PROFILE, settings.browser_channel)
    try:
        for index, name in enumerate(args.sites):
            if name not in sites:
                raise SystemExit(f"Unknown site {name!r}. Known: {', '.join(sites)}")
            page = browser.page if index == 0 else browser.context.new_page()
            page.goto(sites[name]["login_url"])
        input(
            "Sign in to each tab in the Chrome window. Logins are saved in\n"
            "data/browser-profile for future runs. Press Enter here when done. "
        )
    finally:
        browser.close()


def cmd_run(args) -> None:
    with browser_lock():
        _run(args)


def _run(args) -> None:
    from .browser import Browser
    from .harness import AgentRun, OpenRouter
    from .prompts import APPLY_PROMPT, SEARCH_PROMPT
    from .tools import apply_tools, search_tools

    settings = config.Settings.from_env()
    profile = config.load_profile()
    config.read_master_resume()  # Fail early if it is missing.
    all_sites = config.load_sites()
    unknown = [s for s in args.sites if s not in all_sites]
    if unknown:
        raise SystemExit(f"Unknown sites {unknown}. Known: {', '.join(all_sites)}")
    sites = {name: all_sites[name] for name in args.sites}

    tracker = Tracker(DB_PATH)
    llm = OpenRouter(settings.api_key, settings.model, settings.spend_cap_usd,
                     settings.data_collection)
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    logs = config.DATA_DIR / "logs" / run_id
    started = "" if args.include_queued else now()
    print(f"Model: {settings.model} · submit mode: {settings.submit_mode} · "
          f"daily limit: {settings.daily_submit_limit} "
          f"(used today: {tracker.submitted_today()})")

    browser = Browser(BROWSER_PROFILE, settings.browser_channel)
    try:
        queued = tracker.leads(args.max, since=started)
        if len(queued) < args.max and not args.no_search:
            print(f"\n== Searching for {args.max - len(queued)} matching jobs")
            search = AgentRun(llm, SEARCH_PROMPT, search_tools(browser, tracker,
                              args.max - len(queued)), logs / "search.jsonl", max_steps=80)
            search.run(json.dumps({
                "request": args.request,
                "target_new_leads": args.max - len(queued),
                "sites": sites,
                "search_preferences": profile.get("preferences", {}),
                "education": profile.get("education", []),
            }, indent=1))

        jobs = tracker.leads(args.max, since=started)
        print(f"\n== Applying to {len(jobs)} job(s)")
        for job in jobs:
            print(f"\n-- {job['title']} at {job['company']}  ({job['url']})")
            tracker.update(job["id"], status="working")
            slug = re.sub(r"[^a-z0-9]+", "-", f"{job['company']}-{job['title']}".lower())
            job_dir = config.DATA_DIR / "applications" / f"{job['id']}-{slug[:60]}"
            agent = AgentRun(llm, APPLY_PROMPT,
                             apply_tools(browser, tracker, settings, profile, job, job_dir),
                             logs / f"job-{job['id']}.jsonl", max_steps=150)
            try:
                outcome = agent.run(json.dumps({
                    "job": {k: job[k] for k in ("url", "site", "company", "title", "location")},
                    "user_request": args.request,
                    "site_notes": all_sites.get(job["site"], {}).get("notes", ""),
                }, indent=1))
            except KeyboardInterrupt:
                tracker.update(job["id"], status="needs_you", reason="Interrupted by user.")
                raise
            except Exception as error:  # noqa: BLE001 - record and move to the next job.
                outcome = f"failed: {type(error).__name__}: {error}"
                tracker.update(job["id"], status="failed", reason=outcome[:2000])
            # An agent that stopped without finish_job leaves the job unresolved.
            row = next(r for r in tracker.all() if r["id"] == job["id"])
            if row["status"] == "working":
                tracker.update(job["id"], status="needs_you",
                               reason=f"Agent stopped without a recorded outcome: {outcome[:500]}")
            print(f"   result: {outcome[:300]}")
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        browser.close()
    print(f"\nLogs: {logs}")
    cmd_status(args)


def cmd_status(_args) -> None:
    rows = [r for r in Tracker(DB_PATH).all()
            if getattr(_args, "all", False) or r["status"] not in ("found", "filtered_out", "closed")]
    if not rows:
        print("No jobs tracked yet.")
        return
    print(f"\n{'id':>4}  {'status':<11} {'company':<24} {'title':<40} updated")
    for r in rows:
        print(f"{r['id']:>4}  {r['status']:<11} {(r['company'] or '')[:24]:<24} "
              f"{(r['title'] or '')[:40]:<40} {r['updated_at'][:16]}")
        if r["status"] in ("failed", "needs_you", "skipped", "filtered_out") and r["reason"]:
            print(f"{'':>17}{r['reason'][:110]}")


def cmd_refresh(args) -> None:
    from .sources import refresh

    counts = refresh(Tracker(DB_PATH), config.load_profile(), boards=args.boards)
    print(f"{counts['new']} new, {counts['closed']} closed, {counts['seen']} already known, "
          f"{counts['filtered']} filtered out.")


def cmd_prepare(args) -> None:
    from . import systems
    from .prepare import prepare
    from .worker import Context

    settings = config.Settings.from_env(require_model=False)
    tracker = Tracker(DB_PATH)
    ids = args.job_ids or [j["id"] for j in tracker.with_status("found", order="posted_at DESC")
                           if j["ats"] in systems.SUPPORTED][: args.top]
    with browser_lock():
        ctx = Context(settings, tracker, require_model=True)
        try:
            for job_id in ids:
                job = tracker.get(job_id)
                print(f"\n-- #{job_id} {job['title']} at {job['company']}")
                prepare(job_id, ctx, print)
        finally:
            print(f"\nModel calls: {ctx.llm.calls}, spent ${ctx.llm.spent_usd:.4f}")
            ctx.close()


def cmd_resumes(args) -> None:
    from . import resumes
    from .llm import OpenRouter

    def model():
        settings = config.Settings.from_env()
        return OpenRouter(settings.api_key, settings.model, settings.spend_cap_usd,
                          settings.data_collection)

    if args.action == "import":
        if not args.pdfs:
            raise SystemExit("Give one or more PDF paths.")
        llm = model()
        for name in args.pdfs:
            source = Path(name).expanduser()
            pdf = resumes.save_upload(source.name, source.read_bytes())
            details = resumes.parse(llm, pdf)
            print(f"{pdf.name}: content through {details['latest_date']}; checks {details['checks']}")
        args.action = "merge"
    if args.action == "merge":
        proposal = resumes.propose()
        print(f"Proposal from {len(proposal['sources'])} version(s): profile/resume.proposed.md")
        for line in proposal["changes"]:
            print(f"  - {line}")
        for kept in proposal["kept"]:
            print(f"  + kept from {kept['from']}: {kept['line'][:110]}")
        if any(proposal["checks"].values()):
            print(f"  ! not found in any resume: {proposal['checks']}")
        print("Review it, then: uv run job-agent resumes accept")
    elif args.action == "accept":
        from . import profile_sync

        backup = resumes.accept()
        print("profile/resume.md updated." + (f" Previous: {backup}" if backup else ""))
        print("Updating your profile from it:")
        profile_sync.sync(model())
    elif args.action == "sync":
        from . import profile_sync

        profile_sync.sync(model())
    elif args.action == "discard":
        resumes.discard()
        print("Proposal discarded.")
    elif args.action == "list":
        for v in resumes.parsed():
            print(f"{v['latest_date'] or 'unknown':>8}  {v['source']}")


def cmd_history(args) -> None:
    from . import history

    tracker = Tracker(DB_PATH)
    if args.action == "import":
        if not args.file:
            raise SystemExit("Give the CSV file to import.")
        report = history.import_sheet(Path(args.file).expanduser(), tracker, args.status_column)
        print(f"Read your statuses from the {report['status_column']!r} column.")
        print(f"{report['added']} applications added, {report['updated']} outcomes updated, "
              f"{report['unchanged']} already recorded.")
        if report["unknown_dates"]:
            print(f"{report['unknown_dates']} had no date: dated today (so they count toward company "
                  "caps) and marked unknown. Set real dates on the dashboard's Applications page.")
        for line in report["skipped"]:
            print(f"  skipped: {line}")
    elif args.action == "export":
        target = Path(args.file or "data/applications-export.csv").expanduser()
        print(f"Wrote {history.export_sheet(target, tracker)} applications to {target}")
    else:
        stats = history.summary(tracker)
        print(f"{stats['total']} applications · {stats['response_rate']}% heard back · "
              f"{stats['positive_rate']}% OA/interview/offer")
        for job in tracker.applications():
            known = "" if job["applied_date_known"] else "?"
            print(f"  {(job['submitted_at'] or '')[:10]}{known:1} {job['outcome'] or 'applied':<11} "
                  f"{(job['company'] or '')[:24]:<24} {(job['title'] or '')[:40]}")


def cmd_dashboard(args) -> None:
    from .dashboard import serve

    serve(config.Settings.from_env(require_model=False), args.port)


def cmd_worker(_args) -> None:
    from .worker import serve

    def log(text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{stamp}] {text}", flush=True)

    serve(config.Settings.from_env(require_model=False), log)


def cmd_demo_site(args) -> None:
    from .demo_site import serve

    serve(args.port)


def main() -> None:
    parser = argparse.ArgumentParser(prog="job-agent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="Create profile/ and .env from the examples.")
    login = sub.add_parser("login", help="Open site login pages to sign in by hand.")
    login.add_argument("sites", nargs="+")
    run = sub.add_parser("run", help="Find matching jobs and apply.")
    run.add_argument("request", help='What to apply for, e.g. "ML internships in NYC".')
    run.add_argument("--max", type=int, default=3, help="Jobs to apply to this run.")
    run.add_argument("--sites", type=lambda s: s.split(","), default=["linkedin"],
                     help="Comma-separated keys from sites.yaml.")
    run.add_argument("--include-queued", action="store_true",
                     help="Also apply to leads found by earlier runs.")
    run.add_argument("--no-search", action="store_true",
                     help="Skip searching; apply to queued leads only (implies --include-queued).")
    status = sub.add_parser("status", help="List tracked jobs past the found stage.")
    status.add_argument("--all", action="store_true", help="Include found, filtered and closed jobs.")
    refresh = sub.add_parser("refresh", help="Pull the master job list (Simplify lists).")
    refresh.add_argument("--boards", action="store_true",
                         help="Also read every known company's Greenhouse board.")
    prep = sub.add_parser("prepare", help="Prepare packets for found jobs (no submitting).")
    prep.add_argument("job_ids", nargs="*", type=int)
    prep.add_argument("--top", type=int, default=1, help="Newest N supported jobs (default 1).")
    res = sub.add_parser("resumes", help="Build the master resume from resume PDFs.")
    res.add_argument("action", choices=["import", "merge", "accept", "discard", "list", "sync"])
    res.add_argument("pdfs", nargs="*", help="PDF files (for import).")
    hist = sub.add_parser("history", help="Your applications: list, import a job sheet, export.")
    hist.add_argument("action", choices=["list", "import", "export"])
    hist.add_argument("file", nargs="?", help="CSV to import, or where to export.")
    hist.add_argument("--status-column", help="The sheet column with your statuses.")
    dash = sub.add_parser("dashboard", help="Open the local dashboard (starts the worker).")
    dash.add_argument("--port", type=int, default=8770)
    sub.add_parser("worker", help="Run queued tasks (the dashboard starts this for you).")
    demo = sub.add_parser("demo-site", help="Serve a fake job board for safe testing.")
    demo.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if getattr(args, "no_search", False):
        args.include_queued = True
    {
        "setup": cmd_setup,
        "login": cmd_login,
        "run": cmd_run,
        "status": cmd_status,
        "refresh": cmd_refresh,
        "prepare": cmd_prepare,
        "resumes": cmd_resumes,
        "history": cmd_history,
        "dashboard": cmd_dashboard,
        "worker": cmd_worker,
        "demo-site": cmd_demo_site,
    }[args.command](args)


if __name__ == "__main__":
    main()
