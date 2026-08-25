#!/usr/bin/env bash
set -euo pipefail

# After `unikraft login`, organization is stored in the active profile.
# Prefer JSON from `unikraft profile get`; fall back to config.yaml parsing.

ORG=""

if command -v unikraft >/dev/null 2>&1; then
  PROFILE_NAME="$(unikraft profile list -o json 2>/dev/null | jq -r '.[] | select(.active == true) | .name' | head -n1 || true)"
  if [[ -n "$PROFILE_NAME" && "$PROFILE_NAME" != "null" ]]; then
    ORG="$(unikraft profile get "$PROFILE_NAME" -o json 2>/dev/null | jq -r '.organization // empty' || true)"
  fi
  if [[ -z "$ORG" ]]; then
    ORG="$(unikraft profile get -o json 2>/dev/null | jq -r '.organization // empty' || true)"
  fi
fi

CONFIG="${UNIKRAFT_CONFIG:-$HOME/.config/unikraft/config.yaml}"
if [[ -z "$ORG" && -f "$CONFIG" ]]; then
  ORG="$(python3 - "$CONFIG" <<'PY'
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")

if yaml is not None:
    cfg = yaml.safe_load(text) or {}
    profile = cfg.get("profile", "default")
    profiles = cfg.get("profiles") or {}
    entry = profiles.get(profile) or {}
    print(entry.get("organization", "") or "")
else:
    active = "default"
    in_profiles = False
    current = None
    org = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("profile:"):
            active = stripped.split(":", 1)[1].strip()
            continue
        if stripped == "profiles:":
            in_profiles = True
            continue
        if in_profiles and not line.startswith(" ") and stripped.endswith(":"):
            break
        if in_profiles and stripped.endswith(":") and line.startswith("  ") and not line.startswith("    "):
            current = stripped[:-1]
            continue
        if current == active and stripped.startswith("organization:"):
            org = stripped.split(":", 1)[1].strip()
            break
    print(org)
PY
)"
fi

if [[ -z "$ORG" || "$ORG" == "null" ]]; then
  echo "Unable to determine Unikraft organization name after login." >&2
  echo "Ensure UNIKRAFT_TOKEN is valid and the account has an organization." >&2
  exit 1
fi

echo "$ORG"
