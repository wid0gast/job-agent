"""Build the workshop upload ZIP for the hosted demo.

    uv run python scripts/package_hosted.py --slug <address from My app> --output ../job-agent.zip

Standard library only. The ZIP has workshop-app.json at its root.
"""

import argparse
import hashlib
import io
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parent.parent
# Published path in the ZIP -> source file in this project.
FILES = {
    "workshop-app.json": "hosted/workshop-app.json",
    "pyproject.toml": "hosted/pyproject.toml",
    "uv.lock": "hosted/uv.lock",
    "README.md": "hosted/README.md",
    "hosted/app.py": "hosted/app.py",
    "hosted/index.html": "hosted/index.html",
    "hosted/app.js": "hosted/app.js",
    "job_agent/harness.py": "job_agent/harness.py",
    "job_agent/tracker.py": "job_agent/tracker.py",
    "job_agent/documents.py": "job_agent/documents.py",
    "job_agent/demo_site.py": "job_agent/demo_site.py",
    "profile.example/resume.md": "profile.example/resume.md",
}

parser = argparse.ArgumentParser()
parser.add_argument("--slug", required=True, help="Exact app address claimed in My app.")
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()

manifest_path = ROOT / FILES["workshop-app.json"]
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
if manifest["slug"] != args.slug:
    manifest["slug"] = args.slug
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f'Updated hosted/workshop-app.json slug to "{args.slug}".')

for source in manifest["runtime"]["build"][3:]:
    if source not in FILES:
        parser.error(f"Build file {source} is not packaged.")

data = {name: (ROOT / source).read_bytes() for name, source in FILES.items()}
buffer = io.BytesIO()
with ZipFile(buffer, "w") as archive:
    for name, contents in sorted(data.items()):
        entry = ZipInfo(name, (1980, 1, 1, 0, 0, 0))
        entry.create_system = 3
        entry.external_attr = 0o100644 << 16
        entry.compress_type = ZIP_DEFLATED
        archive.writestr(entry, contents)
payload = buffer.getvalue()

output = args.output.expanduser().absolute()
if output.suffix != ".zip":
    parser.error("--output must name a .zip file.")
output.parent.mkdir(parents=True, exist_ok=True)
output.write_bytes(payload)
with ZipFile(output) as archive:
    assert set(archive.namelist()) == set(FILES)
print(json.dumps({"zip": str(output), "slug": args.slug, "files": len(FILES),
                  "sha256": hashlib.sha256(payload).hexdigest()}, indent=1))
