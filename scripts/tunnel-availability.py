#!/usr/bin/env python3
"""Repeated availability probe for one node's Argo tunnel.

Each round checks two things: whether Cloudflare still serves the tunnel
hostname (no connector shows up as 5xx), and whether data actually flows end to
end through the VLESS tunnel to the node's exit IP. Read-only on the platform.

Runs on a datacenter line so the result reflects the tunnel and origin, not the
local last mile or censorship path.
"""
import json
import os
import pathlib
import socket
import statistics
import subprocess
import sys
import tempfile
import time

EXPECTED = {
    "primary": {
        "sfo": ("a7c91ccc-ef4a-44de-941c-177bed5c81b8", "godlike2.xxfxx.kdns.fr", "23.81.176.109"),
        "sin": ("7c310f4f-5b0b-4cd0-9ba8-a72158974ddd", "sugasuga.xxfxx.kdns.fr", "173.234.10.239"),
    },
    "secondary": {
        "dal": ("8515c385-5c93-4281-946e-52e5d6ad2542", "zheshi222.mkvskg.dpdns.org", "172.241.224.90"),
        "was": ("576733ad-701c-4b15-b8b7-0e213de656d4", "zheshi111.mkvskg.dpdns.org", "209.50.250.193"),
    },
}
ACCOUNT = os.environ.get("ACCOUNT", "").strip()
METRO = os.environ.get("METRO", "").strip()
UUID = os.environ.get("UUID", "").strip()
ROUNDS = int(os.environ.get("ROUNDS", "20"))
INTERVAL_S = float(os.environ.get("INTERVAL_S", "3"))
XRAY_BIN = os.environ.get("XRAY_BIN", "/tmp/xray/xray")
PORT = 18081


class Failure(RuntimeError):
    pass


def emit(kind, value):
    print(kind + " " + json.dumps(value, ensure_ascii=False), flush=True)


def write_config(path, sni):
    config = {
        "log": {"loglevel": "error"},
        "inbounds": [{"tag": "in", "listen": "127.0.0.1", "port": PORT, "protocol": "socks",
                      "settings": {"udp": False, "auth": "noauth"}}],
        "outbounds": [{
            "tag": "out", "protocol": "vless",
            "settings": {"vnext": [{"address": sni, "port": 443,
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


def http_code(url, proxy=False, timeout=12):
    args = ["curl", "-s", "-o", "/dev/null", "-m", str(timeout),
            "-A", "Mozilla/5.0 (compatible; TunnelAvailability/1.0)",
            "-w", "%{http_code} %{time_total} %{remote_ip}"]
    if proxy:
        args += ["--socks5-hostname", "127.0.0.1:%d" % PORT]
    args.append(url)
    started = time.monotonic()
    out = subprocess.run(args, capture_output=True, text=True)
    elapsed = round((time.monotonic() - started) * 1000)
    parts = (out.stdout or "000 0 -").split()
    try:
        return {"code": parts[0], "seconds": float(parts[1]), "ip": parts[2], "ms": elapsed}
    except (ValueError, IndexError):
        return {"code": "000", "seconds": 0.0, "ip": "-", "ms": elapsed}


def summarise(name, rows, ok):
    if not rows:
        return {}
    good = [r for r in rows if ok(r)]
    worst = 0
    run = 0
    for r in rows:
        run = 0 if ok(r) else run + 1
        worst = max(worst, run)
    lat = sorted(r["ms"] for r in good)
    return {
        name: {
            "attempts": len(rows),
            "ok": len(good),
            "success_pct": round(len(good) / len(rows) * 100, 1),
            "median_ms": lat[len(lat) // 2] if lat else None,
            "p95_ms": lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else None,
            "max_ms": lat[-1] if lat else None,
            "longest_fail_streak": worst,
            "distinct_failures": sorted({r["detail"] for r in rows if not ok(r)})[:6],
        }
    }


def main():
    if ACCOUNT not in EXPECTED or METRO not in EXPECTED[ACCOUNT]:
        raise Failure("account/metro scope guard failed")
    if not UUID:
        raise Failure("UUID missing")
    uid, sni, expect_ip = EXPECTED[ACCOUNT][METRO]

    tmp = tempfile.mkdtemp(prefix="avail-")
    cfg = os.path.join(tmp, "config.json")
    write_config(cfg, sni)
    xray = subprocess.Popen([XRAY_BIN, "run", "-c", cfg],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_port():
            raise Failure("xray socks port did not open")

        entrance, tunnel = [], []
        for i in range(ROUNDS):
            e = http_code("https://" + sni + "/")
            e["detail"] = "HTTP " + e["code"] if e["code"] != "200" else ""
            entrance.append(e)

            t = http_code("https://api.ipify.org", proxy=True)
            t["ok_ip"] = t["ip"] == expect_ip
            t["detail"] = ("ok" if t["ok_ip"] else
                           ("exit ip " + t["ip"] if t["code"] == "200" else "HTTP " + t["code"]))
            tunnel.append(t)
            print("round %2d  entrance=%s(%sms)  tunnel=%s ip=%s" %
                  (i + 1, e["code"], e["ms"], t["code"], t["ip"]), flush=True)
            if i + 1 < ROUNDS:
                time.sleep(INTERVAL_S)

        report = {"account": ACCOUNT, "metro": METRO, "sni": sni, "expected_exit_ip": expect_ip,
                  "rounds": ROUNDS, "interval_s": INTERVAL_S}
        report.update(summarise("entrance", entrance, lambda r: r["code"] == "200"))
        report.update(summarise("tunnel", tunnel, lambda r: r["ok_ip"]))
        emit("AVAILABILITY", report)
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
        emit("AVAILABILITY_ABORTED", {"account": ACCOUNT, "metro": METRO, "detail": str(error)})
        sys.exit(1)
