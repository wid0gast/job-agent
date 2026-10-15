"""Build step (probe v5): get Chromium and Tectonic from the npm registry, since the
build may only reach package registries. Unpacks them inside the app folder,
where programs may run. Never fails the build: writes build-report.json."""

import io
import json
import os
import py_compile
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "build-report.json"
CHROMIUM = ROOT / "chromium"
BIN = ROOT / "bin"
REGISTRY = "https://registry.npmjs.org"
PACKAGES = {"chromium": "@sparticuz/chromium"}
report = {"started": time.strftime("%Y-%m-%dT%H:%M:%S"), "python": sys.version.split()[0]}

# Brotli is in Node's standard library; Python has no built-in decoder.
UNBROTLI = r"""
const fs = require('fs'), zlib = require('zlib'), { pipeline } = require('stream');
pipeline(fs.createReadStream(process.argv[2]), zlib.createBrotliDecompress(), fs.createWriteStream(process.argv[3]),
  (err) => { if (err) { console.error(err); process.exit(1); } });
"""


def run(cmd, timeout=600, cwd=None):
    start = time.time()
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return {"ok": done.returncode == 0, "seconds": round(time.time() - start, 1),
                "out": (done.stdout + done.stderr)[-800:]}
    except Exception as error:  # noqa: BLE001
        return {"ok": False, "error": f"{type(error).__name__}: {error}"}


def tarball(name: str) -> bytes:
    """The package's .tgz, straight from the registry (or via npm pack)."""
    errors = []
    try:
        with urllib.request.urlopen(f"{REGISTRY}/{name.replace('/', '%2F')}/latest", timeout=60) as r:
            url = json.load(r)["dist"]["tarball"]
        with urllib.request.urlopen(url, timeout=600) as r:
            return r.read()
    except Exception as error:  # noqa: BLE001
        errors.append(f"direct: {type(error).__name__}: {error}"[:200])
    if shutil.which("npm"):
        work = ROOT / ".npm-pack"
        work.mkdir(exist_ok=True)
        result = run(["npm", "pack", name, "--silent"], 600, cwd=str(work))
        packed = sorted(work.glob("*.tgz"))
        if result["ok"] and packed:
            data = packed[-1].read_bytes()
            shutil.rmtree(work, ignore_errors=True)
            return data
        errors.append(f"npm pack: {result}"[:300])
    else:
        errors.append("npm not installed")
    raise RuntimeError(" | ".join(errors))


def members(data: bytes, prefix: str):
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for member in tar.getmembers():
            if member.isfile() and member.name.startswith(prefix):
                yield member.name, tar.extractfile(member).read()


try:
    py_compile.compile(str(ROOT / "probe/app.py"), doraise=True)
    report["compile"] = "ok"
    report["npm"] = shutil.which("npm") or "not installed"
    report["node"] = run(["node", "--version"], 30).get("out", "").strip()

    # Chromium: chromium.br is the program; the .tar.br files hold libraries, fonts and graphics.
    try:
        start = time.time()
        data = tarball(PACKAGES["chromium"])
        report["chromium package MB"] = round(len(data) / 2**20, 1)
        CHROMIUM.mkdir(exist_ok=True)
        helper = CHROMIUM / "unbrotli.js"
        helper.write_text(UNBROTLI)
        for name, content in members(data, "package/bin/"):
            packed = CHROMIUM / Path(name).name
            packed.write_bytes(content)
            target = CHROMIUM / packed.name.removesuffix(".br")
            step = run(["node", str(helper), str(packed), str(target)], 600)
            packed.unlink()
            if not step["ok"]:
                report[f"unpack {packed.name}"] = step
                continue
            if target.suffix == ".tar":
                dest = CHROMIUM if "swiftshader" in target.name else CHROMIUM / target.stem
                dest.mkdir(exist_ok=True)
                report[f"untar {target.name}"] = run(["tar", "-xf", str(target), "-C", str(dest)], 300)["ok"]
                target.unlink()
            else:
                target.chmod(0o755)
        helper.unlink()
        report["chromium"] = {"ok": (CHROMIUM / "chromium").exists(), "seconds": round(time.time() - start, 1),
                              "files": sorted(p.name for p in CHROMIUM.iterdir())}
    except Exception as error:  # noqa: BLE001
        report["chromium"] = {"ok": False, "error": f"{type(error).__name__}: {error}"[:600]}

    tectonic = ROOT / "probe" / "bin" / "tectonic"
    if tectonic.exists():
        tectonic.chmod(0o755)  # In case unzipping dropped the executable bit.
    report["tectonic in zip"] = {"present": tectonic.exists(),
                                 "executable": os.access(tectonic, os.X_OK) if tectonic.exists() else False,
                                 "version": run([str(tectonic), "--version"], 30).get("out", "").strip()[:200]}

    usage = shutil.disk_usage(ROOT)
    report["app folder free MB after"] = usage.free // 2**20
    report["chromium folder MB"] = round(sum(p.stat().st_size for p in CHROMIUM.rglob("*") if p.is_file()) / 2**20, 1) \
        if CHROMIUM.exists() else 0
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
