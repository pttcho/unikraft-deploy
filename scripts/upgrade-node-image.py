#!/usr/bin/env python3
"""Swap one node's image in place, verify it, and roll back on failure.

Only the exact known UUID/FQDN pair is touched. The image is replaced on a
stopped instance (the same in-place path used for the WAS migration), so no new
instance is created and no quota is consumed. The previous image reference is
captured first and restored automatically if the node does not come back up.
"""
import datetime
import json
import os
import time
import urllib.error
import urllib.request

EXPECTED = {
    "primary": {
        "sfo": ("a7c91ccc-ef4a-44de-941c-177bed5c81b8", "wispy-frost-5xvajfi9.sfo.unikraft.app", "godlike2.xxfxx.kdns.fr"),
        "sin": ("7c310f4f-5b0b-4cd0-9ba8-a72158974ddd", "aged-mountain-xuanowfg.sin.unikraft.app", "sugasuga.xxfxx.kdns.fr"),
    },
    "secondary": {
        "dal": ("8515c385-5c93-4281-946e-52e5d6ad2542", "morning-cloud-gfw0gjvx.dal.unikraft.app", "zheshi222.mkvskg.dpdns.org"),
        "was": ("576733ad-701c-4b15-b8b7-0e213de656d4", "crimson-breeze-k85idd62.was.unikraft.app", "zheshi111.mkvskg.dpdns.org"),
    },
}
METROS = ("sin", "sfo", "fra", "dal", "was")
ACCOUNT = os.environ.get("ACCOUNT", "").strip()
METRO = os.environ.get("METRO", "").strip()
TOKEN = os.environ.get("UNIKRAFT_TOKEN", "").strip()
NEW_IMAGE = os.environ.get("NEW_IMAGE", "").strip()
CONFIRM = os.environ.get("CONFIRM", "").strip()


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
    if metro not in METROS or not TOKEN:
        raise Failure("API scope/authentication guard failed")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request("https://api." + metro + ".unikraft.cloud/v1" + path, data=data,
        headers={"Authorization": "Bearer " + TOKEN, "Accept": "application/json",
                 "Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0 (compatible; NodeImageUpgrade/1.0)"}, method=method)
    attempts = 3 if method == "GET" else 1
    for attempt in range(attempts):
        try:
            with OPENER.open(req, timeout=35) as response:
                obj = json.load(response)
        except urllib.error.HTTPError as error:
            detail = error.read(1200).decode("utf-8", "replace").replace(TOKEN, "[REDACTED]")
            if method == "GET" and error.code in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                time.sleep(2)
                continue
            raise Failure(method + " " + metro + path + " HTTP " + str(error.code) + ": " + detail) from None
        except (urllib.error.URLError, TimeoutError):
            if method == "GET" and attempt + 1 < attempts:
                time.sleep(2)
                continue
            raise Failure(method + " " + metro + path + " outcome unknown; mutation not retried") from None
        if obj.get("status") != "success" or obj.get("errors"):
            raise Failure("API non-success: " + json.dumps(obj, ensure_ascii=False).replace(TOKEN, "[REDACTED]")[:1200])
        return obj.get("data", {})
    raise Failure("Read retry budget exhausted")


def domains(n):
    return [d["fqdn"].rstrip(".") for d in (n.get("service_group") or {}).get("domains", []) if d.get("fqdn")]


def node(uid):
    rows = api(METRO, "GET", "/instances/" + uid + "?details=true").get("instances", [])
    if len(rows) != 1 or rows[0].get("uuid") != uid:
        raise Failure("Exact instance UUID guard failed")
    return rows[0]


def safety(n):
    if "delete-on-stop" in (str(n.get("features", "")) + str(n.get("flags", ""))):
        raise Failure("Delete-on-stop instance must not be touched")


def wait_state(uid, desired, timeout=150, started_after=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        n = node(uid)
        if n.get("state") == desired:
            return n
        if desired == "running" and started_after is not None and n.get("state") == "stopped" and n.get("start_count", 0) > started_after:
            raise Failure("Boot failed before running: stop_reason=" + str(n.get("stop_reason")) +
                          " stop_code=" + str(n.get("stop_code")))
        time.sleep(2)
    raise Failure("Instance did not reach " + desired)


def stop(n):
    safety(n)
    uid = n["uuid"]
    if n["state"] == "stopped":
        return n
    if n["state"] in ("draining", "stopping"):
        return wait_state(uid, "stopped")
    if n["state"] not in ("running", "starting"):
        raise Failure("Refusing stop from unexpected state " + n["state"])
    api(METRO, "PUT", "/instances/" + uid + "/stop",
        {"force": False, "drain_timeout_ms": 3000, "quick": False, "ifstate": n["state"]})
    return wait_state(uid, "stopped")


def start(uid):
    n = node(uid)
    if n["state"] == "running":
        return n
    if n["state"] in ("stopping", "draining"):
        n = wait_state(uid, "stopped")
    if n["state"] != "stopped":
        return wait_state(uid, "running")
    previous = n.get("start_count", 0)
    api(METRO, "PUT", "/instances/" + uid + "/start", {"timeout_s": 0})
    return wait_state(uid, "running", started_after=previous)


def set_image(uid, reference):
    api(METRO, "PATCH", "/instances/" + uid,
        {"prop": "image", "op": "set", "value": {"url": reference, "pull_policy": "always"}})
    n = node(uid)
    if n.get("state") != "stopped":
        raise Failure("Image writes require an explicitly stopped node")
    return n


def normalise(image):
    return image.replace("oci://unikraft.io/", "")


def healthy(argo, timeout=120):
    deadline = time.monotonic() + timeout
    last = 0
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request("https://" + argo + "/",
                headers={"User-Agent": "Mozilla/5.0 (compatible; NodeImageUpgrade/1.0)"})
            with OPENER.open(req, timeout=8) as r:
                last = r.status
                r.read(128)
        except (urllib.error.URLError, TimeoutError):
            last = 0
        if last == 200:
            return last
        time.sleep(3)
    raise Failure("Argo entrance did not recover for " + argo + ": HTTP " + str(last))


def public(n):
    return {"metro": METRO, "uuid": n["uuid"], "name": n["name"], "state": n["state"],
            "image": n.get("image"), "vcpus": n.get("vcpus"), "memory_mb": n.get("memory_mb"),
            "domains": domains(n)}


def main():
    if ACCOUNT not in EXPECTED or METRO not in EXPECTED[ACCOUNT]:
        raise Failure("account/metro scope guard failed")
    if not TOKEN or not NEW_IMAGE:
        raise Failure("Missing token or target image")
    if CONFIRM != "SWAP-NODE-IMAGE":
        raise Failure("Explicit swap confirmation missing")

    uid, fqdn, argo = EXPECTED[ACCOUNT][METRO]
    before = node(uid)
    if fqdn not in domains(before):
        raise Failure("Expected FQDN guard failed for " + METRO)
    safety(before)
    old_image = before.get("image", "")
    if not old_image:
        raise Failure("Could not read the current image reference")

    emit("SWAP_CHECKPOINT", {"before": public(before), "target_image": NEW_IMAGE, "utc": utc()})
    changed = False
    try:
        stop(before)
        set_image(uid, NEW_IMAGE)
        changed = True
        n = start(uid)
        if n["vcpus"] != before["vcpus"] or n["memory_mb"] != before["memory_mb"]:
            raise Failure("CPU/memory changed unexpectedly")
        if domains(n) != domains(before):
            raise Failure("Public domain changed unexpectedly")
        if n.get("image", "").split("@")[-1] == old_image.split("@")[-1] and n.get("image") == old_image:
            raise Failure("Image readback still shows the previous reference")
        status = healthy(argo)
        emit("SWAP_VERIFIED", {"node": public(n), "argo": argo, "argo_http": status,
                               "changed_from": old_image, "utc": utc()})
        return
    except Exception as error:
        emit("SWAP_FAILURE", {"metro": METRO, "detail": str(error), "rollback_requested": changed})
        if not changed:
            emit("SWAP_ABORTED_BEFORE_CHANGE", {"metro": METRO, "image_unchanged": old_image})
            raise
        try:
            current = node(uid)
            if current["state"] != "stopped":
                stop(current)
            set_image(uid, normalise(old_image))
            n = start(uid)
            emit("ROLLBACK_VERIFIED", {"node": public(n), "argo": argo,
                                       "argo_http": healthy(argo), "restored_image": normalise(old_image)})
        except Exception as rollback_error:
            emit("ROLLBACK_NEEDS_ATTENTION", {"metro": METRO, "detail": str(rollback_error)})
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        emit("NODE_IMAGE_UPGRADE_ABORTED", {"metro": METRO, "account": ACCOUNT, "detail": str(error)})
        raise SystemExit(1)
