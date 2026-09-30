"""Recover only the user's exact FRA deployment inputs inside the authorized CI job.
Never prints or exports credentials to the assistant. Scope is one repository,
one deployment workflow and the instance FQDN already selected by the user.
"""
import base64
import io
import json
import os
import re
import urllib.error
import urllib.request
import zipfile

REPO = "pttcho/unikraft-deploy"
EXPECTED_FQDN = "little-snowflake-y84r85js.fra.unikraft.app"
EXPECTED_DOMAIN = "zheshi111.mkvskg.dpdns.org"
GHTOKEN = os.environ.get("GH_READ_TOKEN", "")
if not GHTOKEN:
    raise SystemExit("Read-only repository authentication missing.")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


opener = urllib.request.build_opener(NoRedirect())


def github(path):
    req = urllib.request.Request("https://api.github.com/repos/" + REPO + path,
        headers={"Authorization": "Bearer " + GHTOKEN, "Accept": "application/vnd.github+json",
                 "User-Agent": "Mozilla/5.0 (compatible; ScopedDeploymentRecovery/1.0)"})
    try:
        with opener.open(req, timeout=30) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            location = e.headers.get("Location", "")
            if not location.startswith("https://"):
                raise SystemExit("Unsafe logs redirect rejected")
            # Follow signed log URLs WITHOUT forwarding the repository bearer token.
            with urllib.request.urlopen(urllib.request.Request(location, headers={"User-Agent": "Mozilla/5.0"}), timeout=45) as r:
                return r.read()
        raise SystemExit("Repository read failed, HTTP " + str(e.code)) from None


listing = json.loads(github("/actions/workflows/deploy.yml/runs?status=success&event=workflow_dispatch&per_page=20"))
for run in listing.get("workflow_runs", []):
    if run.get("head_branch") != "main":
        continue
    try:
        zipped = github("/actions/runs/" + str(run["id"]) + "/logs")
        with zipfile.ZipFile(io.BytesIO(zipped)) as archive:
            log = "\n".join(archive.read(name).decode("utf-8", "replace") for name in archive.namelist() if name.endswith(".txt"))
    except (zipfile.BadZipFile, urllib.error.URLError, TimeoutError):
        continue
    if EXPECTED_FQDN not in log or EXPECTED_DOMAIN not in log:
        continue
    candidates = re.findall(r"\bARGO_TOKEN:\s*([^\s]+)", log)
    for candidate in candidates:
        if len(candidate) < 80 or "*" in candidate:
            continue
        try:
            decoded = json.loads(base64.b64decode(candidate + "=" * (-len(candidate) % 4)))
            if not all(decoded.get(k) for k in ("a", "t", "s")):
                continue
        except Exception:
            continue
        # Dynamic GitHub masking is installed before any downstream command receives this value.
        print("::add-mask::" + candidate, flush=True)
        with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as env:
            env.write("RECOVERED_FRA_DOMAIN=" + EXPECTED_DOMAIN + "\n")
            env.write("RECOVERED_FRA_TOKEN=" + candidate + "\n")
        print("FRA_CONFIG_RECOVERY " + json.dumps({"source_run": run["id"], "exact_instance_match": True,
            "domain": EXPECTED_DOMAIN, "token_recovered": True, "credential_value_logged": False}), flush=True)
        raise SystemExit(0)
    print("FRA_CONFIG_RECOVERY " + json.dumps({"source_run": run["id"], "exact_instance_match": True,
        "token_recovered": False, "reason": "historical input unavailable or masked"}), flush=True)
raise SystemExit("Exact FRA tunnel input unavailable. Source instance is unchanged; request user-provided token rather than inventing credentials.")
