"""Publish only the locally built, user-owned WAS migration artifact."""
import json
import os
from pathlib import Path
import subprocess

REF = "qilonglin/unikraft:was-migration-20260930"
DEST = "unikraft.io/" + REF
paths = [Path(REF), Path(REF + ".tar"), Path(REF + ".oci")]
# Only report filenames/types, never application contents or credentials.
print("LOCAL_MIGRATION_ARTIFACTS " + json.dumps({str(p): {"exists": p.exists(), "is_dir": p.is_dir()} for p in paths}), flush=True)
print("BUILD_ARTIFACT_NAMES " + json.dumps([p.name for p in Path('.').iterdir()]), flush=True)
for path in paths:
    if path.exists():
        source = "./" + str(path)
        break
else:
    source = REF
r = subprocess.run(["unikraft", "images", "copy", source, DEST], capture_output=True, text=True, timeout=180)
token = os.environ.get("UNIKRAFT_TOKEN", "")
stdout = r.stdout.replace(token, "[REDACTED]") if token else r.stdout
stderr = r.stderr.replace(token, "[REDACTED]") if token else r.stderr
print("MIGRATION_IMAGE_PUBLISH " + json.dumps({"source": source, "dest": DEST, "exit": r.returncode, "stdout": stdout[-4000:], "stderr": stderr[-4000:]}), flush=True)
if r.returncode:
    raise SystemExit("Explicit image publication failed. Original FRA remains untouched.")
