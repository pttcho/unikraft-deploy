#!/usr/bin/env python3
"""Scoped preflight for FRA->WAS migration and four 4096 MiB nodes. GET only."""
import json
import os
import urllib.error
import urllib.request

EXPECTED = {
    "primary": {"sin": "aged-mountain-xuanowfg.sin.unikraft.app", "sfo": "wispy-frost-5xvajfi9.sfo.unikraft.app"},
    "secondary": {"dal": "morning-cloud-gfw0gjvx.dal.unikraft.app", "was": "crimson-breeze-k85idd62.was.unikraft.app"},
}
ACCOUNT = os.environ["ACCOUNT"]
TOKEN = os.environ["UNIKRAFT_TOKEN"].strip()
if ACCOUNT not in EXPECTED or not TOKEN:
    raise SystemExit("Missing or invalid scoped account.")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect())


def get(metro, path):
    req = urllib.request.Request("https://api." + metro + ".unikraft.cloud/v1" + path,
        headers={"Authorization": "Bearer " + TOKEN, "Accept": "application/json",
                 "User-Agent": "Mozilla/5.0 (compatible; ProxyMaintenance/1.0)"}, method="GET")
    try:
        with OPENER.open(req, timeout=30) as r:
            result = json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit("Read-only preflight HTTP " + str(e.code)) from None
    except (urllib.error.URLError, TimeoutError):
        raise SystemExit("Read-only preflight connection failure") from None
    if result.get("status") != "success":
        raise SystemExit("Preflight API returned non-success status.")
    return result.get("data", {})


def domains(instance):
    return [d["fqdn"].rstrip(".") for d in (instance.get("service_group") or {}).get("domains", []) if d.get("fqdn")]


report = []
for metro, expected in EXPECTED[ACCOUNT].items():
    quotas = get(metro, "/users/quotas").get("quotas", [])
    if len(quotas) != 1:
        raise SystemExit("Expected exactly one quota record.")
    q = quotas[0]
    rows = get(metro, "/instances?details=true").get("instances", [])
    if expected:
        matched = [n for n in rows if expected in domains(n)]
        if len(matched) != 1:
            raise SystemExit("Expected FQDN did not identify exactly one " + metro + " node.")
        n = matched[0]
        services = get(metro, "/services/" + n["service_group"]["uuid"] + "?details=true").get("service_groups", [])
        if len(services) != 1:
            raise SystemExit("Expected exactly one service group.")
        sg = services[0]
        memory_after_resize = q["used"]["live_memory_mb"] - (n["memory_mb"] if n["state"] == "running" else 0) + 4096
        eligible = q["limits"]["max_memory_mb"] >= 4096 and q["hard"]["live_memory_mb"] >= memory_after_resize
        item = {"metro": metro, "eligible_4096": eligible, "quota": {"used": q["used"], "hard": q["hard"], "limits": q["limits"]},
            "node": {"uuid": n["uuid"], "name": n["name"], "state": n["state"], "memory_mb": n["memory_mb"], "vcpus": n["vcpus"],
                     "fqdn": expected, "image": n.get("image"), "features": n.get("features"), "flags": n.get("flags"),
                     "volumes_count": len(n.get("volumes", [])), "roms_count": len(n.get("roms", [])),
                     "args_present": bool(n.get("args")), "env_present": bool(n.get("env")), "restart_policy": n.get("restart_policy"),
                     "scale_to_zero": n.get("scale_to_zero"), "autokill": n.get("autokill")},
            "service": {"uuid": sg["uuid"], "persistent": sg.get("persistent"), "services": sg.get("services"), "domains": domains(n)}}
    else:
        eligible = q["limits"]["max_memory_mb"] >= 4096 and q["hard"]["live_memory_mb"] - q["used"]["live_memory_mb"] >= 4096
        item = {"metro": metro, "eligible_4096": eligible, "quota": {"used": q["used"], "hard": q["hard"], "limits": q["limits"]},
                "existing_instances": [{"uuid": n["uuid"], "name": n["name"], "state": n["state"], "memory_mb": n["memory_mb"]} for n in rows]}
    if metro == "was":
        diagnostics = []
        for existing in rows:
            if existing.get("name") != "unikraft-proxy-was-20260930":
                continue
            detailed = get(metro, "/instances/" + existing["uuid"] + "?details=true").get("instances", [existing])[0]
            keys = ["uuid", "name", "state", "memory_mb", "vcpus", "image", "created_at", "started_at", "stopped_at", "start_count", "restart_count", "stop_reason", "stop_code", "exit_code", "boot_time_us", "net_time_us", "vmm_start_time_us", "vmm_load_time_us", "vmm_ready_time_us"]
            diagnostic = {k: detailed.get(k) for k in keys}
            try:
                logs = get(metro, "/instances/" + existing["uuid"] + "/logs")
                raw = json.dumps(logs, ensure_ascii=False).replace(TOKEN, "[REDACTED]")
                import re
                raw = re.sub(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", "[UUID]", raw)
                raw = re.sub(r"[A-Za-z0-9_+/=-]{100,}", "[LONG VALUE REDACTED]", raw)
                diagnostic["boot_log_excerpt"] = raw[-14000:]
            except SystemExit:
                diagnostic["boot_log_excerpt"] = "Logs unavailable"
            diagnostics.append(diagnostic)
        item["was_diagnostics"] = diagnostics
    report.append(item)
print("MAINTENANCE_PREFLIGHT " + json.dumps({"account": ACCOUNT, "mode": "read-only", "nodes_modified": False, "regions": report}), flush=True)
