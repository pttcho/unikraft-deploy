#!/usr/bin/env python3
"""Sustained-load + high-frequency tx sampling: throttle or congestion?

Runs on a clean datacenter line, so the client leg cannot be the limit. It
opens parallel downloads through one proxy node and samples the instance's
cumulative counters (GET /instances/metrics, read-only) every couple of
seconds. The shape of the tx rate series is the answer:

  * flat, low, tightly clustered with low CPU  -> egress/bandwidth cap
  * jittery, wide spread                      -> congestion somewhere

Nothing is mutated; the only write is a temporary xray config on this runner.
"""
import json
import os
import pathlib
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

EXPECTED = {
    "primary": {
        "sfo": ("a7c91ccc-ef4a-44de-941c-177bed5c81b8", "godlike2.xxfxx.kdns.fr"),
        "sin": ("7c310f4f-5b0b-4cd0-9ba8-a72158974ddd", "sugasuga.xxfxx.kdns.fr"),
    },
    "secondary": {
        "dal": ("8515c385-5c93-4281-946e-52e5d6ad2542", "zheshi222.mkvskg.dpdns.org"),
        "was": ("576733ad-701c-4b15-b8b7-0e213de656d4", "zheshi111.mkvskg.dpdns.org"),
    },
}
# Entry address to dial. Empty = use the node's own tunnel domain, which resolves
# globally; saas.sin.fan may not resolve outside the intended region.
ENTRY = os.environ.get("ENTRY", "").strip()
ACCOUNT = os.environ.get("ACCOUNT", "").strip()
METRO = os.environ.get("METRO", "").strip()
TOKEN = os.environ.get("UNIKRAFT_TOKEN", "").strip()
UUID = os.environ.get("UUID", "").strip()
SECONDS = float(os.environ.get("SECONDS", "30"))
SAMPLE_S = float(os.environ.get("SAMPLE_S", "2"))
PARALLEL = int(os.environ.get("PARALLEL", "4"))
URL = os.environ.get("URL", "https://cachefly.cachefly.net/100mb.test")
XRAY_BIN = os.environ.get("XRAY_BIN", "/tmp/xray/xray")
PORT = 18081
METROS = ("sin", "sfo", "fra", "dal", "was")


class Failure(RuntimeError):
    pass


def emit(kind, value):
    print(kind + " " + json.dumps(value, ensure_ascii=False), flush=True)


def metrics(uid):
    req = urllib.request.Request(
        "https://api." + METRO + ".unikraft.cloud/v1/instances/metrics?uuid=" + uid,
        headers={"Authorization": "Bearer " + TOKEN, "Accept": "application/json",
                 "User-Agent": "Mozilla/5.0 (compatible; ThroughputProbe/1.0)"}, method="GET")
    with urllib.request.urlopen(req, timeout=25) as response:
        obj = json.load(response)
    if obj.get("status") != "success":
        raise Failure("metrics API non-success")
    rows = obj.get("data", {}).get("instances", [])
    if not rows:
        raise Failure("no metrics returned")
    return rows[0]


def write_config(path, sni):
    config = {
        "log": {"loglevel": "error"},
        "inbounds": [{"tag": "in", "listen": "127.0.0.1", "port": PORT, "protocol": "socks",
                      "settings": {"udp": False, "auth": "noauth"}}],
        "outbounds": [{
            "tag": "out", "protocol": "vless",
            "settings": {"vnext": [{"address": ENTRY, "port": 443,
                                    "users": [{"id": UUID, "encryption": "none", "flow": ""}]}]},
            "streamSettings": {"network": "ws", "security": "tls",
                               "tlsSettings": {"serverName": sni, "fingerprint": "chrome"},
                               "wsSettings": {"path": "/api/v2/stream", "headers": {"Host": sni}}},
        }],
    }
    pathlib.Path(path).write_text(json.dumps(config, indent=2), encoding="utf-8")


def wait_port(timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as s:
            s.settimeout(1)
            if s.connect_ex(("127.0.0.1", PORT)) == 0:
                return True
        time.sleep(0.3)
    return False


def load_worker(results, index):
    out = subprocess.run(
        ["curl", "-s", "-o", "/dev/null", "-A", "Mozilla/5.0 (compatible; ThroughputProbe/1.0)",
         "-m", str(int(SECONDS) + 2),
         "--socks5-hostname", "127.0.0.1:%d" % PORT,
         "-w", "%{speed_download} %{size_download} %{http_code}", URL],
        capture_output=True, text=True)
    parts = (out.stdout or "0 0 000").split()
    try:
        results[index] = (float(parts[0]), float(parts[1]), parts[2])
    except (ValueError, IndexError):
        results[index] = (0.0, 0.0, "000")


def main():
    if ACCOUNT not in EXPECTED or METRO not in EXPECTED[ACCOUNT]:
        raise Failure("account/metro scope guard failed")
    if not TOKEN or not UUID or METRO not in METROS:
        raise Failure("missing token/uuid or bad metro")
    uid, sni = EXPECTED[ACCOUNT][METRO]
    global ENTRY
    if not ENTRY:
        ENTRY = sni

    tmp = tempfile.mkdtemp(prefix="probe-")
    cfg = os.path.join(tmp, "config.json")
    write_config(cfg, sni)
    xray = subprocess.Popen([XRAY_BIN, "run", "-c", cfg],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_port():
            raise Failure("xray socks port did not open (bad config?)")
        # Prove the tunnel actually carries traffic before measuring anything.
        warm = subprocess.run(["curl", "-s", "-m", "20",
                               "--socks5-hostname", "127.0.0.1:%d" % PORT,
                               "https://api.ipify.org"], capture_output=True, text=True)
        exit_ip = (warm.stdout or "").strip()
        if not exit_ip:
            raise Failure("tunnel unreachable from this runner via " + ENTRY +
                          " (exit IP lookup returned nothing)")
        emit("PROBE_WARMUP", {"metro": METRO, "entry": ENTRY, "exit_ip": exit_ip})

        base = metrics(uid)
        series = []
        results = {}
        threads = [threading.Thread(target=load_worker, args=(results, i), daemon=True)
                   for i in range(PARALLEL)]
        started = time.time()
        for t in threads:
            t.start()

        last = base
        last_t = time.monotonic()
        while time.time() - started < SECONDS:
            time.sleep(SAMPLE_S)
            now = time.monotonic()
            try:
                cur = metrics(uid)
            except Exception as error:
                series.append({"error": str(error)})
                continue
            dt = now - last_t
            if cur.get("start_count") != last.get("start_count"):
                series.append({"note": "instance restarted during probe"})
                break
            series.append({
                "t_s": round(now - last_t, 2),
                "tx_mbps": round((cur["tx_bytes"] - last["tx_bytes"]) * 8 / 1e6 / dt, 2),
                "cpu_pct": round((cur["cpu_time_ms"] - last["cpu_time_ms"]) / (dt * 1000) * 100, 1),
                "nconns": cur.get("nconns", 0),
            })
            last, last_t = cur, now

        for t in threads:
            t.join(timeout=SECONDS + 15)
        total_bytes = sum(v[1] for v in results.values())
        if total_bytes <= 0:
            codes = sorted({v[2] for v in results.values()})
            raise Failure("load generation downloaded nothing (http " + ",".join(codes) +
                          "); pick another URL with the url input")
        final = metrics(uid)

        rates = [s["tx_mbps"] for s in series if "tx_mbps" in s]
        cpus = [s["cpu_pct"] for s in series if "cpu_pct" in s]
        verdict = {}
        if len(rates) >= 4:
            med = statistics.median(rates)
            sd = statistics.pstdev(rates)
            cv = (sd / med) if med else 0
            near = sum(1 for r in rates if med and abs(r - med) / med <= 0.15) / len(rates)
            if cv <= 0.2 and near >= 0.6:
                shape = "FLAT / capped-looking"
            elif cv >= 0.45:
                shape = "JITTERY / congestion-looking"
            else:
                shape = "mixed"
            verdict = {
                "median_mbps": round(med, 2), "max_mbps": round(max(rates), 2),
                "min_mbps": round(min(rates), 2), "stdev": round(sd, 2),
                "cv": round(cv, 2), "within_15pct_of_median": round(near, 2),
                "cpu_pct_peak": max(cpus) if cpus else None,
                "shape": shape,
            }
        emit("PROBE_RESULT", {
            "account": ACCOUNT, "metro": METRO, "name": final.get("name"), "entry": ENTRY,
            "sni": sni, "exit_ip": exit_ip, "parallel": PARALLEL, "seconds": SECONDS, "url": URL,
            "series": series,
            "workers": [{"mbps": round(v[0] * 8 / 1e6, 2), "mb": round(v[1] / 1048576, 2),
                         "http": v[2]} for v in results.values()],
            "verdict": verdict,
        })
    finally:
        xray.terminate()
        try:
            xray.wait(timeout=5)
        except Exception:
            xray.kill()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        emit("PROBE_ABORTED", {"account": ACCOUNT, "metro": METRO, "detail": str(error)})
        sys.exit(1)
