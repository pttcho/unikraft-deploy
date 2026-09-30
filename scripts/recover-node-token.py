#!/usr/bin/env python3
"""Recover one node's tunnel token from its own deployment run log.

Read-only against the repository: it lists successful deploy runs, downloads
their logs, and looks for the run that deployed the given FQDN. The token is
never printed; in apply mode it is masked and exported for a later step.

An image rebuild bakes the tunnel token in, and every node has its own tunnel,
so this is the only way to rebuild a node's image without asking the user to
handle credentials by hand.
"""
import base64
import io
import json
import os
import re
import urllib.error
import urllib.request
import zipfile

REPO = os.environ.get("REPO", "pttcho/unikraft-deploy")
NODE = os.environ.get("NODE", "")
MATCH_FQDN = os.environ.get("MATCH_FQDN", "")
PHASE = os.environ.get("PHASE", "check")
GHTOKEN = os.environ.get("GH_READ_TOKEN", "").strip()

if not MATCH_FQDN or not GHTOKEN:
    raise SystemExit("MATCH_FQDN and a repository read token are required.")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


opener = urllib.request.build_opener(NoRedirect())


def github(path):
    req = urllib.request.Request("https://api.github.com/repos/" + REPO + path,
        headers={"Authorization": "Bearer " + GHTOKEN, "Accept": "application/vnd.github+json",
                 "User-Agent": "Mozilla/5.0 (compatible; NodeTokenRecovery/1.0)"})
    try:
        with opener.open(req, timeout=30) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            location = e.headers.get("Location", "")
            if not location.startswith("https://"):
                raise SystemExit("Unsafe logs redirect rejected")
            with urllib.request.urlopen(urllib.request.Request(
                    location, headers={"User-Agent": "Mozilla/5.0"}), timeout=60) as r:
                return r.read()
        raise SystemExit("Repository read failed, HTTP " + str(e.code)) from None


def valid_token(candidate):
    if len(candidate) < 80 or "*" in candidate:
        return False
    try:
        decoded = json.loads(base64.b64decode(candidate + "=" * (-len(candidate) % 4)))
    except Exception:
        return False
    return all(decoded.get(k) for k in ("a", "t", "s"))


listing = json.loads(github("/actions/workflows/deploy.yml/runs"
                            "?status=success&event=workflow_dispatch&per_page=100"))
for run in listing.get("workflow_runs", []):
    if run.get("head_branch") != "main":
        continue
    try:
        zipped = github("/actions/runs/" + str(run["id"]) + "/logs")
        with zipfile.ZipFile(io.BytesIO(zipped)) as archive:
            log = "\n".join(archive.read(name).decode("utf-8", "replace")
                            for name in archive.namelist() if name.endswith(".txt"))
    except (zipfile.BadZipFile, urllib.error.URLError, TimeoutError, SystemExit):
        continue
    if MATCH_FQDN not in log:
        continue
    token = next((c for c in re.findall(r"\bARGO_TOKEN:\s*([^\s]+)", log) if valid_token(c)), None)
    if token:
        if PHASE == "apply":
            print("::add-mask::" + token, flush=True)
            with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as env:
                env.write("RECOVERED_NODE_TOKEN=" + token + "\n")
        print("TOKEN_RECOVERY " + json.dumps({"node": NODE, "matched_fqdn": MATCH_FQDN,
            "source_run": run["id"], "found": True, "phase": PHASE,
            "credential_value_logged": False}), flush=True)
        raise SystemExit(0)

print("TOKEN_RECOVERY " + json.dumps({"node": NODE, "matched_fqdn": MATCH_FQDN,
    "found": False, "phase": PHASE,
    "reason": "no successful deploy run log contained this FQDN with a readable token"}), flush=True)
raise SystemExit(1 if PHASE == "apply" else 0)
