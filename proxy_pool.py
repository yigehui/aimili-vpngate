#!/usr/bin/env python3
"""Proxy pool manager: fixed slots, OpenVPN-per-slot, list/random/status helpers.

Does not import vpngate_manager. OpenVPN/listener lifecycle is injected.
"""
from __future__ import annotations

import json
import os
import random
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

SLOT_EMPTY = "EMPTY"
SLOT_STARTING = "STARTING"
SLOT_READY = "READY"
SLOT_DRAINING = "DRAINING"

LogFn = Callable[..., None]
StartOpenVpnFn = Callable[[str, str], tuple[bool, str, Any]]
StopOpenVpnFn = Callable[[Any], None]
CreateListenerFn = Callable[..., Any]
WriteConfigFn = Callable[[dict[str, Any], Path], None]


def extract_api_token(headers: dict[str, str] | Any) -> str | None:
    """Extract API token from Authorization Bearer or X-API-Token headers."""
    get = headers.get if hasattr(headers, "get") else (lambda _k, _d=None: None)
    auth = get("Authorization") or get("authorization")
    if auth:
        scheme, _, rest = str(auth).strip().partition(" ")
        if scheme.lower() == "bearer" and rest:
            return rest.strip()
    tok = get("X-API-Token") or get("x-api-token")
    if tok:
        return str(tok).strip() or None
    return None


def token_matches(expected: str, provided: str | None) -> bool:
    """Constant-time compare of expected vs provided API token."""
    if not expected or provided is None:
        return False
    return secrets.compare_digest(str(expected), str(provided))


def load_or_create_pool_config(path: Path) -> dict[str, Any]:
    """Load pool secrets/config from JSON, creating defaults if missing.

    Env overrides (preferred when set):
      POOL_API_TOKEN, POOL_PROXY_USER, POOL_PROXY_PASS, POOL_SIZE, POOL_PORT_BASE,
      POOL_PUBLIC_HOST, POOL_LISTEN_HOST, POOL_API_RETURN_CREDENTIALS, POOL_MAX_STARTING
    """
    path = Path(path)
    file_data: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                file_data = loaded
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            file_data = {}

    api_token = (
        os.environ.get("POOL_API_TOKEN")
        or file_data.get("api_token")
        or ""
    )
    proxy_user = (
        os.environ.get("POOL_PROXY_USER")
        or file_data.get("proxy_user")
        or ""
    )
    proxy_pass = (
        os.environ.get("POOL_PROXY_PASS")
        or file_data.get("proxy_pass")
        or ""
    )

    created = False
    if not api_token:
        api_token = secrets.token_urlsafe(32)
        created = True
    if not proxy_user:
        proxy_user = "pool_" + secrets.token_urlsafe(8)
        created = True
    if not proxy_pass:
        proxy_pass = secrets.token_urlsafe(16)
        created = True

    # Numeric / host fields: env overrides file, else defaults
    def _pick_int(env_name: str, file_key: str, default: int) -> int:
        raw = os.environ.get(env_name)
        if raw is not None and str(raw).strip() != "":
            return int(str(raw).strip())
        if file_key in file_data and file_data[file_key] is not None and str(file_data[file_key]).strip() != "":
            return int(file_data[file_key])
        return default

    def _pick_str(env_name: str, file_key: str, default: str) -> str:
        raw = os.environ.get(env_name)
        if raw is not None and str(raw).strip() != "":
            return str(raw).strip()
        val = file_data.get(file_key)
        if val is not None and str(val).strip() != "":
            return str(val).strip()
        return default

    def _pick_bool(env_name: str, file_key: str, default: bool) -> bool:
        raw = os.environ.get(env_name)
        if raw is not None and str(raw).strip() != "":
            return str(raw).strip().lower() in ("1", "true", "yes", "on")
        if file_key in file_data and file_data[file_key] is not None:
            val = file_data[file_key]
            if isinstance(val, bool):
                return val
            return str(val).strip().lower() in ("1", "true", "yes", "on")
        return default

    cfg: dict[str, Any] = {
        "api_token": str(api_token),
        "proxy_user": str(proxy_user),
        "proxy_pass": str(proxy_pass),
        "pool_size": _pick_int("POOL_SIZE", "pool_size", 50),
        "port_base": _pick_int("POOL_PORT_BASE", "port_base", 52000),
        "public_host": _pick_str("POOL_PUBLIC_HOST", "public_host", ""),
        "listen_host": _pick_str("POOL_LISTEN_HOST", "listen_host", "0.0.0.0"),
        "return_credentials": _pick_bool(
            "POOL_API_RETURN_CREDENTIALS", "return_credentials", True
        ),
        "max_starting": _pick_int("POOL_MAX_STARTING", "max_starting", 5),
    }

    # Persist secrets when file missing or we generated new secrets
    if not path.is_file() or created:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Store secret-ish fields + useful defaults for re-read consistency
        to_write = {
            "api_token": cfg["api_token"],
            "proxy_user": cfg["proxy_user"],
            "proxy_pass": cfg["proxy_pass"],
            "pool_size": cfg["pool_size"],
            "port_base": cfg["port_base"],
            "public_host": cfg["public_host"],
            "listen_host": cfg["listen_host"],
            "return_credentials": cfg["return_credentials"],
            "max_starting": cfg["max_starting"],
        }
        path.write_text(json.dumps(to_write, indent=2) + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    return cfg


def parse_pool_query(qs: dict[str, list[str]]) -> dict[str, Any]:
    """Parse pool API query parameters from parse_qs-style dict.

    Raises ValueError on invalid integer fields.
    """
    def _first(key: str, default: str = "") -> str:
        vals = qs.get(key) if qs else None
        if not vals:
            return default
        return str(vals[0]) if vals[0] is not None else default

    def _int_field(key: str, default: int = 0) -> int:
        raw = _first(key, "")
        if raw == "" or raw is None:
            return default
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid {key}: {raw!r}") from exc

    detail_raw = _first("detail", "").strip().lower()
    detail = detail_raw in ("1", "true", "yes", "on")

    return {
        "country": _first("country", ""),
        "limit": _int_field("limit", 0),
        "offset": _int_field("offset", 0),
        "sort": _first("sort", "latency") or "latency",
        "protocol": _first("protocol", "all") or "all",
        "detail": detail,
    }


class PoolSlot:
    def __init__(self, index: int, port_base: int) -> None:
        self.index = index
        self.port_base = port_base
        self.state = SLOT_EMPTY
        self.node_id = ""
        self.node_ip = ""
        self.country = ""
        self.country_name = ""
        self.latency_ms = 0
        self.updated_at = 0.0
        self.process: Any = None
        self.listener: Any = None
        self.fail_count = 0
        self.last_error = ""
        self.config_path: Path | None = None

    @property
    def port(self) -> int:
        return self.port_base + self.index

    @property
    def tun_name(self) -> str:
        return f"tun{self.index}"


class PoolManager:
    def __init__(
        self,
        *,
        pool_size: int,
        port_base: int,
        public_host: str,
        listen_host: str,
        proxy_user: str,
        proxy_pass: str,
        return_credentials: bool,
        max_starting: int,
        start_openvpn: StartOpenVpnFn,
        stop_openvpn: StopOpenVpnFn,
        create_listener: CreateListenerFn,
        log: LogFn,
        write_config: WriteConfigFn | None = None,
        config_dir: str | Path | None = None,
    ) -> None:
        self.pool_size = int(pool_size)
        self.port_base = int(port_base)
        self.public_host = public_host
        self.listen_host = listen_host
        self.proxy_user = proxy_user
        self.proxy_pass = proxy_pass
        self.return_credentials = bool(return_credentials)
        self.max_starting = max(1, int(max_starting))
        self.start_openvpn = start_openvpn
        self.stop_openvpn = stop_openvpn
        self.create_listener = create_listener
        self.log = log
        self.write_config = write_config
        self.config_dir = Path(config_dir) if config_dir else None
        self.api_token = ""
        self.slots: list[PoolSlot] = [PoolSlot(i, self.port_base) for i in range(self.pool_size)]
        self._lock = threading.RLock()
        self._last_candidates: list[dict[str, Any]] = []
        self._skipped: dict[str, float] = {}
        self._started = False
        self._temp_config_dir: tempfile.TemporaryDirectory[str] | None = None

    def start(self) -> None:
        self._started = True

    def shutdown(self) -> None:
        with self._lock:
            for slot in self.slots:
                self._stop_slot(slot)
            self._started = False
            if self._temp_config_dir is not None:
                try:
                    self._temp_config_dir.cleanup()
                except Exception:
                    pass
                self._temp_config_dir = None

    def tick_health(self) -> None:
        # Task 5 expands health / replace; stub is fine for Task 3.
        return

    def list_proxies(
        self,
        country: str = "",
        limit: int = 0,
        offset: int = 0,
        sort: str = "latency",
    ) -> dict[str, Any]:
        with self._lock:
            slots = self._filtered_ready(country)
            slots = self._sort_slots(slots, sort)
            total = len(slots)
            off = max(0, int(offset or 0))
            if off:
                slots = slots[off:]
            lim = int(limit or 0)
            if lim > 0:
                slots = slots[:lim]
            proxies = [self._proxy_dict(s) for s in slots]
            return {
                "ok": True,
                "total": total,
                "count": len(proxies),
                "proxies": proxies,
            }

    def random_proxy(self, country: str = "") -> dict[str, Any] | None:
        with self._lock:
            slots = self._filtered_ready(country)
            if not slots:
                return None
            return self._proxy_dict(random.choice(slots))

    def status(self, detail: bool = False) -> dict[str, Any]:
        with self._lock:
            counts = {
                "ready": 0,
                "starting": 0,
                "empty": 0,
                "draining": 0,
            }
            for slot in self.slots:
                if slot.state == SLOT_READY:
                    counts["ready"] += 1
                elif slot.state == SLOT_STARTING:
                    counts["starting"] += 1
                elif slot.state == SLOT_DRAINING:
                    counts["draining"] += 1
                else:
                    counts["empty"] += 1
            result: dict[str, Any] = {
                "ok": True,
                "mode": "pool",
                "pool_size": self.pool_size,
                "port_base": self.port_base,
                "slots": counts,
                "proxy_auth": bool(self.proxy_user or self.proxy_pass),
                "public_host": self.public_host,
            }
            if detail:
                result["slot_detail"] = [
                    {
                        "index": s.index,
                        "port": s.port,
                        "state": s.state,
                        "node_id": s.node_id,
                        "country": s.country,
                        "latency_ms": s.latency_ms,
                        "fail_count": s.fail_count,
                        "last_error": s.last_error,
                    }
                    for s in self.slots
                ]
            return result

    def _proxy_dict(self, slot: PoolSlot) -> dict[str, Any]:
        host = self.public_host
        port = slot.port
        item: dict[str, Any] = {
            "id": slot.node_id,
            "slot": slot.index,
            "port": port,
            "host": host,
            "country": slot.country,
            "country_name": slot.country_name,
            "latency_ms": slot.latency_ms,
            "protocol": "http,socks5",
            "node_ip": slot.node_ip,
            "updated_at": slot.updated_at,
        }
        if self.return_credentials:
            user = self.proxy_user or ""
            password = self.proxy_pass or ""
            item["username"] = user
            item["password"] = password
            user_q = quote(user, safe="")
            pass_q = quote(password, safe="")
            auth = f"{user_q}:{pass_q}@" if (user or password) else ""
            item["http"] = f"http://{auth}{host}:{port}"
            item["socks5"] = f"socks5://{auth}{host}:{port}"
        else:
            item["http"] = f"http://{host}:{port}"
            item["socks5"] = f"socks5://{host}:{port}"
        return item

    def _parse_countries(self, country: str) -> list[str]:
        if not country or not str(country).strip():
            return []
        return [part.strip().casefold() for part in str(country).split(",") if part.strip()]

    def _country_match(self, slot_country: str, filters: list[str]) -> bool:
        if not filters:
            return True
        sc = (slot_country or "").casefold()
        for f in filters:
            if sc == f or sc.startswith(f) or f.startswith(sc):
                # equality or startswith either way covers short codes / prefixes
                if sc == f or sc.startswith(f):
                    return True
        return False

    def _filtered_ready(self, country: str) -> list[PoolSlot]:
        filters = self._parse_countries(country)
        ready: list[PoolSlot] = []
        for slot in self.slots:
            if slot.state != SLOT_READY:
                continue
            if not self._country_match(slot.country, filters):
                continue
            ready.append(slot)
        return ready

    def _sort_slots(self, slots: list[PoolSlot], sort: str) -> list[PoolSlot]:
        key = (sort or "latency").strip().lower()
        if key == "country":
            return sorted(slots, key=lambda s: ((s.country or "").casefold(), s.port))
        if key == "port":
            return sorted(slots, key=lambda s: s.port)
        # default latency
        return sorted(slots, key=lambda s: (s.latency_ms if s.latency_ms is not None else 10**9, s.port))

    def _config_root(self) -> Path:
        if self.config_dir is not None:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            return self.config_dir
        if self._temp_config_dir is None:
            self._temp_config_dir = tempfile.TemporaryDirectory(prefix="proxy_pool_")
        return Path(self._temp_config_dir.name)

    def _node_id(self, node: dict[str, Any]) -> str:
        return str(node.get("id") or node.get("node_id") or "")

    def _is_available(self, node: dict[str, Any]) -> bool:
        status = node.get("probe_status")
        if status is None or status == "":
            return True
        return str(status).strip().lower() == "available"

    def _dedupe_nodes(self, nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for node in nodes:
            if not self._is_available(node):
                continue
            nid = self._node_id(node)
            if not nid or nid in seen:
                continue
            seen.add(nid)
            out.append(node)
        return out

    def _latency_key(self, node: dict[str, Any]) -> float:
        for key in ("score_latency", "latency_ms", "latency"):
            if key in node and node[key] is not None:
                try:
                    return float(node[key])
                except (TypeError, ValueError):
                    pass
        return 10**9

    def sync_from_nodes(self, nodes: list[dict[str, Any]]) -> None:
        with self._lock:
            candidates = self._dedupe_nodes(list(nodes or []))
            candidates.sort(key=self._latency_key)
            self._last_candidates = list(candidates)
            available_ids = {self._node_id(n) for n in candidates}

            # Keep READY whose node_id still available; drain others
            for slot in self.slots:
                if slot.state == SLOT_READY and slot.node_id and slot.node_id not in available_ids:
                    self._stop_slot(slot)

            used_ids = {
                s.node_id
                for s in self.slots
                if s.state in (SLOT_READY, SLOT_STARTING) and s.node_id
            }
            unused = [n for n in candidates if self._node_id(n) not in used_ids]

            starting_count = sum(1 for s in self.slots if s.state == SLOT_STARTING)
            empty_slots = [s for s in self.slots if s.state == SLOT_EMPTY]

            for slot in empty_slots:
                if starting_count >= self.max_starting:
                    break
                if not unused:
                    break
                node = unused.pop(0)
                nid = self._node_id(node)
                until = self._skipped.get(nid)
                if until is not None and until > time.time():
                    continue
                ok = self._start_slot(slot, node)
                if ok:
                    starting_count += 1  # briefly counted; marked READY immediately on success
                    used_ids.add(nid)
                else:
                    # try next candidate for same slot
                    # (loop continues to next empty only if this slot stayed EMPTY)
                    if slot.state == SLOT_EMPTY:
                        # retry more candidates on this same slot
                        while unused and slot.state == SLOT_EMPTY:
                            node = unused.pop(0)
                            nid = self._node_id(node)
                            if nid in used_ids:
                                continue
                            until = self._skipped.get(nid)
                            if until is not None and until > time.time():
                                continue
                            if self._start_slot(slot, node):
                                used_ids.add(nid)
                                break

    def _start_slot(self, slot: PoolSlot, node: dict[str, Any]) -> bool:
        nid = self._node_id(node)
        slot.state = SLOT_STARTING
        slot.node_id = nid
        slot.node_ip = str(node.get("ip") or node.get("node_ip") or "")
        slot.country = str(node.get("country_short") or node.get("country") or "")
        slot.country_name = str(node.get("country") or node.get("country_name") or slot.country)
        try:
            slot.latency_ms = int(self._latency_key(node))
            if slot.latency_ms >= 10**9:
                slot.latency_ms = 0
        except Exception:
            slot.latency_ms = 0
        slot.last_error = ""
        slot.fail_count = 0

        try:
            root = self._config_root()
            safe_id = "".join(c if c.isalnum() or c in "-_." else "_" for c in nid) or f"slot{slot.index}"
            path = root / f"pool_{slot.index}_{safe_id}.ovpn"
            if self.write_config is not None:
                self.write_config(node, path)
            else:
                path.write_text(str(node.get("config_text") or ""), encoding="utf-8")
            slot.config_path = path

            ok, msg, process = self.start_openvpn(str(path), slot.tun_name)
            if not ok:
                slot.last_error = msg or "start_openvpn failed"
                self._skipped[nid] = time.time() + 60
                self._reset_slot_fields(slot)
                try:
                    self.log("PoolSlot", f"start failed slot={slot.index} node={nid}: {slot.last_error}")
                except Exception:
                    pass
                return False

            slot.process = process
            listener = self.create_listener(
                host=self.listen_host,
                port=slot.port,
                username=self.proxy_user,
                password=self.proxy_pass,
                bind_device=slot.tun_name,
                require_auth=True,
                max_connections=None,
            )
            if hasattr(listener, "start"):
                try:
                    listener.start(background=True)
                except TypeError:
                    listener.start()
            slot.listener = listener
            slot.updated_at = time.time()
            slot.state = SLOT_READY
            try:
                self.log("PoolSlot", f"READY slot={slot.index} port={slot.port} node={nid}")
            except Exception:
                pass
            return True
        except Exception as exc:
            slot.last_error = str(exc)
            self._skipped[nid] = time.time() + 60
            # best-effort cleanup
            if slot.listener is not None:
                try:
                    slot.listener.stop()
                except Exception:
                    pass
            if slot.process is not None:
                try:
                    self.stop_openvpn(slot.process)
                except Exception:
                    pass
            self._reset_slot_fields(slot)
            try:
                self.log("PoolSlot", f"start exception slot={slot.index} node={nid}: {exc}")
            except Exception:
                pass
            return False

    def _stop_slot(self, slot: PoolSlot) -> None:
        slot.state = SLOT_DRAINING
        if slot.listener is not None:
            try:
                stop = getattr(slot.listener, "stop", None)
                if callable(stop):
                    stop()
            except Exception:
                pass
            slot.listener = None
        if slot.process is not None:
            try:
                self.stop_openvpn(slot.process)
            except Exception:
                pass
            slot.process = None
        self._reset_slot_fields(slot)

    def _reset_slot_fields(self, slot: PoolSlot) -> None:
        slot.state = SLOT_EMPTY
        slot.node_id = ""
        slot.node_ip = ""
        slot.country = ""
        slot.country_name = ""
        slot.latency_ms = 0
        slot.updated_at = 0
        slot.process = None
        slot.listener = None
        slot.config_path = None
