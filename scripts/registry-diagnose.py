"""Inspect runner-generated registry credential structure, never values."""
import json
import os
from pathlib import Path
import yaml


def paths(obj, prefix=""):
    result = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = prefix + str(key)
            result.append(path + (" (mapping)" if isinstance(value, dict) else " (list)" if isinstance(value, list) else " (value withheld)"))
            if isinstance(value, dict):
                result.extend(paths(value, path + "."))
    return result


for name in [Path.home() / ".config/unikraft/config.yaml", Path.home() / ".docker/config.json"]:
    if not name.exists():
        print("REGISTRY_PROFILE " + json.dumps({"file": str(name), "exists": False}))
        continue
    config = yaml.safe_load(name.read_text()) if name.suffix == ".yaml" else json.loads(name.read_text())
    print("REGISTRY_PROFILE " + json.dumps({"file": str(name), "exists": True, "key_paths": paths(config)}))


# Inspect/copy an immutable, user-owned image to a dedicated migration tag.
# CLI handles its own authenticated registry requests. No node is started here.
import subprocess
source = "qilonglin/unikraft@sha256:cac0ae8111d2b75fa83062dafd8b0e6729edb883a5cc547ef159d931ae84f170"
target = "qilonglin/unikraft:was-migration-20260930"
for label, command in [
    ("COPY_HELP", ["unikraft", "images", "copy", "--help"]),
    ("IMMUTABLE_IMAGE", ["unikraft", "images", "get", source, "-o", "json", "-f", "ref,digest,namespace"]),
    ("IMAGE_COPY", ["unikraft", "images", "copy", source, target]),
]:
    result = subprocess.run(command, capture_output=True, text=True, timeout=150)
    # Known configured token is redacted; CLI never dumps login material here.
    token = os.environ.get("UNIKRAFT_TOKEN", "")
    stdout = result.stdout.replace(token, "[REDACTED]") if token else result.stdout
    stderr = result.stderr.replace(token, "[REDACTED]") if token else result.stderr
    print("REGISTRY_COMMAND " + json.dumps({"label": label, "exit": result.returncode, "stdout": stdout[-13000:], "stderr": stderr[-5000:]}), flush=True)
