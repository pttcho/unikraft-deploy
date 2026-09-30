#!/usr/bin/env python3
"""Read-only, scoped resource preflight. Never logs authentication material."""
import json
import os
import re
import sys
import urllib.error
import urllib.request

METRO = os.environ.get("METRO", "sin")
EXPECTED_FQDN = os.environ.get("EXPECTED_FQDN", "")
TARGET_CPU = int(os.environ.get("TARGET_VCPUS", "16"))
TARGET_MEMORY = int(os.environ.get("TARGET_MEMORY_MIB", "4096"))
if METRO != "sin" or EXPECTED_FQDN != "aged-mountain-xuanowfg.sin.unikraft.app":
    raise SystemExit("Scope guard: only the explicitly selected Singapore node is permitted.")
if TARGET_CPU != 16 or TARGET_MEMORY != 4096:
    raise SystemExit("Scope guard: this trial is limited to 16 vCPU / 4096 MiB.")
TOKEN = os.environ.get("UNIKRAFT_TOKEN", "").strip()
if not TOKEN:
    raise SystemExit("Missing configured authentication secret.")
BASE = "https://api.sin.unikraft.cloud/v1"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


opener = urllib.request.build_opener(NoRedirect())


def get(path):
    request = urllib.request.Request(
        BASE + path,
        headers={"Authorization": "Bearer " + TOKEN,
                 "Accept": "application/json",
                 "User-Agent": "Mozilla/5.0 (compatible; ProjectResourceInspector/1.0)"},
        method="GET",
    )
    try:
        with opener.open(request, timeout=25) as response:
            obj = json.load(response)
    except urllib.error.HTTPError as error:
        raise SystemExit("Read-only API request failed with HTTP " + str(error.code) + "; no instance was modified.") from None
    except (urllib.error.URLError, TimeoutError):
        raise SystemExit("Read-only API connection failed; no instance was modified.") from None
    if obj.get("status") not in ("success", "partial_success"):
        raise SystemExit("Platform rejected read-only request; no instance was modified.")
    return obj.get("data", {})


def quota_summary(value):
    # Preserve resource limits/units, but remove identities and any auth-like fields.
    forbidden = {"token", "access_token", "password", "secret", "uuid", "name", "username", "user_uuid",
                 "organization_uuid", "auth", "env", "args", "email", "fqdn", "private_ip", "id"}
    if isinstance(value, dict):
        return {str(k): quota_summary(v) for k, v in value.items()
                if str(k).lower() not in forbidden
                and not re.search(r"token|password|secret|credential|authorization", str(k), re.I)}
    if isinstance(value, list):
        return [quota_summary(v) for v in value]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_. /-]{1,64}", value):
        return value
    return "[non-resource text omitted]"


quotas = get("/users/quotas")
instances = get("/instances?details=true")
rows = instances.get("instances", []) if isinstance(instances, dict) else instances
matched = []
for instance in rows:
    service = instance.get("service_group") or {}
    domains = [d.get("fqdn", "") for d in service.get("domains", []) if isinstance(d, dict)]
    if EXPECTED_FQDN in domains:
        matched.append(instance)
if len(matched) != 1:
    raise SystemExit("Target guard: expected FQDN did not identify exactly one instance. No instance was modified.")
node = matched[0]
report = {
    "mode": "read-only",
    "metro": METRO,
    "target_matched": True,
    "current": {"state": node.get("state"), "vcpus": node.get("vcpus"), "memory_mib": node.get("memory_mb")},
    "requested": {"vcpus": TARGET_CPU, "memory_mib": TARGET_MEMORY},
    "quota_data": quota_summary(quotas),
    "instance_modified": False,
}
print("RESOURCE_PREFLIGHT_BEGIN")
print(json.dumps(report, ensure_ascii=False, indent=2))
print("RESOURCE_PREFLIGHT_END")
