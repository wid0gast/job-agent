"""Run the dashboard on the class app store: `python3.12 -m job_agent.hosted`.

The same app as on a laptop, with the platform's differences handled here:
the app folder is read-only (everything goes in the session workspace), models
come from the class broker, the browser is the Chromium unpacked at build time
and reaches the web through netrelay, and nothing survives the session (the
dashboard offers a download of everything).
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
CHROMIUM = APP / "chromium"
EXE = CHROMIUM / "chromium"
TECTONIC = APP / "bin" / "tectonic"
TECTONIC_CACHE = APP / "bin" / "tectonic-cache.tgz"

# The flags @sparticuz/chromium recommends for small sandboxed Linux machines.
COMMON_ARGS = ["--disable-domain-reliability", "--disable-print-preview", "--disk-cache-size=8388608",
               "--no-pings", "--font-render-hinting=none", "--disable-dev-shm-usage",
               "--disable-features=AudioServiceOutOfProcess,IsolateOrigins,site-per-process",
               "--disable-setuid-sandbox", "--no-sandbox",
               "--ignore-certificate-errors"]  # The relay's loopback certificate; sites are verified by the relay.
SOFTWARE_GL = ["--ignore-gpu-blocklist", "--in-process-gpu", "--use-gl=angle", "--use-angle=swiftshader",
               "--enable-unsafe-swiftshader"]
NO_GPU = ["--disable-gpu", "--disable-software-rasterizer"]
SINGLE = ["--single-process", "--no-zygote"]
# Tried in order by the warm-up; the first that loads a job board is used for the session.
# (/dev/shm is tiny in small containers, so every variant avoids it.)
VARIANTS = {
    "single, software GL": COMMON_ARGS + SINGLE + SOFTWARE_GL,
    "single, no GPU": COMMON_ARGS + SINGLE + NO_GPU,
    "single, no GPU, no JIT": COMMON_ARGS + SINGLE + NO_GPU + ["--js-flags=--jitless"],
    "multi, no GPU": COMMON_ARGS + NO_GPU + ["--renderer-process-limit=1"],
    "multi, no GPU, no JIT": COMMON_ARGS + NO_GPU + ["--renderer-process-limit=1", "--js-flags=--jitless"],
}
chosen = next(iter(VARIANTS))


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

    options = {"headless": True}
    proxy = netrelay.browser_proxy()
    if proxy:
        options["proxy"] = proxy
    if (CHROMIUM / "chromium").exists():
        libs = [str(d) for d in CHROMIUM.rglob("lib") if d.is_dir()] + [str(CHROMIUM)]
        options.update(executable_path=str(CHROMIUM / "chromium"), args=VARIANTS[chosen], env={
            **os.environ, "LD_LIBRARY_PATH": ":".join(libs), "FONTCONFIG_PATH": str(CHROMIUM / "fonts")})
    else:  # A laptop rehearsal: Playwright's own Chromium.
        options["args"] = ["--ignore-certificate-errors"]
    return options


def dump_dom(name: str) -> tuple[bool, str]:
    """Run Chromium itself on a job board (no Playwright) and report what it says."""
    import subprocess

    from . import netrelay

    proxy = netrelay.browser_proxy()
    libs = [str(d) for d in CHROMIUM.rglob("lib") if d.is_dir()] + [str(CHROMIUM)]
    env = {**os.environ, "LD_LIBRARY_PATH": ":".join(libs), "FONTCONFIG_PATH": str(CHROMIUM / "fonts")}
    command = [str(EXE), *VARIANTS[name], "--headless",
               *([f"--proxy-server={proxy['server']}"] if proxy else []),
               "--virtual-time-budget=8000", "--dump-dom", "https://job-boards.greenhouse.io/anthropic"]
    start = time.time()
    try:
        run = subprocess.run(command, capture_output=True, text=True, timeout=90, env=env)
    except subprocess.TimeoutExpired:
        return False, f"Direct [{name}]: timed out."
    links = run.stdout.count('/jobs/')
    noise = ("Fontconfig", "dbus", "DevTools listening", ":INFO:", "CONSOLE")
    lines = [l for l in run.stderr.splitlines() if l.strip() and not any(n in l for n in noise)]
    # A crash's last words, or else only real errors.
    errors = lines if run.returncode else [l for l in lines if "FATAL" in l or "Check failed" in l]
    return links > 0, (f"Direct [{name}]: exit {run.returncode}, {len(run.stdout) // 1024} KB, {links} job links, "
                       f"{time.time() - start:.0f}s" + (f", says: {' / '.join(errors[-3:])[:300]}" if errors else "") + ".")


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
    from . import config, latex

    global chosen
    notes = [f"Memory free: {free_mb()} MB."]
    working = None
    order = list(VARIANTS)
    if EXE.exists():
        passing = []
        for name in VARIANTS:
            ok, note = dump_dom(name)
            notes.append(note)
            if ok:
                passing.append(name)
        order = passing + [n for n in VARIANTS if n not in passing]
    for name in order[:2]:
        chosen = name
        start, browser = time.time(), None
        try:
            browser = ctx.playwright.chromium.launch(**chromium_options())
            page = browser.new_page()
            response = page.goto("https://job-boards.greenhouse.io/anthropic", wait_until="load", timeout=60000)
            links = page.locator('a[href*="/jobs/"]').count()
            notes.append(f"Browser [{name}]: HTTP {response.status if response else '?'}, "
                         f"{len(page.content()) // 1024} KB, {links} job links, {time.time() - start:.0f}s, "
                         f"memory free {free_mb()} MB.")
            if links:
                working = name
        except Exception as error:  # noqa: BLE001
            notes.append(f"Browser [{name}] failed: {str(error).splitlines()[0][:120]}")
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:  # noqa: BLE001
                    pass
        if working:
            break
    chosen = working or next(iter(VARIANTS))
    if not working:
        from . import netrelay

        relay = netrelay._relay
        notes.append("Browser can't load job sites." + (f" Relay: {relay.errors[-3:]}" if relay and relay.errors else ""))
    start = time.time()
    try:
        tex = latex.preamble(1) + "\n\\begin{document}\n\\section{Warm-up}\nReady.\n\\end{document}\n"
        latex.compile_pdf(tex, config.DATA_DIR / "warm-up.pdf")
        notes.append(f"Resume typesetting OK ({time.time() - start:.0f}s).")
    except Exception as error:  # noqa: BLE001
        from . import netrelay

        mirror = netrelay._mirror
        notes.append(f"Resume typesetting failed: {str(error)[:300]}"
                     + (f" · mirror: {mirror.errors[-2:]}" if mirror and mirror.errors else ""))
    log(" ".join(notes))
    return " ".join(notes)


def main() -> None:
    prepare_environment()
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
