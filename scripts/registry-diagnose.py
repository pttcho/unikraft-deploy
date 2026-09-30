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
