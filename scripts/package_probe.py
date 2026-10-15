"""Build the platform probe ZIP: uv run python scripts/package_probe.py --slug <slug> --output ../probe.zip"""

import argparse
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parent.parent
parser = argparse.ArgumentParser()
parser.add_argument("--slug", required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
manifest = json.loads((ROOT / "probe/workshop-app.json").read_text())
manifest["slug"] = args.slug
files = {"workshop-app.json": (json.dumps(manifest, indent=2) + "\n").encode(),
         "README.md": (ROOT / "probe/README.md").read_bytes(),
         "pyproject.toml": (ROOT / "probe/pyproject.toml").read_bytes(),
         "uv.lock": (ROOT / "probe/uv.lock").read_bytes(),
         "probe/app.py": (ROOT / "probe/app.py").read_bytes(),
         "probe/build.py": (ROOT / "probe/build.py").read_bytes(),
         "probe/bin/tectonic": (ROOT / "probe/bin/tectonic").read_bytes()}
if args.output.exists():
    parser.error(f"{args.output} exists; choose another name.")
with ZipFile(args.output, "w") as archive:
    for name, data in sorted(files.items()):
        entry = ZipInfo(name, (1980, 1, 1, 0, 0, 0))
        entry.external_attr = (0o100755 if name.endswith("bin/tectonic") else 0o100644) << 16
        entry.compress_type = ZIP_DEFLATED
        archive.writestr(entry, data)
print(f"Wrote {args.output.resolve()} for slug {args.slug!r}")
