import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


BASE = "http://127.0.0.1:8787"
SECRETS = Path(r"F:\officeProject\yigehui\aimili-vpngate\vpngate_data\pool_secrets.json")
REPORT = Path(r"F:\officeProject\yigehui\aimili-vpngate\exports\reports\pool_proxy_lifetime_report.json")
PING_URL = "http://api.ipify.org?format=json"
READY_TIMEOUT_SECONDS = 9 * 60
PROBE_INTERVAL_SECONDS = 60
MAX_PROBES = 10


def load_token() -> str:
    return json.loads(SECRETS.read_text(encoding="utf-8"))["api_token"]


def api_get(path: str) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}",
        headers={"Authorization": f"Bearer {load_token()}"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def proxy_probe(proxy_url: str) -> dict:
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    )
    started = time.time()
    with opener.open(PING_URL, timeout=20) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    return {
        "ok": True,
        "latency_ms": int((time.time() - started) * 1000),
        "body": body,
    }


def save(data: dict) -> None:
    REPORT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    data: dict = {
        "started_at": time.time(),
        "base": BASE,
        "ready_timeout_seconds": READY_TIMEOUT_SECONDS,
        "probe_interval_seconds": PROBE_INTERVAL_SECONDS,
        "max_probes": MAX_PROBES,
        "events": [],
    }
    save(data)

    deadline = time.time() + READY_TIMEOUT_SECONDS
    proxy = None
    while time.time() < deadline:
        try:
            status = api_get("/api/pool/status?detail=1")
            event = {
                "ts": time.time(),
                "kind": "status_poll",
                "slots": status.get("slots", {}),
                "slot_detail": status.get("slot_detail", []),
            }
            data["events"].append(event)
            if status.get("slots", {}).get("ready", 0) > 0:
                got = api_get("/api/pool/proxies/random?require_exit_ip=1")
                proxy = got.get("proxy")
                data["proxy_fetched_at"] = time.time()
                data["proxy"] = proxy
                data["events"].append(
                    {
                        "ts": data["proxy_fetched_at"],
                        "kind": "proxy_fetched",
                        "proxy": proxy,
                    }
                )
                save(data)
                break
        except Exception as exc:
            data["events"].append({"ts": time.time(), "kind": "status_error", "error": repr(exc)})
        save(data)
        time.sleep(10)

    if not proxy:
        data["finished_at"] = time.time()
        data["result"] = "no_proxy_ready_within_timeout"
        save(data)
        return 2

    time.sleep(PROBE_INTERVAL_SECONDS)
    for idx in range(1, MAX_PROBES + 1):
        probe_event = {"ts": time.time(), "kind": "proxy_probe", "probe_index": idx}
        try:
            result = proxy_probe(proxy["http"])
            probe_event.update(result)
        except Exception as exc:
            probe_event["ok"] = False
            probe_event["error"] = repr(exc)
            try:
                probe_event["status"] = api_get("/api/pool/status?detail=1")
            except Exception as status_exc:
                probe_event["status_error"] = repr(status_exc)
            data["events"].append(probe_event)
            data["failed_at"] = probe_event["ts"]
            data["lifetime_seconds_from_fetch"] = int(probe_event["ts"] - data["proxy_fetched_at"])
            data["result"] = "proxy_failed"
            data["finished_at"] = time.time()
            save(data)
            return 1

        try:
            probe_event["status"] = api_get("/api/pool/status?detail=1")
        except Exception as exc:
            probe_event["status_error"] = repr(exc)
        data["events"].append(probe_event)
        data["last_success_at"] = probe_event["ts"]
        data["lifetime_seconds_from_fetch"] = int(probe_event["ts"] - data["proxy_fetched_at"])
        save(data)
        if idx < MAX_PROBES:
            time.sleep(PROBE_INTERVAL_SECONDS)

    data["finished_at"] = time.time()
    data["result"] = "proxy_alive_through_max_probes"
    save(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
