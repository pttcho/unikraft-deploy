#!/usr/bin/env python3
"""Scheduled safe daily restart of the four proxy nodes.

Restart = user-initiated stop (drain) followed by start, which is what the
official `unikraft instances restart` command performs. Only the exact known
UUID / FQDN pairs from the 2026-09-30 roster are touched. Nothing is deleted,
no resources are changed, and the tunnel token / UUID never appear in logs.

The workflow runs one node per job (ACCOUNT + METRO env), four jobs in
parallel. Recovery is verified against the Argo tunnel entrance only.
"""
import datetime
import json
import os
import re
import time
import urllib.error
import urllib.request

# metro -> (instance UUID, direct FQDN, Argo domain)
EXPECTED = {
    "primary": {
        "sfo": ("a7c91ccc-ef4a-44de-941c-177bed5c81b8", "wispy-frost-5xvajfi9.sfo.unikraft.app", "godlike2.xxfxx.kdns.fr"),
        "sin": ("7c310f4f-5b0b-4cd0-9ba8-a72158974ddd", "aged-mountain-xuanowfg.sin.unikraft.app", "sugasuga.xxfxx.kdns.fr"),
    },
    "secondary": {
        "dal": ("8515c385-5c93-4281-946e-52e5d6ad2542", "morning-cloud-gfw0gjvx.dal.unikraft.app", "zheshi2.gkg.ccwu.cc"),
        "was": ("576733ad-701c-4b15-b8b7-0e213de656d4", "crimson-breeze-k85idd62.was.unikraft.app", "zheshi111.gkg.ccwu.cc"),
    },
}

ACCOUNT = os.environ.get("ACCOUNT", "").strip()
METRO = os.environ.get("METRO", "").strip()
TOKEN = os.environ.get("UNIKRAFT_TOKEN", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes")
METROS = ("sin", "sfo", "fra", "dal", "was")


class Failure(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect())


def emit(kind, value):
    print(kind + " " + json.dumps(value, ensure_ascii=False), flush=True)


def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def redact(text):
    value = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
    if TOKEN:
        value = value.replace(TOKEN, "[REDACTED]")
    value = re.sub(r"[A-Za-z0-9_+/=-]{100,}", "[LONG VALUE REDACTED]", value)
    return value


def api(metro, method, path, body=None):
    if metro not in METROS or not TOKEN:
        raise Failure("API scope/authentication guard failed")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request("https://api." + metro + ".unikraft.cloud/v1" + path, data=data,
        headers={"Authorization": "Bearer " + TOKEN, "Accept": "application/json", "Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0 (compatible; ProxyDailyRestart/1.0)"}, method=method)
    attempts = 3 if method == "GET" else 1
    for attempt in range(attempts):
        try:
            with OPENER.open(req, timeout=35) as response:
                obj = json.load(response)
        except urllib.error.HTTPError as error:
            detail = redact(error.read(1600).decode("utf-8", "replace"))
            if method == "GET" and error.code in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                time.sleep(2)
                continue
            raise Failure(method + " " + metro + path + " HTTP " + str(error.code) + ": " + detail) from None
        except (urllib.error.URLError, TimeoutError):
            if method == "GET" and attempt + 1 < attempts:
                time.sleep(2)
                continue
            raise Failure(method + " " + metro + path + " network outcome unknown; mutation not retried") from None
        if obj.get("status") != "success" or obj.get("errors"):
            raise Failure("API non-success: " + redact(obj)[:1800])
        result = obj.get("data", {})
        for rows in result.values():
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict) and (row.get("status", "success") != "success" or row.get("error", 0)):
                        raise Failure("API item failure: " + redact(row)[:1000])
        return result
    raise Failure("Read retry budget exhausted")


def domain_list(n):
    return [d["fqdn"].rstrip(".") for d in (n.get("service_group") or {}).get("domains", []) if d.get("fqdn")]


def node(metro, uid):
    rows = api(metro, "GET", "/instances/" + uid + "?details=true").get("instances", [])
    if len(rows) != 1 or rows[0].get("uuid") != uid:
        raise Failure("Exact instance UUID guard failed")
    return rows[0]


def safety(n):
    if "delete-on-stop" in (str(n.get("features", "")) + str(n.get("flags", ""))):
        raise Failure("Delete-on-stop instance must not be restarted")
    kill = n.get("autokill") or {}
    if any(kill.get(k, 0) for k in ("time_ms", "num_requests", "time")):
        raise Failure("Automatic deletion policy needs review")


def wait_state(metro, uid, desired, timeout=120, started_after=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        n = node(metro, uid)
        if n.get("state") == desired:
            return n
        if desired == "running" and started_after is not None and n.get("state") == "stopped" and n.get("start_count", 0) > started_after:
            raise Failure("Boot failed before running: stop_reason=" + str(n.get("stop_reason")) + " stop_code=" + str(n.get("stop_code")))
        time.sleep(2)
    raise Failure("Instance did not reach " + desired)


def stop(metro, n):
    uid = n["uuid"]
    if n["state"] == "stopped":
        return n
    if n["state"] in ("draining", "stopping"):
        return wait_state(metro, uid, "stopped")
    if n["state"] not in ("running", "starting"):
        raise Failure("Refusing stop from unexpected state " + n["state"])
    api(metro, "PUT", "/instances/" + uid + "/stop", {"force": False, "drain_timeout_ms": 5000, "quick": False, "ifstate": n["state"]})
    return wait_state(metro, uid, "stopped")


def start(metro, uid):
    n = node(metro, uid)
    if n["state"] == "running":
        return n
    if n["state"] in ("stopping", "draining"):
        n = wait_state(metro, uid, "stopped")
    if n["state"] != "stopped":
        return wait_state(metro, uid, "running")
    previous_count = n.get("start_count", 0)
    api(metro, "PUT", "/instances/" + uid + "/start", {"timeout_s": 0})
    return wait_state(metro, uid, "running", started_after=previous_count)


def healthy(n, argo, timeout=90):
    # Recovery is validated through the Argo tunnel entrance only.
    deadline = time.monotonic() + timeout
    last = 0
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request("https://" + argo + "/", headers={"User-Agent": "Mozilla/5.0 (compatible; ProxyDailyRestart/1.0)"})
            with OPENER.open(req, timeout=7) as r:
                last = r.status
                r.read(128)
        except (urllib.error.URLError, TimeoutError):
            last = 0
        if last == 200:
            return {"argo": argo, "status": last}
        time.sleep(3)
    raise Failure("Argo root health did not recover for " + argo + ": HTTP " + str(last))


def public(n, metro):
    return {"metro": metro, "uuid": n["uuid"], "name": n["name"], "state": n["state"],
            "memory_mb": n["memory_mb"], "vcpus": n["vcpus"], "domains": domain_list(n), "image": n.get("image")}


def restart_one(metro, uid, fqdn, argo):
    before = node(metro, uid)
    if fqdn not in domain_list(before):
        raise Failure("Expected FQDN guard failed for " + metro)
    safety(before)
    if DRY_RUN:
        emit("RESTART_DRY_RUN", {"node": public(before, metro), "argo": argo})
        return
    if before["state"] != "running":
        # A node that is already down is only started; there is nothing to restart.
        emit("RESTART_PRE_STATE", {"node": public(before, metro), "note": "not running; starting only"})
        n = start(metro, uid)
        health = healthy(n, argo)
        emit("RESTART_VERIFIED", {"node": public(n, metro), "health": health, "was_running": False})
        return
    start_count = before.get("start_count", 0)
    emit("RESTART_BEGIN", {"node": public(before, metro), "argo": argo, "utc": utc()})
    stop(metro, before)
    n = start(metro, uid)
    if n.get("start_count", 0) <= start_count:
        raise Failure("Restart did not increment start_count for " + metro)
    if domain_list(n) != domain_list(before):
        raise Failure("Direct domain changed across restart for " + metro)
    health = healthy(n, argo)
    emit("RESTART_VERIFIED", {"node": public(n, metro), "health": health, "start_count": n.get("start_count")})


def main():
    if ACCOUNT not in EXPECTED or not TOKEN:
        raise Failure("Missing account authentication")
    if not DRY_RUN and os.environ.get("CONFIRM") != "DAILY-RESTART":
        raise Failure("Daily restart confirmation missing")
    available = EXPECTED[ACCOUNT]
    if METRO:
        if METRO not in available:
            raise Failure("Requested metro " + METRO + " is not part of account " + ACCOUNT)
        targets = {METRO: available[METRO]}
    else:
        targets = available
    results = []
    for metro, (uid, fqdn, argo) in targets.items():
        try:
            restart_one(metro, uid, fqdn, argo)
            results.append({"metro": metro, "ok": True})
        except Exception as error:  # keep going so one bad node cannot block the others
            detail = redact(str(error))
            emit("RESTART_FAILURE", {"metro": metro, "detail": detail})
            results.append({"metro": metro, "ok": False, "detail": detail})
    failed = [r for r in results if not r["ok"]]
    emit("DAILY_RESTART_SUMMARY", {"account": ACCOUNT, "utc": utc(), "dry_run": DRY_RUN, "results": results})
    if failed:
        raise Failure(str(len(failed)) + " node(s) failed to restart and recover")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        emit("DAILY_RESTART_ABORTED", {"detail": redact(str(error)) if TOKEN else str(error)})
        raise SystemExit(1)
