#!/usr/bin/env python3
"""Proxy pool manager: fixed slots, OpenVPN-per-slot, list/random/status helpers.

Does not import vpngate_manager. OpenVPN/listener lifecycle is injected.
"""
from __future__ import annotations

import json
import os
import random
import secrets
import socket
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
HealthCheckFn = Callable[[Any], tuple[bool, str, dict[str, Any]] | bool]
CleanupPortFn = Callable[[str, int], bool]


class ShadowCandidate:
    def __init__(self, *, index: int, tun_name: str, port: int) -> None:
        self.index = index
        self.tun_name = tun_name
        self.port = port
        self.node_id = ""
        self.node_ip = ""
        self.country = ""
        self.country_name = ""
        self.ip_type = ""
        self.entry_ip_type = ""
        self.latency_ms = 0
        self.exit_ip = ""
        self.health_latency_ms = 0
        self.last_error = ""
        self.started_at = 0.0
        self.process: Any = None
        self.listener: Any = None
        self.config_path: Path | None = None


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
      POOL_PUBLIC_HOST, POOL_LISTEN_HOST, POOL_API_RETURN_CREDENTIALS, POOL_MAX_STARTING,
      POOL_SLOT_START_TIMEOUT, POOL_REQUIRE_EXIT_IP
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
        "max_shadow_starting": _pick_int("POOL_MAX_SHADOW_STARTING", "max_shadow_starting", 5),
        "slot_start_timeout": _pick_int("POOL_SLOT_START_TIMEOUT", "slot_start_timeout", 90),
        "replacement_grace_seconds": _pick_int("POOL_REPLACEMENT_GRACE_SECONDS", "replacement_grace_seconds", 180),
        "shadow_port_base": _pick_int("POOL_SHADOW_PORT_BASE", "shadow_port_base", 53000),
        "shadow_port_count": _pick_int("POOL_SHADOW_PORT_COUNT", "shadow_port_count", 200),
        "require_exit_ip": _pick_bool("POOL_REQUIRE_EXIT_IP", "require_exit_ip", True),
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
            "max_shadow_starting": cfg["max_shadow_starting"],
            "slot_start_timeout": cfg["slot_start_timeout"],
            "replacement_grace_seconds": cfg["replacement_grace_seconds"],
            "shadow_port_base": cfg["shadow_port_base"],
            "shadow_port_count": cfg["shadow_port_count"],
            "require_exit_ip": cfg["require_exit_ip"],
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
    fallback_unknown_raw = _first(
        "fallback_unknown",
        _first("include_unknown_ip_type", _first("allow_unknown_ip_type", "")),
    ).strip().lower()
    fallback_unknown = fallback_unknown_raw in ("1", "true", "yes", "on")
    strict_raw = _first("strict", "").strip().lower()
    if strict_raw in ("0", "false", "no", "off"):
        fallback_unknown = True
    require_exit_raw = _first("require_exit_ip", _first("strict_exit_ip", "")).strip().lower()
    require_exit_ip = None
    if require_exit_raw in ("1", "true", "yes", "on"):
        require_exit_ip = True
    elif require_exit_raw in ("0", "false", "no", "off"):
        require_exit_ip = False

    return {
        "country": _first("country", ""),
        "limit": _int_field("limit", 0),
        "offset": _int_field("offset", 0),
        "sort": _first("sort", "latency") or "latency",
        "protocol": _first("protocol", "all") or "all",
        "ip_type": _first("ip_type", _first("type", "all")) or "all",
        "detail": detail,
        "fallback_unknown": fallback_unknown,
        "require_exit_ip": require_exit_ip,
    }


class PoolSlot:
    def __init__(self, index: int, port_base: int) -> None:
        self.index = index
        self.port_base = port_base
        self.device_name = f"tun{self.index}"
        self.state = SLOT_EMPTY
        self.node_id = ""
        self.node_ip = ""
        self.country = ""
        self.country_name = ""
        self.ip_type = ""
        self.entry_ip_type = ""
        self.latency_ms = 0
        self.updated_at = 0.0
        self.starting_at = 0.0
        self.last_health_at = 0.0
        self.health_latency_ms = 0
        self.exit_ip = ""
        self.process: Any = None
        self.listener: Any = None
        self.fail_count = 0
        self.last_error = ""
        self.config_path: Path | None = None
        self.replacement_pending = False
        self.replacement_reason = ""
        self.replacement_requested_at = 0.0
        self.replacement_deadline_at = 0.0
        self.shadow: ShadowCandidate | None = None

    @property
    def port(self) -> int:
        return self.port_base + self.index

    @property
    def tun_name(self) -> str:
        return self.device_name


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
        slot_start_timeout: int = 90,
        require_exit_ip: bool = True,
        start_openvpn: StartOpenVpnFn,
        stop_openvpn: StopOpenVpnFn,
        create_listener: CreateListenerFn,
        log: LogFn,
        write_config: WriteConfigFn | None = None,
        health_check: HealthCheckFn | None = None,
        cleanup_port: CleanupPortFn | None = None,
        health_check_interval: int = 60,
        config_dir: str | Path | None = None,
        max_shadow_starting: int = 5,
        replacement_grace_seconds: int = 180,
        shadow_port_base: int = 53000,
        shadow_port_count: int = 200,
    ) -> None:
        self.pool_size = int(pool_size)
        self.port_base = int(port_base)
        self.public_host = public_host
        self.listen_host = listen_host
        self.proxy_user = proxy_user
        self.proxy_pass = proxy_pass
        self.return_credentials = bool(return_credentials)
        self.max_starting = max(1, int(max_starting))
        self.slot_start_timeout = max(30, int(slot_start_timeout or 90))
        self.require_exit_ip = bool(require_exit_ip)
        self.start_openvpn = start_openvpn
        self.stop_openvpn = stop_openvpn
        self.create_listener = create_listener
        self.log = log
        self.write_config = write_config
        self.health_check = health_check
        self.cleanup_port = cleanup_port
        self.health_check_interval = max(5, int(health_check_interval or 60))
        self.config_dir = Path(config_dir) if config_dir else None
        self.max_shadow_starting = max(1, int(max_shadow_starting or 5))
        self.replacement_grace_seconds = max(0, int(replacement_grace_seconds or 180))
        self.shadow_port_base = int(shadow_port_base or 53000)
        self.shadow_port_count = max(1, int(shadow_port_count or 200))
        self.api_token = ""
        self.slots: list[PoolSlot] = [PoolSlot(i, self.port_base) for i in range(self.pool_size)]
        self._lock = threading.RLock()
        self._last_candidates: list[dict[str, Any]] = []
        self._skipped: dict[str, float] = {}
        self._started = False
        self._fill_thread: threading.Thread | None = None
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

    def _shadow_tun_name(self, slot: PoolSlot) -> str:
        return f"tun{self.pool_size + slot.index}"

    def _shadow_port(self, slot: PoolSlot) -> int:
        return self.shadow_port_base + (slot.index % self.shadow_port_count)

    def _shadow_inflight_count_locked(self) -> int:
        return sum(1 for s in self.slots if s.shadow is not None)

    def _cleanup_shadow_locked(self, slot: PoolSlot, keep_process: bool = False) -> None:
        shadow = slot.shadow
        if shadow is None:
            return
        if shadow.listener is not None:
            try:
                stop = getattr(shadow.listener, "stop", None)
                if callable(stop):
                    stop()
            except Exception:
                pass
            shadow.listener = None
        if not keep_process and shadow.process is not None:
            try:
                self.stop_openvpn(shadow.process)
            except Exception:
                pass
            shadow.process = None
        slot.shadow = None

    def _reset_replacement_fields_locked(self, slot: PoolSlot) -> None:
        slot.replacement_pending = False
        slot.replacement_reason = ""
        slot.replacement_requested_at = 0.0
        slot.replacement_deadline_at = 0.0
        self._cleanup_shadow_locked(slot)

    def _shadow_meta_from_node(self, shadow: ShadowCandidate, node: dict[str, Any]) -> None:
        shadow.node_id = self._node_id(node)
        shadow.node_ip = str(node.get("ip") or node.get("node_ip") or "")
        shadow.country = str(node.get("country_short") or node.get("country") or "")
        shadow.country_name = str(node.get("country") or node.get("country_name") or shadow.country)
        shadow.ip_type = str(node.get("ip_type") or "")
        shadow.entry_ip_type = shadow.ip_type
        try:
            shadow.latency_ms = int(self._latency_key(node))
            if shadow.latency_ms >= 10**9:
                shadow.latency_ms = 0
        except Exception:
            shadow.latency_ms = 0

    def _reserve_shadow_node_locked(self, slot: PoolSlot) -> dict[str, Any] | None:
        candidates = list(self._last_candidates or [])
        used_ids = {
            s.node_id
            for s in self.slots
            if s.node_id and s.state in (SLOT_READY, SLOT_STARTING)
        }
        used_ids.update(
            s.shadow.node_id
            for s in self.slots
            if s.shadow is not None and s.shadow.node_id
        )
        now = time.time()
        for node in candidates:
            nid = self._node_id(node)
            if not nid or nid == slot.node_id or nid in used_ids:
                continue
            until = self._skipped.get(nid)
            if until is not None and until > now:
                continue
            return node
        return None

    def _request_slot_replacement_locked(self, slot: PoolSlot, reason: str, now: float | None = None) -> None:
        current = time.time() if now is None else now
        if not slot.replacement_pending:
            slot.replacement_pending = True
            slot.replacement_reason = reason
            slot.replacement_requested_at = current
            slot.replacement_deadline_at = current + self.replacement_grace_seconds
        else:
            slot.replacement_reason = reason

        if self._should_drop_slot_immediately(reason) or current >= slot.replacement_deadline_at:
            old_id = slot.node_id
            self._stop_slot(slot)
            slot.last_error = reason
            if old_id:
                self._skipped[old_id] = time.time() + 60
            return

        if slot.shadow is not None:
            return
        if self._shadow_inflight_count_locked() >= self.max_shadow_starting:
            return
        node = self._reserve_shadow_node_locked(slot)
        if node is None:
            return
        shadow = ShadowCandidate(index=slot.index, tun_name=self._shadow_tun_name(slot), port=self._shadow_port(slot))
        self._shadow_meta_from_node(shadow, node)
        slot.shadow = shadow
        thread = threading.Thread(
            target=self._start_shadow_for_slot,
            args=(slot, node),
            name=f"proxy-pool-shadow-{slot.index}",
            daemon=True,
        )
        thread.start()

    def _should_drop_slot_immediately(self, reason: str) -> bool:
        text = str(reason or "").casefold()
        return (
            "urlopen error" in text
            or "错误代码 2005" in text
            or "err_ovpn_auth_failed" in text
            or "auth_failed" in text
        )

    def tick_health(self) -> None:
        """Check READY slots and request background refill when capacity is empty."""
        to_probe: list[PoolSlot] = []
        now = time.time()
        with self._lock:
            to_replace: list[tuple[PoolSlot, str]] = []
            for slot in self.slots:
                if slot.state == SLOT_STARTING:
                    if slot.starting_at > 0 and now - slot.starting_at > self.slot_start_timeout:
                        old_id = slot.node_id
                        self._stop_slot(slot)
                        slot.last_error = f"start timeout after {self.slot_start_timeout}s"
                        if old_id:
                            self._skipped[old_id] = time.time() + 60
                        try:
                            self.log("PoolSlot", f"start timeout reset slot={slot.index} node={old_id}")
                        except Exception:
                            pass
                    continue
                if slot.state != SLOT_READY:
                    continue
                unhealthy = False
                reason = "health: process/listener dead"
                if slot.process is None:
                    unhealthy = True
                else:
                    try:
                        if slot.process.poll() is not None:
                            unhealthy = True
                    except Exception:
                        unhealthy = True
                if not unhealthy:
                    if slot.listener is None:
                        unhealthy = True
                    else:
                        try:
                            is_alive = getattr(slot.listener, "is_alive", None)
                            if callable(is_alive) and not is_alive():
                                unhealthy = True
                        except Exception:
                            unhealthy = True
                if not unhealthy and self.health_check is not None and now - slot.last_health_at >= self.health_check_interval:
                    to_probe.append(slot)
                    continue
                if unhealthy:
                    slot.fail_count += 1
                    slot.last_error = reason
                    if slot.fail_count >= 2:
                        to_replace.append((slot, reason))
                else:
                    slot.fail_count = 0

            for slot, reason in to_replace:
                old_id = slot.node_id
                if slot.process is not None and slot.listener is not None:
                    self._request_slot_replacement_locked(slot, reason, now)
                    try:
                        self.log("PoolSlot", f"health replacement pending slot={slot.index} node={old_id}: {reason}")
                    except Exception:
                        pass
                else:
                    self._stop_slot(slot)
                    slot.last_error = reason
                    if old_id:
                        self._skipped[old_id] = time.time() + 60
                    try:
                        self.log("PoolSlot", f"health replace slot={slot.index} node={old_id}: {reason}")
                    except Exception:
                        pass

        for slot in to_probe:
            self._probe_ready_slot(slot)

        self._request_fill_slots()

    def list_proxies(
        self,
        country: str = "",
        limit: int = 0,
        offset: int = 0,
        sort: str = "latency",
        ip_type: str = "all",
        fallback_unknown: bool = False,
        require_exit_ip: bool | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            strict_exit = self.require_exit_ip if require_exit_ip is None else bool(require_exit_ip)
            slots = self._filtered_ready(country, ip_type, require_exit_ip=strict_exit)
            fallback_unknown_used = False
            if fallback_unknown and not slots and self._can_fallback_unknown(ip_type):
                slots = self._filtered_ready(country, ip_type, include_unknown_ip_type=True, require_exit_ip=strict_exit)
                fallback_unknown_used = bool(slots)
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
                "ip_type": ip_type or "all",
                "fallback_unknown_used": fallback_unknown_used,
                "require_exit_ip": strict_exit,
                "proxies": proxies,
            }

    def random_proxy(
        self,
        country: str = "",
        ip_type: str = "all",
        fallback_unknown: bool = False,
        require_exit_ip: bool | None = None,
    ) -> dict[str, Any] | None:
        with self._lock:
            strict_exit = self.require_exit_ip if require_exit_ip is None else bool(require_exit_ip)
            slots = self._filtered_ready(country, ip_type, require_exit_ip=strict_exit)
            fallback_unknown_used = False
            if fallback_unknown and not slots and self._can_fallback_unknown(ip_type):
                slots = self._filtered_ready(country, ip_type, include_unknown_ip_type=True, require_exit_ip=strict_exit)
                fallback_unknown_used = bool(slots)
            if not slots:
                return None
            item = self._proxy_dict(random.choice(slots))
            item["fallback_unknown_used"] = fallback_unknown_used
            item["require_exit_ip"] = strict_exit
            return item

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
                "require_exit_ip": self.require_exit_ip,
            }
            if detail:
                result["slot_detail"] = [
                    {
                        "index": s.index,
                        "port": s.port,
                        "state": s.state,
                        "node_id": s.node_id,
                        "country": s.country,
                        "ip_type": s.ip_type,
                        "entry_ip_type": s.entry_ip_type,
                        "latency_ms": s.latency_ms,
                        "health_latency_ms": s.health_latency_ms,
                        "exit_ip": s.exit_ip,
                        "last_health_at": s.last_health_at,
                        "fail_count": s.fail_count,
                        "last_error": s.last_error,
                        "replacement_pending": s.replacement_pending,
                        "replacement_reason": s.replacement_reason,
                        "replacement_deadline_at": s.replacement_deadline_at,
                        "shadow_node_id": s.shadow.node_id if s.shadow is not None else "",
                    }
                    for s in self.slots
                ]
            return result

    def _proxy_dict(self, slot: PoolSlot) -> dict[str, Any]:
        host = self.public_host
        port = slot.port
        proxy_ip = slot.exit_ip or slot.node_ip
        item: dict[str, Any] = {
            "id": slot.node_id,
            "slot": slot.index,
            "port": port,
            "host": host,
            "country": slot.country,
            "country_name": slot.country_name,
            "ip_type": slot.ip_type,
            "entry_ip_type": slot.entry_ip_type,
            "exit_ip_type": slot.ip_type if slot.exit_ip else "",
            "ip_type_source": "exit_ip" if slot.exit_ip else "entry_ip",
            "latency_ms": slot.latency_ms,
            "health_latency_ms": slot.health_latency_ms,
            "exit_ip": slot.exit_ip,
            "entry_ip": slot.node_ip,
            "proxy_ip": proxy_ip,
            "protocol": "http,socks5",
            "node_ip": proxy_ip,
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

    def _ip_type_match(self, slot_ip_type: str, requested: str) -> bool:
        reqs = [
            part.strip().casefold()
            for part in str(requested or "all").replace("|", ",").split(",")
            if part.strip()
        ]
        if not reqs or any(req in ("all", "any") for req in reqs):
            return True
        value = (slot_ip_type or "").strip().casefold()
        for req in reqs:
            if req in ("residential", "resi", "res", "\u4f4f\u5b85", "\u4f4f\u5b85ip"):
                if value in ("residential", "mobile"):
                    return True
                continue
            if req in ("hosting", "datacenter", "dc", "\u673a\u623f", "\u673a\u623fip"):
                if value == "hosting":
                    return True
                continue
            if req == "mobile":
                if value == "mobile":
                    return True
                continue
            if value == req:
                return True
        return False

    def _can_fallback_unknown(self, requested: str) -> bool:
        req = (requested or "all").strip().casefold()
        return req not in ("", "all", "any")

    def _filtered_ready(
        self,
        country: str,
        ip_type: str = "all",
        include_unknown_ip_type: bool = False,
        require_exit_ip: bool = False,
    ) -> list[PoolSlot]:
        filters = self._parse_countries(country)
        ready: list[PoolSlot] = []
        for slot in self.slots:
            if slot.state != SLOT_READY:
                continue
            if require_exit_ip and not (slot.exit_ip or "").strip():
                continue
            if not self._country_match(slot.country, filters):
                continue
            if not self._ip_type_match(slot.ip_type, ip_type):
                if include_unknown_ip_type and not (slot.ip_type or "").strip():
                    ready.append(slot)
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

            # Do not churn existing READY ports during a node-list refresh.
            # VPNGate availability lists fluctuate a lot; a node disappearing from
            # the latest CSV/test batch does not prove the already connected tunnel
            # is unusable. Keep current proxies stable and let health checks replace
            # a slot only when the actual OpenVPN/listener/exit-IP check fails.

        self._request_fill_slots()

    def _request_fill_slots(self) -> None:
        if not self._started:
            return
        with self._lock:
            if self._fill_thread is not None and self._fill_thread.is_alive():
                return
            self._fill_thread = threading.Thread(target=self._fill_worker, name="proxy-pool-fill", daemon=True)
            self._fill_thread.start()

    def _fill_worker(self) -> None:
        while True:
            tasks: list[tuple[PoolSlot, dict[str, Any]]] = []
            with self._lock:
                if not self._started:
                    return
                capacity = self.max_starting - sum(1 for s in self.slots if s.state == SLOT_STARTING)
                for _ in range(max(0, capacity)):
                    task = self._reserve_start_task_locked()
                    if task is None:
                        break
                    tasks.append(task)
            if not tasks:
                return

            threads: list[threading.Thread] = []
            for slot, node in tasks:
                t = threading.Thread(
                    target=self._start_reserved_slot,
                    args=(slot, node),
                    name=f"proxy-pool-slot-{slot.index}",
                    daemon=True,
                )
                t.start()
                threads.append(t)
            for t in threads:
                t.join(timeout=self.slot_start_timeout)

    def _reserve_start_task_locked(self) -> tuple[PoolSlot, dict[str, Any]] | None:
        candidates = list(self._last_candidates or [])
        used_ids = {
            s.node_id
            for s in self.slots
            if s.state in (SLOT_READY, SLOT_STARTING) and s.node_id
        }
        empty_slots = [s for s in self.slots if s.state == SLOT_EMPTY]
        if not empty_slots:
            return None
        now = time.time()
        for slot in empty_slots:
            if not self._prepare_empty_slot_port(slot):
                continue
            for node in candidates:
                nid = self._node_id(node)
                if not nid or nid in used_ids:
                    continue
                until = self._skipped.get(nid)
                if until is not None and until > now:
                    continue
                self._assign_slot_metadata(slot, node)
                return slot, node
        return None

    def _port_is_available(self, port: int) -> bool:
        host = self.listen_host or "0.0.0.0"
        is_ipv6 = ":" in host or host == ""
        af = socket.AF_INET6 if is_ipv6 else socket.AF_INET
        sock = None
        try:
            sock = socket.socket(af, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if is_ipv6:
                try:
                    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                except OSError:
                    pass
            sock.bind((host, int(port)))
            return True
        except OSError:
            return False
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

    def _prepare_empty_slot_port(self, slot: PoolSlot) -> bool:
        if self.cleanup_port is not None:
            try:
                self.cleanup_port(self.listen_host, slot.port)
            except Exception:
                pass
        if self._port_is_available(slot.port):
            return True
        slot.last_error = f"port {slot.port} occupied"
        try:
            self.log("PoolSlot", f"skip empty slot={slot.index} port={slot.port}: occupied")
        except Exception:
            pass
        return False

    def _assign_slot_metadata(self, slot: PoolSlot, node: dict[str, Any]) -> None:
        nid = self._node_id(node)
        slot.state = SLOT_STARTING
        slot.device_name = f"tun{slot.index}"
        slot.starting_at = time.time()
        slot.node_id = nid
        slot.node_ip = str(node.get("ip") or node.get("node_ip") or "")
        slot.country = str(node.get("country_short") or node.get("country") or "")
        slot.country_name = str(node.get("country") or node.get("country_name") or slot.country)
        slot.ip_type = str(node.get("ip_type") or "")
        slot.entry_ip_type = slot.ip_type
        try:
            slot.latency_ms = int(self._latency_key(node))
            if slot.latency_ms >= 10**9:
                slot.latency_ms = 0
        except Exception:
            slot.latency_ms = 0
        slot.last_error = ""
        slot.fail_count = 0
        slot.last_health_at = 0.0
        slot.health_latency_ms = 0
        slot.exit_ip = ""
        self._reset_replacement_fields_locked(slot)

    def _start_reserved_slot(self, slot: PoolSlot, node: dict[str, Any]) -> bool:
        nid = self._node_id(node)
        process = None
        listener = None
        path: Path | None = None
        try:
            root = self._config_root()
            safe_id = "".join(c if c.isalnum() or c in "-_." else "_" for c in nid) or f"slot{slot.index}"
            path = root / f"pool_{slot.index}_{safe_id}.ovpn"
            if self.write_config is not None:
                self.write_config(node, path)
            else:
                path.write_text(str(node.get("config_text") or ""), encoding="utf-8")

            ok, msg, process = self.start_openvpn(str(path), slot.tun_name)
            if not ok:
                if process is not None:
                    try:
                        self.stop_openvpn(process)
                    except Exception:
                        pass
                with self._lock:
                    slot.last_error = msg or "start_openvpn failed"
                    self._skipped[nid] = time.time() + 60
                    self._reset_slot_fields(slot)
                try:
                    self.log("PoolSlot", f"start failed slot={slot.index} node={nid}: {msg}")
                except Exception:
                    pass
                return False

            with self._lock:
                if slot.state != SLOT_STARTING or slot.node_id != nid:
                    try:
                        self.stop_openvpn(process)
                    except Exception:
                        pass
                    return False

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
            with self._lock:
                if slot.state != SLOT_STARTING or slot.node_id != nid:
                    try:
                        listener.stop()
                    except Exception:
                        pass
                    try:
                        self.stop_openvpn(process)
                    except Exception:
                        pass
                    return False
                slot.process = process
                slot.listener = listener
                slot.config_path = path
                slot.updated_at = time.time()
                slot.state = SLOT_READY
            try:
                self.log("PoolSlot", f"READY slot={slot.index} port={slot.port} node={nid}")
            except Exception:
                pass
            return True
        except Exception as exc:
            if listener is not None:
                try:
                    listener.stop()
                except Exception:
                    pass
            if process is not None:
                try:
                    self.stop_openvpn(process)
                except Exception:
                    pass
            with self._lock:
                if slot.node_id == nid:
                    slot.last_error = str(exc)
                    self._skipped[nid] = time.time() + 60
                    self._reset_slot_fields(slot)
            try:
                self.log("PoolSlot", f"start exception slot={slot.index} node={nid}: {exc}")
            except Exception:
                pass
            return False

    def _start_shadow_for_slot(self, slot: PoolSlot, node: dict[str, Any]) -> bool:
        shadow: ShadowCandidate | None
        with self._lock:
            shadow = slot.shadow
            if shadow is None or not slot.replacement_pending:
                return False
            if shadow.node_id != self._node_id(node):
                return False
        nid = self._node_id(node)
        process = None
        listener = None
        path: Path | None = None
        try:
            root = self._config_root()
            safe_id = "".join(c if c.isalnum() or c in "-_." else "_" for c in nid) or f"shadow{slot.index}"
            path = root / f"pool_shadow_{slot.index}_{safe_id}.ovpn"
            if self.write_config is not None:
                self.write_config(node, path)
            else:
                path.write_text(str(node.get("config_text") or ""), encoding="utf-8")

            ok, msg, process = self.start_openvpn(str(path), shadow.tun_name)
            if not ok:
                if process is not None:
                    try:
                        self.stop_openvpn(process)
                    except Exception:
                        pass
                with self._lock:
                    if slot.shadow is not None and slot.shadow.node_id == nid:
                        slot.last_error = msg or "shadow start_openvpn failed"
                        self._skipped[nid] = time.time() + 60
                        self._cleanup_shadow_locked(slot)
                return False

            listener = self.create_listener(
                host=self.listen_host,
                port=shadow.port,
                username=self.proxy_user,
                password=self.proxy_pass,
                bind_device=shadow.tun_name,
                require_auth=True,
                max_connections=None,
            )
            if hasattr(listener, "start"):
                try:
                    listener.start(background=True)
                except TypeError:
                    listener.start()

            meta: dict[str, Any] = {}
            if self.health_check is not None:
                try:
                    checked = self.health_check(shadow)
                    if isinstance(checked, tuple):
                        ok, msg, meta = checked
                    else:
                        ok, msg, meta = bool(checked), "", {}
                except Exception as exc:
                    ok, msg, meta = False, str(exc), {}
                if not ok:
                    try:
                        stop = getattr(listener, "stop", None)
                        if callable(stop):
                            stop()
                    except Exception:
                        pass
                    try:
                        self.stop_openvpn(process)
                    except Exception:
                        pass
                    with self._lock:
                        if slot.shadow is not None and slot.shadow.node_id == nid:
                            slot.last_error = msg or "shadow health_check failed"
                            self._skipped[nid] = time.time() + 60
                            self._cleanup_shadow_locked(slot)
                    return False

            with self._lock:
                current_shadow = slot.shadow
                if current_shadow is None or current_shadow.node_id != nid or not slot.replacement_pending:
                    try:
                        stop = getattr(listener, "stop", None)
                        if callable(stop):
                            stop()
                    except Exception:
                        pass
                    try:
                        self.stop_openvpn(process)
                    except Exception:
                        pass
                    return False
                current_shadow.process = process
                current_shadow.listener = listener
                current_shadow.config_path = path
                current_shadow.started_at = time.time()
                current_shadow.exit_ip = str(meta.get("exit_ip") or meta.get("ip") or "")
                current_shadow.health_latency_ms = int(meta.get("latency_ms") or 0)
                current_shadow.last_error = ""
                exit_ip_type = str(meta.get("ip_type") or "").strip()
                if exit_ip_type:
                    current_shadow.ip_type = exit_ip_type
                self._cutover_shadow_locked(slot)
            return True
        except Exception as exc:
            if listener is not None:
                try:
                    stop = getattr(listener, "stop", None)
                    if callable(stop):
                        stop()
                except Exception:
                    pass
            if process is not None:
                try:
                    self.stop_openvpn(process)
                except Exception:
                    pass
            with self._lock:
                if slot.shadow is not None and slot.shadow.node_id == nid:
                    slot.last_error = str(exc)
                    self._skipped[nid] = time.time() + 60
                    self._cleanup_shadow_locked(slot)
            return False

    def _cutover_shadow_locked(self, slot: PoolSlot) -> bool:
        shadow = slot.shadow
        if shadow is None or shadow.process is None:
            return False
        old_listener = slot.listener
        old_process = slot.process
        old_tun = slot.tun_name
        new_public_listener = None
        try:
            if old_listener is not None:
                stop = getattr(old_listener, "stop", None)
                if callable(stop):
                    stop()
            if shadow.listener is not None:
                stop_shadow = getattr(shadow.listener, "stop", None)
                if callable(stop_shadow):
                    stop_shadow()
                shadow.listener = None
            new_public_listener = self.create_listener(
                host=self.listen_host,
                port=slot.port,
                username=self.proxy_user,
                password=self.proxy_pass,
                bind_device=shadow.tun_name,
                require_auth=True,
                max_connections=None,
            )
            if hasattr(new_public_listener, "start"):
                try:
                    new_public_listener.start(background=True)
                except TypeError:
                    new_public_listener.start()
        except Exception:
            if new_public_listener is not None:
                try:
                    stop = getattr(new_public_listener, "stop", None)
                    if callable(stop):
                        stop()
                except Exception:
                    pass
            slot.listener = None
            try:
                slot.listener = self.create_listener(
                    host=self.listen_host,
                    port=slot.port,
                    username=self.proxy_user,
                    password=self.proxy_pass,
                    bind_device=old_tun,
                    require_auth=True,
                    max_connections=None,
                )
                if hasattr(slot.listener, "start"):
                    try:
                        slot.listener.start(background=True)
                    except TypeError:
                        slot.listener.start()
            except Exception:
                slot.listener = None
            return False

        slot.device_name = shadow.tun_name
        slot.node_id = shadow.node_id
        slot.node_ip = shadow.node_ip
        slot.country = shadow.country
        slot.country_name = shadow.country_name
        slot.ip_type = shadow.ip_type
        slot.entry_ip_type = shadow.entry_ip_type
        slot.latency_ms = shadow.latency_ms
        slot.exit_ip = shadow.exit_ip
        slot.health_latency_ms = shadow.health_latency_ms
        slot.process = shadow.process
        slot.listener = new_public_listener
        slot.config_path = shadow.config_path
        slot.updated_at = time.time()
        slot.fail_count = 0
        slot.last_error = ""
        if old_process is not None:
            try:
                self.stop_openvpn(old_process)
            except Exception:
                pass
        self._reset_replacement_fields_locked(slot)
        return True

    def _probe_ready_slot(self, slot: PoolSlot) -> None:
        if self.health_check is None:
            return
        try:
            checked = self.health_check(slot)
            if isinstance(checked, tuple):
                ok, message, meta = checked
            else:
                ok, message, meta = bool(checked), "", {}
        except Exception as exc:
            ok, message, meta = False, str(exc), {}
        with self._lock:
            if slot.state != SLOT_READY:
                return
            slot.last_health_at = time.time()
            if ok:
                slot.fail_count = 0
                slot.last_error = ""
                if isinstance(meta, dict):
                    slot.health_latency_ms = int(meta.get("latency_ms") or slot.health_latency_ms or 0)
                    slot.exit_ip = str(meta.get("exit_ip") or meta.get("ip") or slot.exit_ip or "")
                    exit_ip_type = str(meta.get("ip_type") or "").strip()
                    if exit_ip_type:
                        slot.ip_type = exit_ip_type
                return
            slot.fail_count += 1
            slot.last_error = message or "health_check failed"
            if slot.fail_count >= 2:
                reason = slot.last_error
                self._request_slot_replacement_locked(slot, reason, time.time())
                try:
                    self.log("PoolSlot", f"health replacement pending slot={slot.index} node={slot.node_id}: {reason}")
                except Exception:
                    pass
    def _stop_slot(self, slot: PoolSlot) -> None:
        slot.state = SLOT_DRAINING
        self._cleanup_shadow_locked(slot)
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
        slot.device_name = f"tun{slot.index}"
        slot.node_id = ""
        slot.node_ip = ""
        slot.country = ""
        slot.country_name = ""
        slot.ip_type = ""
        slot.entry_ip_type = ""
        slot.latency_ms = 0
        slot.updated_at = 0
        slot.starting_at = 0
        slot.last_health_at = 0
        slot.health_latency_ms = 0
        slot.exit_ip = ""
        slot.process = None
        slot.listener = None
        slot.config_path = None
        slot.replacement_pending = False
        slot.replacement_reason = ""
        slot.replacement_requested_at = 0.0
        slot.replacement_deadline_at = 0.0
        slot.shadow = None
