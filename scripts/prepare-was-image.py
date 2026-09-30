"""Bind stopped WAS candidate to the freshly built, privately recovered image."""
import importlib.util
import json
import os
import re
import subprocess

REF = "qilonglin/unikraft:was-migration-20260930"
result = subprocess.run(["unikraft", "images", "get", "unikraft.io/" + REF, "-o", "json"], capture_output=True, text=True, timeout=45)
if result.returncode:
    token = os.environ.get("UNIKRAFT_TOKEN", "")
    detail = result.stderr.replace(token, "[REDACTED]") if token else result.stderr
    print("NEW_IMAGE_LOOKUP_ERROR " + json.dumps({"exit": result.returncode, "detail": detail[-1800:]}), flush=True)
    raise SystemExit("New migration image could not be read from the authenticated registry. FRA is unchanged.")
obj = json.loads(result.stdout)

def digests(value):
    found = []
    if isinstance(value, dict):
        for key, v in value.items():
            if key == "digest" and isinstance(v, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", v):
                found.append(v)
            else:
                found.extend(digests(v))
    elif isinstance(value, list):
        for v in value:
            found.extend(digests(v))
    return found

values = list(set(digests(obj)))
if len(values) != 1:
    raise SystemExit("Exactly one authenticated image digest is required.")
digest = values[0]
spec = importlib.util.spec_from_file_location("maintenance", "scripts/maintenance-apply.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
source = m.original("fra")
if source["state"] != "running":
    raise SystemExit("FRA must remain running during image preparation")
matched = [n for n in m.nodes("was") if n.get("name") == m.MIGRATION_NAME]
if len(matched) != 1 or matched[0]["state"] != "stopped":
    raise SystemExit("Exactly one stopped migration candidate is required")
target = matched[0]
m.api("was", "PATCH", "/instances/" + target["uuid"], {"prop": "image", "op": "set", "value": {"url": REF, "pull_policy": "always"}})
with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as env:
    env.write("MIGRATION_REBUILT_DIGEST=" + digest + "\n")
print("REBUILT_IMAGE_READY " + json.dumps({"ref": REF, "digest": digest, "target_uuid": target["uuid"], "fra_running": True, "credentials_logged": False}), flush=True)
