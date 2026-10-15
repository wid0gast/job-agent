"""Platform probe: what the class app store gives a Python app.

Standard library only. Open the page, press "Run checks", then the optional
install trials, and copy the results. Reports environment variable names,
never their values.
"""

import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import traceback
import urllib.request
import uuid
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
WORK = Path(os.environ.get("WORKSHOP_WORKDIR") or os.environ.get("PROBE_WORKDIR") or "/tmp") / "probe"
STARTED = time.time()
RESULTS: dict = {}
LOCK = threading.Lock()
ARCH = "aarch64" if platform.machine().lower() in ("aarch64", "arm64") else "x86_64"
TECTONIC_URL = ("https://github.com/tectonic-typesetting/tectonic/releases/download/"
                f"tectonic%400.17.0/tectonic-0.17.0-{ARCH}-unknown-linux-musl.tar.gz")
TARGETS = {
    "Simplify list (GitHub raw)": ("GET", "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/README.md"),
    "Greenhouse API": ("GET", "https://boards-api.greenhouse.io/v1/boards/anthropic/jobs"),
    "Greenhouse forms": ("GET", "https://job-boards.greenhouse.io/anthropic"),
    "Lever API": ("GET", "https://api.lever.co/v0/postings/zoox?mode=json&limit=1"),
    "Ashby API": ("GET", "https://api.ashbyhq.com/posting-api/job-board/cohere"),
    "Ashby forms": ("GET", "https://jobs.ashbyhq.com/cohere"),
    "Workday site": ("GET", "https://adobe.wd5.myworkdayjobs.com/external_experienced"),
    "PyPI (pip)": ("GET", "https://pypi.org/simple/playwright/"),
    "Playwright browser CDN": ("GET", "https://cdn.playwright.dev/"),
    "GitHub releases (Tectonic)": ("GET", "https://github.com/tectonic-typesetting/tectonic/releases"),
    "Tectonic package bundle": ("GET", "https://relay.fullyjustified.net/"),
    "OpenRouter": ("GET", "https://openrouter.ai/api/v1/models"),
}


def record(section: str, key: str, value) -> None:
    with LOCK:
        RESULTS.setdefault(section, {})[key] = value


def run(cmd: list[str], timeout: int = 600, env: dict | None = None) -> dict:
    start = time.time()
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              env={**os.environ, **(env or {})})
        return {"ok": done.returncode == 0, "code": done.returncode, "seconds": round(time.time() - start, 1),
                "out": (done.stdout[-1500:] + done.stderr[-1500:]).strip()[-2000:]}
    except Exception as error:  # noqa: BLE001
        return {"ok": False, "seconds": round(time.time() - start, 1), "error": f"{type(error).__name__}: {error}"}


# ----- checks -----------------------------------------------------------------

def check_system() -> None:
    record("system", "python", sys.version.split()[0])
    record("system", "platform", platform.platform())
    record("system", "machine", platform.machine())
    record("system", "cpus", os.cpu_count())
    try:
        mem = {l.split(":")[0]: l.split(":")[1].strip() for l in open("/proc/meminfo") if ":" in l}
        record("system", "memory", {k: mem.get(k) for k in ("MemTotal", "MemAvailable")})
    except OSError:
        record("system", "memory", "unknown")
    for place in (HERE, Path("/tmp"), Path.home()):
        try:
            usage = shutil.disk_usage(place)
            test = place / f".probe-{uuid.uuid4().hex[:6]}"
            test.write_text("x")
            test.unlink()
            writable = True
        except Exception:  # noqa: BLE001
            usage, writable = None, False
        record("disk", str(place), {"free_mb": usage.free // 2**20 if usage else None, "writable": writable})
    record("system", "user", os.environ.get("USER") or os.getuid())
    record("system", "env names", sorted(os.environ))
    record("system", "seconds since start", round(time.time() - STARTED))


def check_packages() -> None:
    for name in ("playwright", "httpx", "yaml", "pypdf", "keyring", "markdown"):
        try:
            module = __import__(name)
            record("packages", name, getattr(module, "__version__", "installed"))
        except Exception as error:  # noqa: BLE001
            record("packages", name, f"missing ({type(error).__name__})")
    record("packages", "pip", run([sys.executable, "-m", "pip", "--version"], 60))
    for tool in ("uv", "chromium", "chromium-browser", "google-chrome", "tectonic", "pdflatex",
                 "xelatex", "node", "git", "tar", "fc-list"):
        record("tools", tool, shutil.which(tool) or "not found")
    cache = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".cache/ms-playwright")
    record("tools", "playwright browsers dir", sorted(p.name for p in cache.iterdir()) if cache.exists() else f"{cache} missing")
    fonts = run(["fc-list", ":family"], 30) if shutil.which("fc-list") else {"out": "no fc-list"}
    record("tools", "fonts with Charter", [l for l in fonts.get("out", "").splitlines() if "harter" in l][:5])


def fetch_status(url: str, method: str = "GET", timeout: int = 25) -> dict:
    start = time.time()
    try:
        request = urllib.request.Request(url, method=method, headers={"User-Agent": "Mozilla/5.0 probe"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(2048)
            return {"status": response.status, "ms": round(1000 * (time.time() - start))}
    except urllib.error.HTTPError as error:
        return {"status": error.code, "ms": round(1000 * (time.time() - start))}
    except Exception as error:  # noqa: BLE001
        return {"error": f"{type(error).__name__}: {error}"[:160], "ms": round(1000 * (time.time() - start))}


def check_network() -> None:
    for name, (method, url) in TARGETS.items():  # One at a time: is anything blocked?
        result = fetch_status(url, method)
        if "429" in str(result):
            time.sleep(3)
            result = {"first": result, "retry": fetch_status(url, method)}
        record("network (one at a time)", name, result)
    burst = [fetch_status("https://api.lever.co/v0/postings/zoox?mode=json&limit=1") for _ in range(8)]
    record("network limits", "8 quick requests in a row", [r.get("status") or r.get("error", "")[:60] for r in burst])
    results = []

    def one():
        results.append(fetch_status("https://api.ashbyhq.com/posting-api/job-board/cohere"))

    threads = [threading.Thread(target=one) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(40)
    record("network limits", "4 at the same time", [r.get("status") or r.get("error", "")[:60] for r in results])


def check_browser(label: str = "browser") -> None:
    script = r"""
import json, time
from playwright.sync_api import sync_playwright
out = {}
with sync_playwright() as p:
    start = time.time()
    b = p.chromium.launch()
    out["launch_s"] = round(time.time() - start, 1)
    page = b.new_page()
    page.goto("https://job-boards.greenhouse.io/anthropic", wait_until="domcontentloaded", timeout=30000)
    out["title"] = page.title()
    out["screenshot_bytes"] = len(page.screenshot())
    page.set_content("<p>pdf test</p>")
    out["pdf_bytes"] = len(page.pdf())
    out["version"] = b.version
    b.close()
print(json.dumps(out))
"""
    result = run([sys.executable, "-c", script], 180, deps_env())
    record(label, "headless chromium", result)


BOOT: dict = {}
SECRET = ("token", "secret", "password", "key", "auth", "cookie", "credential", "signature")


def hide_secrets(value, name=""):
    if isinstance(value, dict):
        return {k: hide_secrets(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [hide_secrets(v, name) for v in value]
    if isinstance(value, str) and (any(s in name.lower() for s in SECRET) or
                                   (len(value) > 40 and " " not in value and not value.startswith("http"))):
        return f"<hidden, {len(value)} chars>"
    if isinstance(value, str) and "token=" in value:
        return value.split("token=")[0] + "token=<hidden>"
    return value


def check_platform_browser() -> None:
    """If the platform offers a browser endpoint, try driving it."""
    browser = BOOT.get("browser")
    if not browser:
        record("platform browser", "offered", "no browser field")
        return
    urls = [v for v in json.dumps(browser).replace('"', " ").split() if v.startswith(("ws://", "wss://", "http://", "https://"))]
    record("platform browser", "endpoints found", [u.split("?")[0] for u in urls])
    script = r"""
import json, sys, time
from playwright.sync_api import sync_playwright
url = sys.argv[1]
out = {}
with sync_playwright() as p:
    start = time.time()
    try:
        b = p.chromium.connect_over_cdp(url, timeout=30000); out["how"] = "cdp"
    except Exception as e:
        out["cdp_error"] = str(e)[:200]
        b = p.chromium.connect(url, timeout=30000); out["how"] = "playwright"
    out["connect_s"] = round(time.time() - start, 1)
    ctx = b.contexts[0] if b.contexts else b.new_context()
    page = ctx.new_page()
    page.goto("https://job-boards.greenhouse.io/anthropic", wait_until="domcontentloaded", timeout=45000)
    out["title"] = page.title()
    out["screenshot_bytes"] = len(page.screenshot())
    b.close()
print(json.dumps(out))
"""
    for url in urls[:2]:
        record("platform browser", url.split("?")[0], run([sys.executable, "-c", script, url], 150, deps_env()))


def deps_env() -> dict:
    env = {}
    if (WORK / "deps").exists():
        env["PYTHONPATH"] = str(WORK / "deps")
    if (WORK / "browsers").exists():
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(WORK / "browsers")
    return env


def check_broker() -> None:
    url, token = os.environ.get("WORKSHOP_BROKER_URL"), os.environ.get("WORKSHOP_RUN_TOKEN")
    if not url or not token:
        record("models", "broker", "WORKSHOP_BROKER_URL / WORKSHOP_RUN_TOKEN not set")
        return
    split = urlsplit(url)

    def call(path, body=None, profile=None):
        conn = (HTTPSConnection if split.scheme == "https" else HTTPConnection)(split.hostname, split.port, timeout=120)
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json",
                   "X-Workshop-Contract-Version": "workshop.app.v1"}
        if body is not None:
            headers.update({"X-Workshop-Operation-ID": str(uuid.uuid4()), "X-Workshop-Expected-Profile": profile,
                            "X-Workshop-Attempt-Limit": "1"})
        conn.request("GET" if body is None else "POST", path, None if body is None else json.dumps(body), headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        try:
            return response.status, json.loads(data or b"{}")
        except ValueError:
            return response.status, {"raw": data[:300].decode(errors="replace")}

    status, boot = call("/v1/bootstrap")
    selection = (boot.get("inference") or {}).get("selection") or {}
    record("platform", "bootstrap (secrets hidden)", hide_secrets(boot))
    if boot.get("expiresAt"):
        try:
            from datetime import datetime, timezone
            left = datetime.fromisoformat(boot["expiresAt"].replace("Z", "+00:00")) - datetime.now(timezone.utc)
            record("platform", "visit minutes left", round(left.total_seconds() / 60, 1))
        except ValueError:
            pass
    BOOT.update(boot)
    record("models", "bootstrap", {"status": status, "mode": boot.get("mode"), "selection": selection,
                                   "keys": sorted(boot)})
    if status != 200 or not selection:
        return
    base = {"model": selection.get("modelId"), "max_tokens": 200,
            "messages": [{"role": "user", "content": 'Reply with the JSON {"ok": true} only.'}]}
    tool = {"type": "function", "function": {"name": "noop", "description": "Do nothing.",
                                             "parameters": {"type": "object", "properties": {}}}}
    schema = {"type": "json_schema", "json_schema": {"name": "x", "strict": True, "schema": {
        "type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}}}
    trials = {
        "plain": {},
        "tools": {"tools": [tool]},
        "response_format json_schema": {"response_format": schema},
        "response_format json_object": {"response_format": {"type": "json_object"}},
        "reasoning max_tokens": {"reasoning": {"max_tokens": 256}},
        "provider data_collection": {"provider": {"data_collection": "deny"}},
        "models fallback list": {"models": [selection.get("modelId")]},
    }
    for name, extra in trials.items():
        try:
            status, reply = call("/v1/llm/chat/completions", {**base, **extra}, selection.get("profileId"))
            choice = (reply.get("choices") or [{}])[0]
            record("models", name, {"status": status, "finish": choice.get("finish_reason"),
                                    "content": str((choice.get("message") or {}).get("content"))[:120],
                                    "error": str(reply.get("error", ""))[:200], "usage": reply.get("usage")})
        except Exception as error:  # noqa: BLE001
            record("models", name, {"error": f"{type(error).__name__}: {error}"[:200]})


# ----- install trials ---------------------------------------------------------

def trial_pip() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    record("install trials", "uv pip install into the workspace",
           run(["uv", "pip", "install", "--quiet", "--python", sys.executable, "--target", str(WORK / "deps"),
                "playwright", "httpx", "pypdf", "pyyaml"], 600, {"UV_CACHE_DIR": str(WORK / "uv-cache")}))
    record("install trials", "workspace free MB after", shutil.disk_usage(WORK).free // 2**20)


def trial_chromium() -> None:
    env = {"PYTHONPATH": str(WORK / "deps"), "PLAYWRIGHT_BROWSERS_PATH": str(WORK / "browsers")}
    record("install trials", "playwright install chromium (headless shell)",
           run([sys.executable, "-m", "playwright", "install", "--only-shell", "chromium"], 900, env))
    record("install trials", "workspace free MB after", shutil.disk_usage(WORK).free // 2**20)
    record("install trials", "playwright install-deps (needs root)",
           run([sys.executable, "-m", "playwright", "install-deps", "chromium"], 300, env))
    check_browser("browser after install")


def trial_tectonic() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    start = time.time()
    try:
        archive = WORK / "tectonic.tar.gz"
        urllib.request.urlretrieve(TECTONIC_URL, archive)
        with tarfile.open(archive) as tar:
            tar.extractall(WORK)
        binary = WORK / "tectonic"
        binary.chmod(0o755)
        record("install trials", "tectonic download", {"ok": True, "seconds": round(time.time() - start, 1)})
        doc = WORK / "probe.tex"
        doc.write_text("\\documentclass{article}\\usepackage[T1]{fontenc}\\usepackage{charter}"
                       "\\usepackage{fontawesome5}\\begin{document}\\faGithub\\ Probe page.\\end{document}\n")
        record("install trials", "tectonic compile (first run downloads packages)",
               run([str(binary), "-X", "compile", "--outdir", str(WORK), str(doc)], 600))
    except Exception as error:  # noqa: BLE001
        record("install trials", "tectonic download", {"ok": False, "error": f"{type(error).__name__}: {error}"})


ROOT = HERE.parent


CHROMIUM_ARGS = ["--disable-domain-reliability", "--disable-print-preview", "--disk-cache-size=33554432",
                 "--no-pings", "--single-process", "--font-render-hinting=none",
                 "--disable-features=AudioServiceOutOfProcess,IsolateOrigins,site-per-process",
                 "--ignore-gpu-blocklist", "--in-process-gpu", "--use-gl=angle", "--use-angle=swiftshader",
                 "--enable-unsafe-swiftshader", "--disable-setuid-sandbox", "--no-sandbox", "--no-zygote"]


def chromium_env() -> dict:
    base = ROOT / "chromium"
    libs = [str(d) for d in base.rglob("lib") if d.is_dir()] + [str(base)]
    fonts = next((str(f.parent) for f in base.rglob("fonts.conf")), str(base / "fonts"))
    WORK.mkdir(parents=True, exist_ok=True)
    return {"LD_LIBRARY_PATH": ":".join(libs), "FONTCONFIG_PATH": fonts, "HOME": str(WORK)}


def check_build() -> None:
    """What the build installed, and whether Chromium and Tectonic run here."""
    report = ROOT / "build-report.json"
    record("build", "report", json.loads(report.read_text()) if report.exists() else "no build-report.json")
    exe = ROOT / "chromium" / "chromium"
    if exe.exists():
        env = chromium_env()
        record("chromium", "environment", env)
        record("chromium", "missing libraries", run(["sh", "-c", f"ldd '{exe}' | grep 'not found' || echo none"], 30, env))
        record("chromium", "version", run([str(exe), "--version"], 30, env))
        script = r"""
import json, sys, time
from playwright.sync_api import sync_playwright
out = {}
with sync_playwright() as p:
    start = time.time()
    import os
    from urllib.parse import unquote, urlsplit
    raw = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
    proxy = None
    if raw:
        u = urlsplit(raw)
        proxy = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
        if u.username:
            proxy.update(username=unquote(u.username), password=unquote(u.password or ""))
        bypass = os.environ.get("NO_PROXY") or os.environ.get("no_proxy")
        if bypass:
            proxy["bypass"] = bypass
    out["proxy"] = {"set": bool(proxy), "has_credentials": bool(proxy and proxy.get("username"))}
    b = p.chromium.launch(executable_path=sys.argv[1], args=json.loads(sys.argv[2]), proxy=proxy)
    out["launch_s"] = round(time.time() - start, 1)
    out["version"] = b.version
    page = b.new_page()
    start = time.time()
    page.goto("https://job-boards.greenhouse.io/anthropic", wait_until="domcontentloaded", timeout=60000)
    out["goto_s"] = round(time.time() - start, 1)
    out["title"] = page.title()
    out["form_inputs_on_a_job"] = None
    links = page.locator("a[href*='/jobs/']")
    if links.count():
        page.goto(links.first.get_attribute("href"), wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        out["form_inputs_on_a_job"] = page.locator("form input, form textarea").count()
    out["screenshot_bytes"] = len(page.screenshot())
    page.set_content("<h1>PDF test</h1><p>Charter? " + "word " * 50 + "</p>")
    out["pdf_bytes"] = len(page.pdf())
    b.close()
print(json.dumps(out))
"""
        record("chromium", "playwright drives it",
               run([sys.executable, "-c", script, str(exe), json.dumps(CHROMIUM_ARGS)], 240, env))
    else:
        record("chromium", "installed", "no")
    tectonic = ROOT / "probe" / "bin" / "tectonic"
    if tectonic.exists():
        WORK.mkdir(parents=True, exist_ok=True)
        doc = WORK / "resume-test.tex"
        doc.write_text(r"\documentclass[a4paper,10pt]{article}\usepackage[T1]{fontenc}\usepackage{charter}"
                       r"\usepackage{fontawesome5}\usepackage{academicons}\usepackage{titlesec}\usepackage{enumitem}"
                       r"\usepackage{soul}\usepackage{hyperref}\usepackage{fancyhdr}\usepackage[empty]{fullpage}"
                       r"\usepackage{needspace}\usepackage{bibentry}\usepackage{hanging}\usepackage{multicol}"
                       r"\begin{document}\faGithub\ \aiGoogleScholar\ \so{RESUME TEST}\end{document}" "\n")
        env = {"TECTONIC_CACHE_DIR": str(WORK / "tectonic-cache"), "HOME": str(WORK)}
        record("tectonic", "first compile (downloads LaTeX packages)",
               run([str(tectonic), "-X", "compile", "--outdir", str(WORK), str(doc)], 600, env))
        record("tectonic", "second compile (cached)",
               run([str(tectonic), "-X", "compile", "--outdir", str(WORK), str(doc)], 300, env))
        record("tectonic", "pdf made", (WORK / "resume-test.pdf").exists())
        cache = WORK / "tectonic-cache"
        record("tectonic", "cache MB", round(sum(f.stat().st_size for f in cache.rglob("*") if f.is_file()) / 2**20, 1)
               if cache.exists() else 0)
    else:
        record("tectonic", "installed", "no")


# ----- proxy relay ------------------------------------------------------------

import base64
import select
import socket
from urllib.parse import unquote


def upstream_proxy():
    raw = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
    if not raw:
        return None
    u = urlsplit(raw)
    auth = None
    if u.username:
        auth = "Basic " + base64.b64encode(f"{unquote(u.username)}:{unquote(u.password or '')}".encode()).decode()
    return {"host": u.hostname, "port": u.port or 80, "scheme": u.scheme, "auth": auth}


def check_proxy_shape() -> None:
    raw = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
    u = urlsplit(raw) if raw else None
    record("proxy", "shape", {"set": bool(raw), "scheme": u.scheme if u else None, "port": u.port if u else None,
                              "has_username": bool(u and u.username), "has_password": bool(u and u.password),
                              "no_proxy_entries": len((os.environ.get("NO_PROXY") or "").split(","))})
    up = upstream_proxy()
    if not up:
        return
    for label, auth in (("CONNECT without login", None), ("CONNECT with URL login (Basic)", up["auth"])):
        try:
            sock = socket.create_connection((up["host"], up["port"]), timeout=15)
            request = "CONNECT job-boards.greenhouse.io:443 HTTP/1.1\r\nHost: job-boards.greenhouse.io:443\r\n"
            if auth:
                request += f"Proxy-Authorization: {auth}\r\n"
            sock.sendall((request + "\r\n").encode())
            head = sock.recv(4096).decode(errors="replace")
            sock.close()
            lines = head.split("\r\n")
            schemes = [l.split(":", 1)[1].strip().split(" ")[0] for l in lines if l.lower().startswith("proxy-authenticate")]
            record("proxy", label, {"status": lines[0], "login methods offered": schemes})
        except Exception as error:  # noqa: BLE001
            record("proxy", label, f"{type(error).__name__}: {error}"[:200])


class Relay(threading.Thread):
    """A local proxy with no login that forwards CONNECT tunnels to the platform proxy."""

    def __init__(self):
        super().__init__(daemon=True)
        self.server = socket.socket()
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(64)
        self.port = self.server.getsockname()[1]
        self.up = upstream_proxy()
        self.served = 0
        self.log = []

    def run(self):
        while True:
            client, _ = self.server.accept()
            threading.Thread(target=self.handle, args=(client,), daemon=True).start()

    def handle(self, client):
        try:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = client.recv(4096)
                if not chunk:
                    return
                head += chunk
            first = head.split(b"\r\n", 1)[0].decode()
            method, target, _ = first.split(" ", 2)
            upstream = socket.create_connection((self.up["host"], self.up["port"]), timeout=30)
            if method == "CONNECT":
                request = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
                if self.up["auth"]:
                    request += f"Proxy-Authorization: {self.up['auth']}\r\n"
                upstream.sendall((request + "\r\n").encode())
                reply = b""
                while b"\r\n\r\n" not in reply:
                    chunk = upstream.recv(4096)
                    if not chunk:
                        break
                    reply += chunk
                client.sendall(reply.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n")
                if b" 200" not in reply.split(b"\r\n", 1)[0]:
                    return
            else:  # Plain http: forward the request with the login added.
                extra = f"Proxy-Authorization: {self.up['auth']}\r\n".encode() if self.up["auth"] else b""
                line_end = head.index(b"\r\n") + 2
                upstream.sendall(head[:line_end] + extra + head[line_end:])
            self.served += 1
            self.log.append(pipe(client, upstream))
        except Exception:  # noqa: BLE001
            pass
        finally:
            client.close()


def pipe(a, b):
    """Copy both ways; report bytes each way and which side closed first."""
    sockets, sent, closed_by = [a, b], {"to_site": 0, "to_browser": 0}, "timeout"
    while True:
        ready, _, errored = select.select(sockets, [], sockets, 120)
        if errored or not ready:
            break
        for s in ready:
            try:
                data = s.recv(65536)
            except OSError as error:
                return {**sent, "closed_by": f"error {error}"}
            if not data:
                return {**sent, "closed_by": "browser" if s is a else "proxy/site"}
            (b if s is a else a).sendall(data)
            sent["to_site" if s is a else "to_browser"] += len(data)
    return {**sent, "closed_by": closed_by}


def check_tls_through_proxy() -> None:
    """Open TLS through the proxy tunnel from Python: who issued the certificate?"""
    import ssl
    up = upstream_proxy()
    if not up:
        return
    for verify in (True, False):
        try:
            sock = socket.create_connection((up["host"], up["port"]), timeout=15)
            sock.sendall((f"CONNECT job-boards.greenhouse.io:443 HTTP/1.1\r\nHost: job-boards.greenhouse.io:443\r\n"
                          f"Proxy-Authorization: {up['auth']}\r\n\r\n").encode())
            head = b""
            while b"\r\n\r\n" not in head:
                head += sock.recv(4096)
            context = ssl.create_default_context()
            if not verify:
                context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
            tls = context.wrap_socket(sock, server_hostname="job-boards.greenhouse.io")
            cert = tls.getpeercert(binary_form=not verify)
            issuer = dict(x[0] for x in cert["issuer"]) if verify else "(unverified)"
            tls.sendall(b"GET /anthropic HTTP/1.1\r\nHost: job-boards.greenhouse.io\r\nConnection: close\r\n\r\n")
            first = tls.recv(200).split(b"\r\n", 1)[0].decode(errors="replace")
            record("tls through proxy", "verified" if verify else "unverified",
                   {"tunnel": head.split(b"\r\n", 1)[0].decode(), "issuer": issuer, "first line": first})
            tls.close()
        except Exception as error:  # noqa: BLE001
            record("tls through proxy", "verified" if verify else "unverified", f"{type(error).__name__}: {error}"[:300])


def check_chromium_via_relay() -> None:
    exe = ROOT / "chromium" / "chromium"
    if not exe.exists() or not upstream_proxy():
        record("chromium variants", "skipped", "no chromium or no proxy")
        return
    relay = Relay()
    relay.start()
    script = r"""
import json, os, sys, time
from urllib.parse import unquote, urlsplit
from playwright.sync_api import sync_playwright
exe, args, port, mode = sys.argv[1], json.loads(sys.argv[2]), sys.argv[3], sys.argv[4]
out = {"mode": mode}
u = urlsplit(os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"))
upstream = {"server": f"http://{u.hostname}:{u.port}", "username": unquote(u.username or ""), "password": unquote(u.password or "")}
with sync_playwright() as p:
    if mode == "relay":
        b = p.chromium.launch(executable_path=exe, args=args, proxy={"server": f"http://127.0.0.1:{port}"})
        context = b.new_context()
    elif mode == "relay + accept proxy certificate":
        b = p.chromium.launch(executable_path=exe, args=args + ["--ignore-certificate-errors"],
                              proxy={"server": f"http://127.0.0.1:{port}"})
        context = b.new_context(ignore_https_errors=True)
    else:  # Playwright fetches every request through the proxy; the browser makes no connections.
        b = p.chromium.launch(executable_path=exe, args=args)
        context = b.new_context()
        fetcher = p.request.new_context(proxy=upstream, ignore_https_errors=True)
        def route(r):
            try:
                resp = fetcher.fetch(r.request, max_redirects=0, timeout=30000)
                r.fulfill(response=resp)
            except Exception as e:
                r.abort()
        context.route("**/*", route)
    page = context.new_page()
    start = time.time()
    try:
        page.goto("https://job-boards.greenhouse.io/anthropic", wait_until="domcontentloaded", timeout=60000)
        out["goto_s"] = round(time.time() - start, 1)
        out["title"] = page.title()
        links = page.locator("a[href*='/jobs/']")
        out["job_links"] = links.count()
        if links.count():
            href = links.first.get_attribute("href")
            page.goto(href if href.startswith("http") else "https://job-boards.greenhouse.io" + href,
                      wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)
            out["form_fields_on_a_job"] = page.locator("form input, form textarea").count()
        out["ok"] = True
    except Exception as e:
        out["error"] = str(e).splitlines()[0][:200]
    b.close()
print(json.dumps(out))
"""
    for mode in ("relay", "relay + accept proxy certificate", "playwright fetches through proxy"):
        result = run([sys.executable, "-c", script, str(exe), json.dumps(CHROMIUM_ARGS), str(relay.port), mode],
                     240, chromium_env())
        line = (result.get("out") or "").strip().splitlines()[-1:] or [""]
        try:
            summary = json.loads(line[0])
        except ValueError:
            summary = {"crashed": (result.get("out") or result.get("error", ""))[-400:]}
        record("chromium variants", mode, summary)
    record("chromium variants", "relay tunnels", relay.log[:6])

JOBS = {"checks": [check_proxy_shape, check_tls_through_proxy, check_chromium_via_relay, check_build, check_system, check_packages, check_broker, check_platform_browser, check_browser, check_network],
        "pip": [trial_pip], "chromium": [trial_chromium], "tectonic": [trial_tectonic]}
RUNNING: set = set()


def start(job: str) -> None:
    if job in RUNNING:
        return
    RUNNING.add(job)

    def go():
        try:
            for step in JOBS[job]:
                try:
                    step()
                except Exception:  # noqa: BLE001
                    record("probe errors", step.__name__, traceback.format_exc()[-800:])
        finally:
            RUNNING.discard(job)

    threading.Thread(target=go, daemon=True).start()


PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Platform probe</title>
<style>body{font:14px system-ui;max-width:900px;margin:24px auto;padding:0 16px}button{font:inherit;padding:7px 12px;margin:4px 6px 4px 0}
pre,textarea{background:#f4f4f2;padding:12px;white-space:pre-wrap;font:12px ui-monospace,monospace;border-radius:8px;width:100%;box-sizing:border-box}</style>
<h1>Platform probe</h1>
<p>Probe v8. 1. Run checks (about 2 minutes). 2. Try the installs, one at a time. 3. Copy the results and send them over.</p>
<button data-job="checks">Run checks</button><button data-job="pip">Try package install</button>
<button data-job="chromium">Try Chromium download</button><button data-job="tectonic">Try Tectonic (LaTeX)</button>
<button id=copy>Copy results</button> <span id=state>Loading…</span>
<textarea id=out rows=30 readonly>Not run yet.</textarea>
<script src="/probe.js"></script>"""

SCRIPT = """
const out = document.getElementById('out'), state = document.getElementById('state');
async function poll() {
  try {
    const r = await (await fetch('/results', {cache: 'no-store'})).json();
    out.value = JSON.stringify(r.results, null, 1);
    state.textContent = r.running.length ? 'Running: ' + r.running.join(', ') + '…' : 'Idle.';
    if (r.running.length) setTimeout(poll, 2000);
  } catch (e) { state.textContent = 'Error talking to the app: ' + e; }
}
document.querySelectorAll('button[data-job]').forEach((b) => b.addEventListener('click', async () => {
  state.textContent = 'Starting ' + b.dataset.job + '…';
  try { await fetch('/run?job=' + b.dataset.job, {method: 'POST'}); } catch (e) { state.textContent = 'Error: ' + e; return; }
  setTimeout(poll, 500);
}));
document.getElementById('copy').addEventListener('click', async () => {
  out.select();
  try { await navigator.clipboard.writeText(out.value); state.textContent = 'Copied.'; }
  catch (e) { document.execCommand('copy'); state.textContent = 'Selected: press Cmd+C if it did not copy.'; }
});
poll();
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, body: bytes, kind: str = "text/html; charset=utf-8", status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            return self.send(b"ok", "text/plain")
        if self.path == "/probe.js":
            return self.send(SCRIPT.encode(), "text/javascript; charset=utf-8")
        if self.path == "/results":
            with LOCK:
                body = json.dumps({"results": RESULTS, "running": sorted(RUNNING)}, default=str)
            return self.send(body.encode(), "application/json")
        self.send(PAGE.encode())

    def do_POST(self):
        if self.path.startswith("/run?job="):
            job = self.path.split("=", 1)[1]
            if job in JOBS:
                start(job)
                return self.send(b"{}", "application/json")
        self.send(b"{}", "application/json", 404)


def main():
    port = int(os.environ.get("PORT", "8080"))
    print(f"Probe on port {port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
