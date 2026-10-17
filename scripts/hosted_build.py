"""Class app store build step: check the code compiles and make the bundled Tectonic
executable. Chromium comes with the platform's "browser" runtime profile
(/usr/bin/chromium, WORKSHOP_BROWSER_EXECUTABLE), so nothing is downloaded here.
Writes build-report.json; the dashboard's warm-up task reports whether everything works."""

import json
import os
import py_compile
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "build-report.json"
report = {"started": time.strftime("%Y-%m-%dT%H:%M:%S"), "python": sys.version.split()[0]}


def run(cmd, timeout=600, cwd=None):
    start = time.time()
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return {"ok": done.returncode == 0, "seconds": round(time.time() - start, 1),
                "out": (done.stdout + done.stderr)[-800:]}
    except Exception as error:  # noqa: BLE001
        return {"ok": False, "error": f"{type(error).__name__}: {error}"}


try:
    for module in (ROOT / "job_agent").glob("*.py"):
        py_compile.compile(str(module), doraise=True)
    report["compile"] = "ok"
    browser = os.environ.get("WORKSHOP_BROWSER_EXECUTABLE") or shutil.which("chromium")
    report["chromium"] = run([browser, "--version"], 30).get("out", "").strip() if browser else "not in this image"

    tectonic = ROOT / "bin" / "tectonic"
    if tectonic.exists():
        tectonic.chmod(0o755)  # In case unzipping dropped the executable bit.
    report["tectonic in zip"] = {"present": tectonic.exists(),
                                 "executable": os.access(tectonic, os.X_OK) if tectonic.exists() else False,
                                 "version": run([str(tectonic), "--version"], 30).get("out", "").strip()[:200]}
    report["app folder free MB after"] = shutil.disk_usage(ROOT).free // 2**20
except Exception as error:  # noqa: BLE001
    report["build error"] = f"{type(error).__name__}: {error}"
finally:
    report["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        REPORT.write_text(json.dumps(report, indent=1))
    except OSError as error:
        print("could not write report:", error)
    print(json.dumps(report, indent=1)[-4000:])
sys.exit(0)
