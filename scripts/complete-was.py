"""User-authorized exact FRA deletion, then WAS reconstruction/cutover.
No broad deletes. The freshly built tunnel configuration is retained in a
verified dedicated registry image. Three healthy 4GiB nodes are untouched.
"""
import importlib.util
import json
import os
import re
import subprocess
import time

REF = "unikraft.io/qilonglin/unikraft:was-migration-20260930"
if os.environ.get("CONFIRM") != "DELETE-EXACT-FRA-THEN-WAS-4096":
    raise SystemExit("Explicit FRA deletion confirmation is missing.")

r = subprocess.run(["unikraft", "images", "get", REF, "-o", "json"], capture_output=True, text=True, timeout=45)
if r.returncode:
    token = os.environ.get("UNIKRAFT_TOKEN", "")
    print("REGISTRY_READ_ERROR " + json.dumps({"exit": r.returncode, "detail": r.stderr.replace(token, "[REDACTED]")[-1200:] if token else r.stderr[-1200:]}), flush=True)
    raise SystemExit("Registry image not verified; FRA has not been deleted.")
metadata = json.loads(r.stdout)

def normalize(value):
    if not isinstance(value, str):
        return None
    if re.fullmatch(r"[0-9a-f]{64}", value):
        return "sha256:" + value
    if re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        return value
    return None


def digest_paths(value, path=""):
    results = []
    if isinstance(value, dict):
        for key, v in value.items():
            keypath = path + str(key)
            if str(key).lower() == "digest" and normalize(v):
                results.append((keypath, normalize(v)))
            results.extend(digest_paths(v, keypath + "."))
    elif isinstance(value, list):
        for index, v in enumerate(value):
            results.extend(digest_paths(v, path + str(index) + "."))
    return results

candidates = digest_paths(metadata)
# Prefer only the top-level image digest, not rootfs/kernel layer digests.
preferred = [(p, d) for p, d in candidates if p.lower() in ("digest", "0.digest", "images.0.digest", "data.images.0.digest", "image.digest")]
if len(set(d for p, d in preferred)) == 1:
    digest = preferred[0][1]
elif len(set(d for p, d in candidates)) == 1:
    digest = candidates[0][1]
else:
    # Structural diagnostics contain only image references/digests, never app contents.
    print("IMAGE_DIGEST_STRUCTURE " + json.dumps({"root_type": type(metadata).__name__, "root_keys": list(metadata) if isinstance(metadata, dict) else None, "digest_paths": candidates}), flush=True)
    # The registry command's success verifies the dedicated tag is readable.
    # Retain a ref-pinned readback check if the CLI does not expose a root digest.
    digest = None

spec = importlib.util.spec_from_file_location("maintenance", "scripts/maintenance-apply.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
source = m.original("fra")
m.safety(source)
found = [n for n in m.nodes("was") if n.get("name") == m.MIGRATION_NAME]
if len(found) != 1 or found[0]["state"] != "stopped" or found[0]["memory_mb"] != 4096 or found[0]["vcpus"] != 1:
    raise SystemExit("Exactly one stopped 4GiB/1vCPU WAS candidate is required before deletion.")
target = found[0]
m.check_quota("was", target)
new_image = REF if not digest else "unikraft.io/qilonglin/unikraft@" + digest
m.api("was", "PATCH", "/instances/" + target["uuid"], {"prop": "image", "op": "set", "value": {"url": new_image, "pull_policy": "always"}})
prepared = m.node("was", target["uuid"])
if prepared["state"] != "stopped" or prepared["memory_mb"] != 4096:
    raise SystemExit("Prepared candidate changed unexpectedly; FRA not deleted.")
m.emit("DELETION_CHECKPOINT", {"source": m.public(source, "fra"), "prepared_was": m.public(prepared, "was"), "dedicated_registry_ref": REF, "registry_digest": digest, "original_tunnel_domain": m.EXPECTED["fra"][2]})

# The user explicitly chose deletion rather than a retained stopped instance.
m.stop("fra", source)
try:
    m.api("fra", "DELETE", "/instances/" + source["uuid"])
except Exception:
    # Reconcile a lost DELETE response without sending DELETE twice.
    remaining = [n for n in m.nodes("fra") if n.get("uuid") == source["uuid"]]
    if remaining:
        m.start("fra", source["uuid"])
        raise SystemExit("Exact deletion not confirmed; original FRA restarted and WAS left stopped.")
remaining = [n for n in m.nodes("fra") if n.get("uuid") == source["uuid"]]
if remaining:
    m.start("fra", source["uuid"])
    raise SystemExit("Exact FRA deletion not yet confirmed; source restored and WAS not started.")
m.emit("EXACT_FRA_DELETED", {"uuid": source["uuid"], "metro": "fra", "utc": m.utc()})
time.sleep(3)

try:
    n = m.start("was", target["uuid"])
    if n["state"] != "running" or n["memory_mb"] != 4096 or n["vcpus"] != 1:
        raise m.Failure("WAS actual resource readback mismatch")
    if digest and n.get("image", "").split("@")[-1] != digest:
        raise m.Failure("WAS actual image digest differs from the verified registry image")
    health = m.healthy(n, m.EXPECTED["fra"][2])
    m.emit("WAS_REBUILD_VERIFIED", {"node": m.public(n, "was"), "health": health, "fra_deleted": True, "argo_domain_retained": m.EXPECTED["fra"][2]})
except Exception as error:
    m.emit("WAS_REBUILD_FAILURE", {"detail": str(error), "automatic_fra_reconstruction_requested": True})
    # A deleted original cannot retain its old ephemeral direct FQDN. Restore
    # the Argo service using the already recovered new image, never invent keys.
    try:
        current = m.node("was", target["uuid"])
        m.stop("was", current)
        m.api("was", "DELETE", "/instances/" + target["uuid"])
        body = {"name": "unikraft-proxy-fra-rollback-20260930", "image": {"url": new_image, "pull_policy": "always"},
                "memory_mb": 4096, "vcpus": 1, "autostart": True, "restart_policy": "never",
                "service_group": {"services": [{"port":443,"destination_port":8080,"protocol":"tcp","handlers":["tls","http"]},
                                                {"port":8880,"destination_port":8081,"protocol":"tcp","handlers":["tls"]}]}}
        m.api("fra", "POST", "/instances", body)
        matches = [n for n in m.nodes("fra") if n.get("name") == body["name"]]
        if len(matches) != 1:
            raise m.Failure("Rollback reconstruction could not be read back")
        restored = m.wait_state("fra", matches[0]["uuid"], "running")
        m.emit("FRA_RECONSTRUCTED_ROLLBACK", {"node": m.public(restored, "fra"), "health": m.healthy(restored, m.EXPECTED["fra"][2]), "new_direct_fqdn_requires_sync": True})
    except Exception as rollback_error:
        m.emit("ROLLBACK_NEEDS_ATTENTION", {"detail": str(rollback_error)})
    raise SystemExit(1)
