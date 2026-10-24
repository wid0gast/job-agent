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
# The session's workspace is a 448 MiB in-memory disk (the "browser" profile), so the cache is capped.
CHROMIUM_ARGS = ["--disable-gpu", "--renderer-process-limit=2", "--disk-cache-size=33554432"]
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
  fields: []          # Empty: every field. Pick yours on the Search page.
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
        "BROWSER_CHANNEL": "",
        # The class models choose resume lines by id rather than write the resume (see prepare.select).
        "JOB_AGENT_TAILOR": os.environ.get("JOB_AGENT_TAILOR", "select"),
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


def warm_latex(ctx, log) -> str:
    """Typeset a one-line page at the start of a session, beside your first tasks, so Tectonic
    fetches its packages now rather than during the first tailored resume."""
    from . import config, latex, netrelay

    start = time.time()
    tex = latex.preamble(1) + "\n\\begin{document}\n\\section{Warm-up}\nReady.\n\\end{document}\n"
    try:
        latex.compile_pdf(tex, config.DATA_DIR / "warm-up.pdf")
    except Exception as error:
        mirror = netrelay._mirror
        raise RuntimeError(f"Resume typesetting isn't ready: {str(error)[:300]}"
                           + (f" · mirror: {mirror.errors[-2:]}" if mirror and mirror.errors else "")) from None
    log(f"Resume typesetting ready ({time.time() - start:.0f}s).")
    return f"Resume typesetting ready ({time.time() - start:.0f}s)."


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
