#!/usr/bin/env python3
"""Explicitly authorized rolling 4096 MiB resize / FRA-to-WAS migration.
Mutating requests are never automatically retried. Existing UUID/FQDN guards,
quota checks, state reconciliation and best-effort rollback protect live nodes.
No tokens, application args or environment values are logged.
"""
import datetime
import json
import os
import time
import urllib.error
import urllib.request

EXPECTED = {
    "sin": ("7c310f4f-5b0b-4cd0-9ba8-a72158974ddd", "aged-mountain-xuanowfg.sin.unikraft.app", "sugasuga.xxfxx.kdns.fr"),
    "sfo": ("a7c91ccc-ef4a-44de-941c-177bed5c81b8", "wispy-frost-5xvajfi9.sfo.unikraft.app", "godlike2.xxfxx.kdns.fr"),
    "fra": ("f36e5308-ac40-4c16-8f7e-13416059df21", "little-snowflake-y84r85js.fra.unikraft.app", "zheshi111.mkvskg.dpdns.org"),
    "dal": ("8515c385-5c93-4281-946e-52e5d6ad2542", "morning-cloud-gfw0gjvx.dal.unikraft.app", "zheshi222.mkvskg.dpdns.org"),
}
MIGRATION_NAME = "unikraft-proxy-was-20260930"
TARGET_MEMORY = 4096
TOKEN = os.environ.get("UNIKRAFT_TOKEN", "").strip()
ACTION = os.environ.get("ACTION", "verify")
ACCOUNT = os.environ.get("ACCOUNT", "")
CONFIRM = os.environ.get("CONFIRM", "")


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


def api(metro, method, path, body=None):
    if metro not in ("sin", "sfo", "fra", "dal", "was") or not TOKEN:
        raise Failure("API scope/authentication guard failed")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request("https://api." + metro + ".unikraft.cloud/v1" + path, data=data,
        headers={"Authorization": "Bearer " + TOKEN, "Accept": "application/json", "Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0 (compatible; ProxyMaintenance/1.0)"}, method=method)
    attempts = 3 if method == "GET" else 1
    for attempt in range(attempts):
        try:
            with OPENER.open(req, timeout=35) as response:
                obj = json.load(response)
        except urllib.error.HTTPError as error:
            detail = error.read(1600).decode("utf-8", "replace").replace(TOKEN, "[REDACTED]")
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
            raise Failure("API non-success: " + json.dumps(obj, ensure_ascii=False).replace(TOKEN, "[REDACTED]")[:1800])
        result = obj.get("data", {})
        for rows in result.values():
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict) and (row.get("status", "success") != "success" or row.get("error", 0)):
                        raise Failure("API item failure: " + json.dumps(row).replace(TOKEN, "[REDACTED]")[:1000])
        return result
    raise Failure("Read retry budget exhausted")


def domain_list(n):
    return [d["fqdn"].rstrip(".") for d in (n.get("service_group") or {}).get("domains", []) if d.get("fqdn")]


def nodes(metro):
    return api(metro, "GET", "/instances?details=true").get("instances", [])


def node(metro, uid):
    rows = api(metro, "GET", "/instances/" + uid + "?details=true").get("instances", [])
    if len(rows) != 1 or rows[0].get("uuid") != uid:
        raise Failure("Exact instance UUID guard failed")
    return rows[0]


def original(metro):
    uid, fqdn, _ = EXPECTED[metro]
    n = node(metro, uid)
    if fqdn not in domain_list(n):
        raise Failure("Expected existing FQDN guard failed")
    return n


def safety(n):
    features = str(n.get("features", "")) + str(n.get("flags", ""))
    if "delete-on-stop" in features:
        raise Failure("Delete-on-stop instances cannot be safely maintained")
    kill = n.get("autokill") or {}
    if any(kill.get(k, 0) for k in ("time_ms", "num_requests", "time")):
        raise Failure("Automatic deletion policy needs review")


def check_quota(metro, n=None, new=False):
    rows = api(metro, "GET", "/users/quotas").get("quotas", [])
    if len(rows) != 1:
        raise Failure("Exactly one user quota is required")
    q = rows[0]
    old_live = n["memory_mb"] if n and n.get("state") == "running" else 0
    desired_total = q["used"]["live_memory_mb"] - old_live + TARGET_MEMORY
    if q["limits"]["max_memory_mb"] < TARGET_MEMORY or desired_total > q["hard"]["live_memory_mb"]:
        raise Failure("4096 MiB exceeds actual quota; no paid tier change is attempted")
    cpu = n["vcpus"] if n else 1
    if cpu > q["limits"]["max_vcpus"]:
        raise Failure("Current CPU is outside the target region's per-instance quota")
    if new and (q["used"]["instances"] >= q["hard"]["instances"] or q["used"]["live_vcpus"] + cpu > q["hard"]["live_vcpus"]):
        raise Failure("Target region has insufficient instance/CPU headroom")


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
    safety(n)
    uid = n["uuid"]
    if n["state"] == "stopped":
        return n
    if n["state"] in ("draining", "stopping"):
        return wait_state(metro, uid, "stopped")
    if n["state"] not in ("running", "starting"):
        raise Failure("Refusing stop from unexpected state " + n["state"])
    api(metro, "PUT", "/instances/" + uid + "/stop", {"force": False, "drain_timeout_ms": 3000, "quick": False, "ifstate": n["state"]})
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


def patch_memory(metro, uid, value):
    n = node(metro, uid)
    if n["memory_mb"] == value:
        return n
    if n["state"] != "stopped":
        raise Failure("RAM writes require an explicitly stopped node")
    api(metro, "PATCH", "/instances/" + uid, {"prop": "memory_mb", "op": "set", "value": value})
    n = node(metro, uid)
    if n["memory_mb"] != value:
        raise Failure("Memory update readback did not match")
    return n


def healthy(n, argo, timeout=90):
    names = domain_list(n)
    if len(names) != 1:
        raise Failure("Exactly one direct domain is required")
    expected_hosts = [names[0], argo]
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        for host in expected_hosts:
            try:
                req = urllib.request.Request("https://" + host + "/", headers={"User-Agent": "Mozilla/5.0 (compatible; ProxyHealthCheck/1.0)"})
                with OPENER.open(req, timeout=7) as r:
                    last[host] = r.status
                    r.read(128)
            except (urllib.error.URLError, TimeoutError):
                last[host] = 0
        if all(last.get(h) == 200 for h in expected_hosts):
            return last
        time.sleep(3)
    raise Failure("Direct/Argo root health did not recover: " + json.dumps(last))


def public(n, metro):
    return {"metro": metro, "uuid": n["uuid"], "name": n["name"], "state": n["state"], "memory_mb": n["memory_mb"], "vcpus": n["vcpus"], "domains": domain_list(n), "image": n.get("image")}


def resize(metro):
    old = original(metro)
    if old["state"] != "running":
        raise Failure("Expected original node to be running")
    safety(old)
    check_quota(metro, old)
    emit("ROLLBACK_CHECKPOINT", public(old, metro))
    if old["memory_mb"] == TARGET_MEMORY:
        health = healthy(old, EXPECTED[metro][2])
        emit("RESIZE_VERIFIED", {"node": public(old, metro), "health": health, "changed": False})
        return
    try:
        emit("RESIZE_STOP", {"metro": metro, "utc": utc()})
        stop(metro, old)
        patch_memory(metro, old["uuid"], TARGET_MEMORY)
        n = start(metro, old["uuid"])
        if n["vcpus"] != old["vcpus"] or n.get("image") != old.get("image") or domain_list(n) != domain_list(old):
            raise Failure("Unexpected CPU/image/domain change")
        health = healthy(n, EXPECTED[metro][2])
        emit("RESIZE_VERIFIED", {"node": public(n, metro), "health": health, "changed": True})
    except Exception as error:
        emit("RESIZE_FAILURE", {"metro": metro, "detail": str(error), "rollback_requested": True})
        try:
            current = node(metro, old["uuid"])
            if current["memory_mb"] != old["memory_mb"]:
                stop(metro, current)
                patch_memory(metro, old["uuid"], old["memory_mb"])
            n = start(metro, old["uuid"])
            emit("ROLLBACK_VERIFIED", {"node": public(n, metro), "health": healthy(n, EXPECTED[metro][2])})
        except Exception as rollback_error:
            emit("ROLLBACK_NEEDS_ATTENTION", {"metro": metro, "detail": str(rollback_error)})
        raise


def migration_image_matches(target, source):
    expected = os.environ.get("MIGRATION_REBUILT_DIGEST", "")
    if expected:
        import re
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected):
            raise Failure("Rebuilt digest format guard failed")
        image = target.get("image", "")
        return image.split("@")[-1] == expected or image == "oci://unikraft.io/qilonglin/unikraft:was-migration-20260930"
    return target.get("image", "").split("@")[-1] == source.get("image", "").split("@")[-1]


def migrate():
    src = original("fra")
    safety(src)
    if src.get("volumes") or src.get("roms") or src.get("plugins") or src.get("dependencies"):
        raise Failure("Attached state/dependencies require a separate migration plan")
    existing = [n for n in nodes("was") if n.get("name") == MIGRATION_NAME]
    if len(existing) > 1:
        raise Failure("Migration name is not unique")
    if existing:
        target = existing[0]
        if "fra-to-was-20260930" not in (target.get("tags") or []) or not migration_image_matches(target, src):
            raise Failure("Existing WAS candidate is not this exact migration")
        if target["state"] == "running" and src["state"] == "stopped" and target["memory_mb"] == TARGET_MEMORY:
            emit("MIGRATION_VERIFIED", {"node": public(target, "was"), "old_node": public(src, "fra"), "health": healthy(target, EXPECTED["fra"][2]), "resumed": True})
            return
    else:
        if src["state"] != "running":
            raise Failure("Source FRA must be running before provisioning")
        check_quota("was", new=True)
        groups = api("fra", "GET", "/services/" + src["service_group"]["uuid"] + "?details=true").get("service_groups", [])
        if len(groups) != 1:
            raise Failure("Source service group is not unique")
        ports = [{k: p[k] for k in ("port", "destination_port", "protocol", "handlers") if k in p} for p in groups[0]["services"]]
        if sorted(p["port"] for p in ports) != [443, 8880]:
            raise Failure("Unexpected source public port mapping")
        image = src["image"].removeprefix("oci://unikraft.io/")
        body = {"name": MIGRATION_NAME, "image": image, "memory_mb": TARGET_MEMORY, "vcpus": src["vcpus"],
                "autostart": False, "restart_policy": src.get("restart_policy", "never"),
                "service_group": {"services": ports}, "tags": ["unikraft-proxy", "fra-to-was-20260930"]}
        if src.get("args"):
            body["args"] = src["args"]
        if src.get("env"):
            body["env"] = src["env"]
        emit("MIGRATION_PROVISION", {"source": public(src, "fra"), "target_name": MIGRATION_NAME, "autostart": False, "opaque_image_reused": True})
        try:
            api("was", "POST", "/instances", body)
        except Exception:
            # Reconcile an uncertain create response by unique name; never repeat POST.
            created = [n for n in nodes("was") if n.get("name") == MIGRATION_NAME]
            if len(created) != 1:
                raise
        created = [n for n in nodes("was") if n.get("name") == MIGRATION_NAME]
        if len(created) != 1:
            raise Failure("Created target could not be uniquely read back")
        target = created[0]
    if target["state"] != "stopped" or target["memory_mb"] != TARGET_MEMORY or target["vcpus"] != src["vcpus"]:
        if target["state"] == "running":
            stop("was", target)
        raise Failure("Stopped target resource verification failed")
    if not migration_image_matches(target, src):
        raise Failure("Opaque image digest mismatch; original FRA remains untouched")
    check_quota("was", target)
    emit("ROLLBACK_CHECKPOINT", {"source": public(src, "fra"), "stopped_target": public(target, "was")})
    try:
        emit("MIGRATION_CUTOVER", {"utc": utc(), "existing_argo_domain_retained": EXPECTED["fra"][2]})
        stop("fra", src)
        n = start("was", target["uuid"])
        health = healthy(n, EXPECTED["fra"][2])
        if n["memory_mb"] != TARGET_MEMORY or n["vcpus"] != src["vcpus"] or node("fra", src["uuid"])["state"] != "stopped":
            raise Failure("Cutover state verification failed")
        emit("MIGRATION_VERIFIED", {"node": public(n, "was"), "old_node": public(node("fra", src["uuid"]), "fra"), "health": health, "old_node_retained_stopped": True})
    except Exception as error:
        emit("MIGRATION_FAILURE", {"detail": str(error), "rollback_requested": True})
        try:
            stop("was", node("was", target["uuid"]))
            n = start("fra", src["uuid"])
            emit("ROLLBACK_VERIFIED", {"node": public(n, "fra"), "health": healthy(n, EXPECTED["fra"][2])})
        except Exception as rollback_error:
            emit("ROLLBACK_NEEDS_ATTENTION", {"detail": str(rollback_error)})
        raise


def verify():
    targets = ["sin", "sfo"] if ACCOUNT == "primary" else ["dal", "was"]
    records = []
    for metro in targets:
        if metro == "was":
            matched = [n for n in nodes("was") if n.get("name") == MIGRATION_NAME]
            if len(matched) != 1:
                raise Failure("Exactly one migrated WAS instance required")
            n = matched[0]
            argo = EXPECTED["fra"][2]
        else:
            n = original(metro)
            argo = EXPECTED[metro][2]
        if n["state"] != "running" or n["memory_mb"] != TARGET_MEMORY or n["vcpus"] != 1:
            raise Failure("Final running resource readback mismatch for " + metro)
        records.append({"node": public(n, metro), "health": healthy(n, argo)})
    if ACCOUNT == "secondary":
        old_fra = [n for n in nodes("fra") if n.get("uuid") == EXPECTED["fra"][0]]
        if old_fra and old_fra[0]["state"] != "stopped":
            raise Failure("Old FRA is still active")
    emit("FINAL_NODE_VERIFICATION", {"account": ACCOUNT, "utc": utc(), "nodes": records})


def main():
    if ACCOUNT not in ("primary", "secondary") or not TOKEN:
        raise Failure("Missing account authentication")
    if ACTION != "verify" and CONFIRM != "FRA-TO-WAS-ALL-4096":
        raise Failure("Explicit apply confirmation missing")
    if ACTION == "resize-primary" and ACCOUNT == "primary":
        # Check both nodes before touching the first one.
        for metro in ("sfo", "sin"):
            n = original(metro)
            safety(n)
            check_quota(metro, n)
        for metro in ("sfo", "sin"):
            resize(metro)
    elif ACTION == "migrate-secondary" and ACCOUNT == "secondary":
        migrate()
    elif ACTION == "resize-secondary" and ACCOUNT == "secondary":
        resize("dal")
    elif ACTION == "verify":
        verify()
    else:
        raise Failure("Action/account scope guard failed")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        emit("MAINTENANCE_ABORTED", {"action": ACTION, "account": ACCOUNT, "detail": str(error).replace(TOKEN, "[REDACTED]") if TOKEN else str(error)})
        raise SystemExit(1)
