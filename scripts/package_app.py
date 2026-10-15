"""Build the class app store ZIP: uv run python scripts/package_app.py --output ../job-agent-app.zip

Only code and public data go in: no profile/, data/, .env or job sheets.
"""

import argparse
import re
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parent.parent


def personal_pattern() -> re.Pattern:
    """Your name, email and links from your local profile: none may appear in the ZIP."""
    import yaml

    path = ROOT / "profile" / "resume-profile.yaml"
    profile = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}
    contact = profile.get("contact") or {}
    words = [*(profile.get("name") or contact.get("name") or "").split(), contact.get("email", "")]
    words += [v for k, v in contact.items() if k != "phone" and isinstance(v, str) and "/" in v]
    words = [w.split("@")[0].rstrip("/").rsplit("/", 1)[-1] for w in words if len(w) > 3]
    return re.compile("|".join(re.escape(w) for w in words).encode() or rb"(?!)", re.I)


PERSONAL = personal_pattern()

parser = argparse.ArgumentParser()
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
if args.output.exists():
    parser.error(f"{args.output} exists; choose another name.")

files = {
    "workshop-app.json": ROOT / "appstore/workshop-app.json",
    "README.md": ROOT / "appstore/README.md",
    "pyproject.toml": ROOT / "appstore/pyproject.toml",  # Dependencies only, like the class examples.
    "uv.lock": ROOT / "appstore/uv.lock",
    "scripts/hosted_build.py": ROOT / "scripts/hosted_build.py",
    "bin/tectonic": ROOT / "appstore/bin/tectonic",
    "companies.csv": ROOT / "companies.csv",
    "company-rules.yaml": ROOT / "company-rules.yaml",
}
for pattern in ("job_agent/*.py", "job_agent/templates/*.tex"):
    for path in sorted(ROOT.glob(pattern)):
        if not path.name.startswith("demo_"):  # Fake job boards for the tests.
            files[str(path.relative_to(ROOT))] = path

with ZipFile(args.output, "w") as archive:
    for name, path in sorted(files.items()):
        data = path.read_bytes()
        if path.suffix.lower() in (".pem", ".key", ".p12", ".pfx", ".zip", ".log", ".pyc"):
            args.output.unlink()
            raise SystemExit(f"Refusing: the class platform rejects {name}.")
        if not name.startswith("bin/") and PERSONAL.search(data):
            args.output.unlink()
            raise SystemExit(f"Refusing: {name} contains personal data.")
        entry = ZipInfo(name, (1980, 1, 1, 0, 0, 0))
        entry.external_attr = (0o100755 if name == "bin/tectonic" else 0o100644) << 16
        entry.compress_type = ZIP_DEFLATED
        archive.writestr(entry, data)
if args.output.stat().st_size > 20 * 2**20:
    args.output.unlink()
    raise SystemExit("Refusing: over the platform's 20 MB ZIP limit.")
print(f"Wrote {args.output.resolve()} ({args.output.stat().st_size / 2**20:.1f} MB, {len(files)} files)")
