#!/usr/bin/env python3
"""Read-only CPU / network metrics for the four proxy nodes.

Only calls GET /instances/metrics. Nothing is mutated. Used to tell apart
"Unikraft throttling", "CPU saturated on the 1 vCPU" and "tunnel/origin slow".

Counters are cumulative since last start, so the script samples twice and
reports both the since-start averages and a short-window rate.
"""
import json
import os
import time
import urllib.error
import urllib.request

EXPECTED = {
    "primary": {
        "sfo": "a7c91ccc-ef4a-44de-941c-177bed5c81b8",
        "sin": "7c310f4f-5b0b-4cd0-9ba8-a72158974ddd",
    },
    "secondary": {
        "dal": "8515c385-5c93-4281-946e-52e5d6ad2542",
        "was": "576733ad-701c-4b15-b8b7-0e213de656d4",
    },
}
ACCOUNT = os.environ.get("ACCOUNT", "").strip()
TOKEN = os.environ.get("UNIKRAFT_TOKEN", "").strip()
INTERVAL = float(os.environ.get("INTERVAL_S", "15"))
METROS = ("sin", "sfo", "fra", "dal", "was")


class Failure(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(NoRedirect())


def emit(kind, value):
    print(kind + " " + json.dumps(value, ensure_ascii=False), flush=True)


def api(metro, path):
    if metro not in METROS or not TOKEN:
        raise Failure("API scope/authentication guard failed")
    req = urllib.request.Request("https://api." + metro + ".unikraft.cloud/v1" + path,
        headers={"Authorization": "Bearer " + TOKEN, "Accept": "application/json",
                 "User-Agent": "Mozilla/5.0 (compatible; ProxyMetrics/1.0)"}, method="GET")
    for attempt in range(3):
        try:
            with OPENER.open(req, timeout=30) as response:
                obj = json.load(response)
            break
        except urllib.error.HTTPError as error:
            if error.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2)
                continue
            raise Failure("GET " + metro + path + " HTTP " + str(error.code)) from None
        except (urllib.error.URLError, TimeoutError):
            if attempt < 2:
                time.sleep(2)
                continue
            raise Failure("GET " + metro + path + " network failure") from None
    if obj.get("status") != "success":
        raise Failure("API non-success for " + metro)
    return obj.get("data", {})


def metric(metro, uid):
    rows = api(metro, "/instances/metrics?uuid=" + uid).get("instances", [])
    if not rows:
        raise Failure("No metrics returned for " + metro)
    return rows[0]


def mbit(byte_delta, seconds):
    return byte_delta * 8 / 1e6 / seconds if seconds > 0 else 0.0


def snapshot(metro, uid):
    m = metric(metro, uid)
    return {
        "name": m.get("name"),
        "state": m.get("state"),
        "uptime_ms": m.get("uptime_ms", 0),
        "start_count": m.get("start_count", 0),
        "cpu_time_ms": m.get("cpu_time_ms", 0),
        "rss_mb": round(m.get("rss_bytes", 0) / 1048576, 1),
        "tx_bytes": m.get("tx_bytes", 0),
        "rx_bytes": m.get("rx_bytes", 0),
        "nconns": m.get("nconns", 0),
        "ntotal": m.get("ntotal", 0),
        "boot_time_us": m.get("boot_time_us", 0),
    }


def report(metro, uid):
    a = snapshot(metro, uid)
    time.sleep(INTERVAL)
    b = snapshot(metro, uid)
    up_s = b["uptime_ms"] / 1000
    since_cpu = (b["cpu_time_ms"] / b["uptime_ms"] * 100) if b["uptime_ms"] else 0
    since_tx = mbit(b["tx_bytes"], up_s)
    since_rx = mbit(b["rx_bytes"], up_s)
    dt = (b["uptime_ms"] - a["uptime_ms"]) / 1000
    if b["start_count"] != a["start_count"] or dt <= 0:
        window = {"note": "restarted between samples; window rate skipped"}
    else:
        window = {
            "cpu_pct": round((b["cpu_time_ms"] - a["cpu_time_ms"]) / (dt * 1000) * 100, 1),
            "tx_mbps": round(mbit(b["tx_bytes"] - a["tx_bytes"], dt), 2),
            "rx_mbps": round(mbit(b["rx_bytes"] - a["rx_bytes"], dt), 2),
            "nconns": b["nconns"],
            "ntotal_delta": b["ntotal"] - a["ntotal"],
            "seconds": round(dt, 1),
        }
    return {
        "metro": metro,
        "name": b["name"],
        "state": b["state"],
        "uptime_h": round(up_s / 3600, 2),
        "rss_mb": b["rss_mb"],
        "boot_time_us": b["boot_time_us"],
        "since_start": {
            "cpu_pct_of_1vcpu": round(since_cpu, 1),
            "tx_mbps": round(since_tx, 2),
            "rx_mbps": round(since_rx, 2),
            "tx_gb": round(b["tx_bytes"] / 1e9, 2),
            "rx_gb": round(b["rx_bytes"] / 1e9, 2),
        },
        "window": window,
    }


def main():
    if ACCOUNT not in EXPECTED or not TOKEN:
        raise Failure("Missing account authentication")
    out = []
    for metro, uid in EXPECTED[ACCOUNT].items():
        try:
            out.append(report(metro, uid))
        except Exception as error:
            emit("METRIC_FAILURE", {"metro": metro, "detail": str(error)})
            out.append({"metro": metro, "error": str(error)})
    emit("NODE_METRICS", {"account": ACCOUNT, "interval_s": INTERVAL, "instances": out})


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        emit("METRICS_ABORTED", {"detail": str(error)})
        raise SystemExit(1)
