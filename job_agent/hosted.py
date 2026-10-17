"""Run the dashboard on the class app store: `python3.12 -m job_agent.hosted`.

The same app as on a laptop, with the platform's differences handled here:
the app folder is read-only (everything goes in the session workspace), models
come from the class broker, the browser is the platform's Chromium (the "browser"
runtime profile) and reaches the web through netrelay, and nothing survives the
session (the dashboard offers a download of everything).
"""

import json
import os
import shutil
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
TECTONIC = APP / "bin" / "tectonic"
TECTONIC_CACHE = APP / "bin" / "tectonic-cache.tgz"

# Chromium's own sandbox stays on, as in the platform's browser image.
CHROMIUM_ARGS = ["--disable-gpu", "--renderer-process-limit=2"]
# The platform proxy rejects a TLS ClientHello carrying Encrypted Client Hello (extension
# 0xfe0d, see services/worker/src/proxy/tls-hello.ts), which Chromium sends by default.
# Only this Local State setting turns it off; the old EncryptedClientHello feature flag no longer does.
LOCAL_STATE = {"ssl": {"ech_enabled": False}}

# A visitor's starting profile: no personal details. They come from the resume
# the visitor uploads, and their preferences from the Search page.
STARTER_PROFILE = """\
# Your details come from the resume you upload; your job preferences from the Search page.
preferences:
  roles: []
  fields: [ai_ml_data]
  job_types: [internship, full_time]
  terms: []
  locations: [any]
  max_posting_age_days: 45
  exclude_title_words: []
  exclude_companies: []
eeo:
  gender: Decline to self-identify
  race_ethnicity: Decline to self-identify
  hispanic_latino: Decline to self-identify
  veteran: Decline to self-identify
  disability: Decline to self-identify
"""


def prepare_environment() -> Path:
    """Point the app at the session workspace. Must run before job_agent.config is imported."""
    work = Path(os.environ.get("WORKSHOP_WORKDIR") or "/tmp") / "job-agent"
    for folder in ("home", "tmp", "profile", "data"):
        (work / folder).mkdir(parents=True, exist_ok=True)
    os.environ.update({
        "JOB_AGENT_HOME": str(work), "JOB_AGENT_HOSTED": "1",
        "HOME": str(work / "home"), "XDG_CACHE_HOME": str(work / "home" / ".cache"),
        "TECTONIC_CACHE_DIR": str(work / "tectonic-cache"),
        "TMPDIR": str(work / "tmp"),  # The platform's /tmp holds only 32 MB.
        "PATH": f"{TECTONIC.parent}:{os.environ.get('PATH', '')}",
        "SUBMIT_MODE": "review", "BROWSER_CHANNEL": "",
    })
    profile = work / "profile" / "profile.yaml"
    if not profile.exists():
        profile.write_text(STARTER_PROFILE, encoding="utf-8")
    # LaTeX packages for the resume templates, fetched ahead of time so the first resume takes seconds.
    cache = work / "tectonic-cache"
    if TECTONIC_CACHE.exists() and not cache.exists():
        with tarfile.open(TECTONIC_CACHE) as tar:
            tar.extractall(work / "tectonic-unpack", filter="data")
        (work / "tectonic-unpack" / "cache").rename(cache)
        shutil.rmtree(work / "tectonic-unpack", ignore_errors=True)
    return work


def connect_models() -> dict:
    """Ask the broker which model this visit uses; the worker process inherits the settings."""
    url = os.environ.get("WORKSHOP_BROKER_URL", "").rstrip("/")
    token = os.environ.get("WORKSHOP_RUN_TOKEN", "")
    if not url or not token:
        return {}
    request = urllib.request.Request(url + "/v1/bootstrap", headers={
        "Authorization": f"Bearer {token}", "X-Workshop-Contract-Version": "workshop.app.v1"})
    # The broker is a class service: reach it directly, not through the web proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=30) as response:
        boot = json.load(response)
    selection = boot["inference"]["selection"]
    usage = boot.get("usage") or {}
    os.environ.update({
        "OPENROUTER_MODEL": selection["modelId"], "OPENROUTER_API_KEY": "class-broker",
        "WORKSHOP_PROFILE_ID": selection["profileId"],
        "SPEND_CAP_USD": str(usage.get("availableUsd") or usage.get("limitUsd") or "0.5"),
        "JOB_AGENT_SESSION_ENDS": boot.get("expiresAt", ""),
    })
    return boot


def chromium_options() -> dict:
    """Playwright launch options for the platform's Chromium, through the local relay."""
    from . import netrelay

    options = {"headless": True, "chromium_sandbox": True, "args": CHROMIUM_ARGS}
    proxy = netrelay.browser_proxy()
    if proxy:
        options["proxy"] = proxy
    executable = os.environ.get("WORKSHOP_BROWSER_EXECUTABLE")
    if executable:
        options["executable_path"] = executable
    # Otherwise a laptop rehearsal: Playwright's own Chromium.
    return options


def launch_chromium(playwright):
    """The hosted Chromium, started on a fresh profile that carries LOCAL_STATE.

    Playwright only seeds a profile through a persistent launch; its .browser still makes
    separate contexts and pages, which share the profile's settings."""
    profile = Path(os.environ["JOB_AGENT_HOME"]) / "chromium-profile"
    shutil.rmtree(profile, ignore_errors=True)
    profile.mkdir(parents=True)
    (profile / "Local State").write_text(json.dumps(LOCAL_STATE), encoding="utf-8")
    return playwright.chromium.launch_persistent_context(str(profile), **chromium_options()).browser


def free_mb() -> int:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable"):
                return int(line.split()[1]) // 1024
    except OSError:
        pass
    return -1


def warm_up(ctx, log) -> str:
    """First task of every session: prove the browser reaches job sites and LaTeX compiles."""
    from . import config, latex, netrelay

    notes = [f"Memory free: {free_mb()} MB."]
    start, browser = time.time(), None
    try:
        browser = launch_chromium(ctx.playwright)
        page = browser.new_page()
        response = page.goto("https://job-boards.greenhouse.io/anthropic", wait_until="load", timeout=60000)
        links = page.locator('a[href*="/jobs/"]').count()
        notes.append(f"Browser: HTTP {response.status if response else '?'}, {len(page.content()) // 1024} KB, "
                     f"{links} job links, {time.time() - start:.0f}s, memory free {free_mb()} MB.")
        if not links:
            notes.append("Browser can't load job sites.")
    except Exception as error:  # noqa: BLE001
        notes.append(f"Browser failed: {str(error).splitlines()[0][:200]}")
    finally:
        if browser:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
    if netrelay.recent_errors():
        notes.append(f"Relay: {netrelay.recent_errors()}")
    start = time.time()
    try:
        tex = latex.preamble(1) + "\n\\begin{document}\n\\section{Warm-up}\nReady.\n\\end{document}\n"
        latex.compile_pdf(tex, config.DATA_DIR / "warm-up.pdf")
        notes.append(f"Resume typesetting OK ({time.time() - start:.0f}s).")
    except Exception as error:  # noqa: BLE001
        mirror = netrelay._mirror
        notes.append(f"Resume typesetting failed: {str(error)[:300]}"
                     + (f" · mirror: {mirror.errors[-2:]}" if mirror and mirror.errors else ""))
    log(" ".join(notes))
    return " ".join(notes)


def main() -> None:
    prepare_environment()
    from . import netrelay

    netrelay.start()
    try:
        connect_models()
    except Exception as error:  # noqa: BLE001 - the dashboard still opens and says so.
        os.environ["JOB_AGENT_MODEL_ERROR"] = f"{type(error).__name__}: {error}"[:300]
    from . import config, dashboard
    from .tracker import Tracker
    from .worker import DB_PATH

    settings = config.Settings.from_env(require_model=False)
    Tracker(DB_PATH).add_task("warmup")
    dashboard.serve(settings, port=int(os.environ.get("PORT", "8080")), host="0.0.0.0")


if __name__ == "__main__":
    sys.exit(main())
