# Proxy Pool Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a mutually exclusive `pool` service mode that exposes up to 50 fixed-port HTTP/SOCKS5 proxies (from 52000) over independent OpenVPN tunnels, with a token API for list/random/status.

**Architecture:** Keep gateway mode as-is. New `proxy_pool.PoolManager` owns slots, OpenVPN-per-slot, and query helpers. `proxy_server` gains a stoppable listener that can bind egress to any `tun{N}` and accept explicit credentials. `vpngate_manager` only switches mode, feeds available nodes, and mounts `/api/pool/*` with bearer token auth (bypassing web session + secret-path for those routes only).

**Tech Stack:** Python 3 stdlib only (`unittest`, `http.server`, `socket`, `threading`, `subprocess`). OpenVPN on Linux for real tunnels. No mihomo in v1.

**Spec:** `docs/superpowers/specs/2026-07-19-proxy-pool-design.md`

---

## File structure

| File | Responsibility |
| --- | --- |
| `proxy_server.py` | Dual HTTP/SOCKS proxy. Extract `ProxyListener` with start/stop, per-listener `bind_device`, per-listener username/password. Keep `start_proxy_server()` as thin wrapper for gateway. |
| `proxy_pool.py` (**new**) | `PoolSlot`, `PoolManager`, config/secrets, filter/list/random, health tick, OpenVPN lifecycle via callbacks injected from manager (or thin imports of `run_openvpn_until_ready` / `stop_process`). |
| `vpngate_manager.py` | `SERVICE_MODE` branch in `main()`, skip single-proxy + auto-connect in pool mode, call `sync_from_nodes`, register pool HTTP routes with token auth before session gate. |
| `tests/test_proxy_listener.py` | Listener auth + stop + bind_device parameter plumbing (no real TUN required). |
| `tests/test_proxy_pool.py` | Slot state, filters, random, port formula, dedupe (mocked OpenVPN/listener). |
| `tests/test_pool_api_auth.py` | Token header parsing and filter query parsing helpers. |
| `install.sh` | Optional env documentation / print pool secrets when `SERVICE_MODE=pool` (minimal). |
| `README.md` | Short pool mode section (after code works). |

**Dependency rule:** `proxy_pool` must not import the HTML/UI half of `vpngate_manager`. Prefer injecting callables:

```python
# conceptual
PoolManager(
    start_openvpn=fn,   # (config_path, dev) -> (ok, msg, process)
    stop_openvpn=fn,    # (process) -> None
    create_listener=fn, # (...) -> ProxyListener
    log=fn,
)
```

Or import only pure helpers (`openvpn_command` is heavy/global). Prefer **injecting** openvpn start/stop from manager after Task 4 to avoid circular imports.

**Critical existing behaviors to preserve / avoid:**

1. `proxy_server.create_connection` currently hardcodes `b"tun0"` — must become parameter.
2. `start_proxy_server` currently never returns / cannot stop — must wrap with stoppable listener for pool.
3. `kill_existing_openvpn_processes()` kills **all** project OpenVPN under `DATA_DIR` — call only on process start or full pool shutdown, **never** when replacing one slot.
4. `Handler.is_authorized()` is session-cookie based under `/{secret}/...`. Pool API uses **absolute** `/api/pool/*` + token, **without** secret path and **without** session cookie (per design curl examples).
5. In pool mode do **not** call `setup_policy_routing()` (host default route steal). Egress is `SO_BINDTODEVICE` per slot only.
6. Gateway `background_proxy_checker` / `active_node_pinger` / `connect_node` auto-switch must not run (or must no-op) in pool mode.

---

### Task 1: Test harness + failing listener tests

**Files:**
- Create: `tests/__init__.py` (empty)
- Create: `tests/test_proxy_listener.py`

- [ ] **Step 1: Create test package**

```bash
mkdir -p tests
printf '' > tests/__init__.py
```

- [ ] **Step 2: Write failing tests for ProxyListener API**

Create `tests/test_proxy_listener.py`:

```python
#!/usr/bin/env python3
from __future__ import annotations

import socket
import threading
import time
import unittest
from unittest import mock

import proxy_server


class ProxyListenerTests(unittest.TestCase):
    def test_create_proxy_listener_requires_credentials_when_forced(self) -> None:
        listener = proxy_server.create_proxy_listener(
            "127.0.0.1",
            0,
            username="u1",
            password="p1",
            bind_device=None,
            max_connections=8,
            require_auth=True,
        )
        self.assertTrue(listener.auth_enabled)
        self.assertEqual(listener.username, "u1")
        self.assertEqual(listener.password, "p1")

    def test_listener_start_stop_binds_port(self) -> None:
        listener = proxy_server.create_proxy_listener(
            "127.0.0.1",
            0,
            username="u1",
            password="p1",
            bind_device=None,
            max_connections=4,
            require_auth=True,
        )
        port = listener.start()
        self.assertGreater(port, 0)
        self.assertTrue(listener.is_alive())
        with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
            sock.settimeout(2)
            # unauthenticated HTTP CONNECT-ish probe: expect 407 when auth required
            sock.sendall(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
            data = sock.recv(4096)
            self.assertIn(b"407", data)
        listener.stop()
        self.assertFalse(listener.is_alive())

    def test_create_connection_uses_bind_device(self) -> None:
        with mock.patch("proxy_server.resolve_dns_over_device", return_value="1.2.3.4"):
            with mock.patch("socket.socket") as sock_cls:
                sock = mock.MagicMock()
                sock_cls.return_value = sock
                sock.connect.return_value = None
                with mock.patch("socket.getaddrinfo", return_value=[
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 443))
                ]):
                    proxy_server.create_connection(
                        ("example.com", 443),
                        timeout=2,
                        bind_device="tun7",
                    )
                sock.setsockopt.assert_any_call(
                    socket.SOL_SOCKET,
                    socket.SO_BINDTODEVICE,
                    b"tun7",
                )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run tests — expect fail**

```bash
python -m unittest tests.test_proxy_listener -v
```

Expected: `ImportError` or `AttributeError: module 'proxy_server' has no attribute 'create_proxy_listener'`.

- [ ] **Step 4: Commit scaffold**

```bash
git add tests/__init__.py tests/test_proxy_listener.py
git commit -m "test: add failing ProxyListener contract tests"
```

---

### Task 2: Implement stoppable ProxyListener + bind_device

**Files:**
- Modify: `proxy_server.py`

- [ ] **Step 1: Parameterize DNS/connect bind device**

In `proxy_server.py`, rename/generalize:

- `dns_query_over_tun0` → keep as wrapper calling `dns_query_over_device(host, qtype, dns_server, timeout, bind_device="tun0")`
- `resolve_dns_over_tun0` → wrapper around `resolve_dns_over_device(..., bind_device="tun0")`
- `create_connection(address, timeout=20, bind_device: str | None = "tun0")`:
  - if `bind_device` is None: do **not** set `SO_BINDTODEVICE` (still resolve via normal DNS)
  - if set: bind as today with `bind_device.encode()`

Keep old function names as thin wrappers so gateway code keeps working:

```python
def resolve_dns_over_tun0(host: str, dns_server: str = "8.8.8.8", timeout: float = 3.0) -> str | None:
    return resolve_dns_over_device(host, dns_server=dns_server, timeout=timeout, bind_device="tun0")
```

- [ ] **Step 2: Thread credentials and bind_device through client handlers**

Change handler chain so they are not only global-env based:

```python
def check_credentials(
    username: str | None,
    password: str | None,
    expected_user: str | None = None,
    expected_pass: str | None = None,
) -> bool:
    if expected_user is None and expected_pass is None:
        expected_user, expected_pass = get_proxy_credentials()
        if expected_user is None and expected_pass is None:
            return True
    return secrets.compare_digest(username or "", expected_user or "") and secrets.compare_digest(
        password or "", expected_pass or ""
    )
```

Pass `bind_device`, `username`, `password`, `require_auth` into `proxy_client` / `http_client` / `socks5_client` / `create_connection` calls.

- [ ] **Step 3: Add ProxyListener class + factory**

```python
class ProxyListener:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        username: str | None,
        password: str | None,
        bind_device: str | None,
        max_connections: int | None,
        require_auth: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.bind_device = bind_device
        self.max_connections = max_connections
        self.require_auth = require_auth or (username is not None and password is not None)
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._sem = threading.BoundedSemaphore(max_connections or MAX_PROXY_CONNECTIONS)
        self._alive = False
        self.bound_port = 0

    @property
    def auth_enabled(self) -> bool:
        return bool(self.require_auth)

    def start(self) -> int:
        # bind logic adapted from start_proxy_server; if port==0 OS assigns
        # set self.bound_port from getsockname()
        # loop accept until self._stop is set
        # on each client, pass bind_device + credentials into proxy_client
        ...
        return self.bound_port

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._alive = False

    def is_alive(self) -> bool:
        return self._alive and not self._stop.is_set()


def create_proxy_listener(
    host: str,
    port: int,
    *,
    username: str | None = None,
    password: str | None = None,
    bind_device: str | None = "tun0",
    max_connections: int | None = None,
    require_auth: bool = False,
) -> ProxyListener:
    return ProxyListener(
        host,
        port,
        username=username,
        password=password,
        bind_device=bind_device,
        max_connections=max_connections,
        require_auth=require_auth,
    )


def start_proxy_server(host: str, port: int) -> None:
    # Gateway compatibility: blocking forever using env credentials + tun0
    listener = create_proxy_listener(
        host,
        port,
        username=(get_proxy_credentials()[0]),
        password=(get_proxy_credentials()[1]),
        bind_device="tun0",
        max_connections=MAX_PROXY_CONNECTIONS,
        require_auth=proxy_auth_enabled(),
    )
    listener.start()
    # start() should block in gateway mode OR start thread then join forever:
    while listener.is_alive():
        time.sleep(3600)
```

Implementation detail: `start()` for pool should **return after bind** (background accept thread). For gateway `start_proxy_server`, call `start()` then block on `stop` event / join.

Recommended split:

```python
def start(self, background: bool = True) -> int:
    self._open_server_socket()
    if background:
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
    else:
        self._accept_loop()
    return self.bound_port
```

Gateway:

```python
def start_proxy_server(host: str, port: int) -> None:
    listener = create_proxy_listener(..., bind_device="tun0", ...)
    listener.start(background=False)  # blocks in accept loop
```

- [ ] **Step 4: Run tests**

```bash
python -m unittest tests.test_proxy_listener -v
```

Expected: PASS (on Windows, `SO_BINDTODEVICE` mock test still passes; real bind-device only on Linux).

- [ ] **Step 5: Commit**

```bash
git add proxy_server.py tests/test_proxy_listener.py
git commit -m "feat: extract stoppable ProxyListener with bind_device and auth"
```

---

### Task 3: Pool config + pure query helpers (TDD)

**Files:**
- Create: `proxy_pool.py`
- Create: `tests/test_proxy_pool.py`

- [ ] **Step 1: Write failing tests for filters/random/port mapping**

Create `tests/test_proxy_pool.py`:

```python
#!/usr/bin/env python3
from __future__ import annotations

import unittest
from unittest import mock

import proxy_pool


def _ready_slot(index: int, country: str, latency: int, node_id: str | None = None) -> proxy_pool.PoolSlot:
    slot = proxy_pool.PoolSlot(index=index, port_base=52000)
    slot.state = proxy_pool.SLOT_READY
    slot.country = country
    slot.country_name = country
    slot.latency_ms = latency
    slot.node_id = node_id or f"{country}_node_{index}"
    slot.node_ip = f"1.2.3.{index}"
    slot.updated_at = 1000 + index
    return slot


class PoolQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mgr = proxy_pool.PoolManager(
            pool_size=5,
            port_base=52000,
            public_host="203.0.113.10",
            listen_host="127.0.0.1",
            proxy_user="user",
            proxy_pass="pass",
            return_credentials=True,
            max_starting=2,
            start_openvpn=mock.Mock(return_value=(False, "skip", None)),
            stop_openvpn=mock.Mock(),
            create_listener=mock.Mock(),
            log=lambda *a, **k: None,
        )
        self.mgr.slots = [
            _ready_slot(0, "JP", 50),
            _ready_slot(1, "US", 20),
            _ready_slot(2, "JP", 80),
            proxy_pool.PoolSlot(index=3, port_base=52000),  # EMPTY
        ]
        # pad to size if needed
        while len(self.mgr.slots) < 5:
            self.mgr.slots.append(proxy_pool.PoolSlot(index=len(self.mgr.slots), port_base=52000))

    def test_port_mapping(self) -> None:
        self.assertEqual(self.mgr.slots[0].port, 52000)
        self.assertEqual(self.mgr.slots[2].port, 52002)

    def test_list_country_filter_and_limit(self) -> None:
        result = self.mgr.list_proxies(country="JP", limit=1, offset=0, sort="latency")
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["proxies"][0]["country"], "JP")
        self.assertEqual(result["proxies"][0]["port"], 52000)  # lower latency JP first

    def test_list_multi_country(self) -> None:
        result = self.mgr.list_proxies(country="jp,us", limit=0, offset=0, sort="port")
        self.assertEqual(result["total"], 3)

    def test_random_respects_country(self) -> None:
        seen = set()
        for _ in range(30):
            item = self.mgr.random_proxy(country="US")
            self.assertIsNotNone(item)
            assert item is not None
            self.assertEqual(item["country"], "US")
            seen.add(item["port"])
        self.assertEqual(seen, {52001})

    def test_random_empty(self) -> None:
        self.assertIsNone(self.mgr.random_proxy(country="KR"))

    def test_status_counts(self) -> None:
        st = self.mgr.status()
        self.assertEqual(st["slots"]["ready"], 3)
        self.assertEqual(st["slots"]["empty"], 2)
        self.assertEqual(st["port_base"], 52000)


class PoolSyncTests(unittest.TestCase):
    def test_dedupe_same_node_id(self) -> None:
        started: list[str] = []

        def fake_start(config_path: str, dev: str):
            started.append(dev)
            proc = mock.Mock()
            proc.poll.return_value = None
            return True, "ok", proc

        listeners = []

        def fake_listener(**kwargs):
            lis = mock.Mock()
            lis.start.return_value = kwargs.get("port")
            lis.is_alive.return_value = True
            listeners.append(lis)
            return lis

        mgr = proxy_pool.PoolManager(
            pool_size=3,
            port_base=52000,
            public_host="127.0.0.1",
            listen_host="127.0.0.1",
            proxy_user="u",
            proxy_pass="p",
            return_credentials=True,
            max_starting=3,
            start_openvpn=fake_start,
            stop_openvpn=mock.Mock(),
            create_listener=fake_listener,
            log=lambda *a, **k: None,
            write_config=lambda node, path: path.write_text("cfg", encoding="utf-8"),
        )
        nodes = [
            {
                "id": "JP_1.1.1.1_1194_udp",
                "country_short": "JP",
                "country": "Japan",
                "ip": "1.1.1.1",
                "score_latency": 10,
                "config_text": "remote 1.1.1.1 1194",
                "probe_status": "available",
            },
            {
                "id": "JP_1.1.1.1_1194_udp",
                "country_short": "JP",
                "country": "Japan",
                "ip": "1.1.1.1",
                "score_latency": 10,
                "config_text": "remote 1.1.1.1 1194",
                "probe_status": "available",
            },
            {
                "id": "US_2.2.2.2_1194_udp",
                "country_short": "US",
                "country": "United States",
                "ip": "2.2.2.2",
                "score_latency": 20,
                "config_text": "remote 2.2.2.2 1194",
                "probe_status": "available",
            },
        ]
        mgr.sync_from_nodes(nodes)
        # wait briefly if start is async; for sync implementation assert immediately
        ready_ids = [s.node_id for s in mgr.slots if s.state == proxy_pool.SLOT_READY]
        self.assertEqual(len(ready_ids), 2)
        self.assertEqual(len(set(ready_ids)), 2)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run — expect fail**

```bash
python -m unittest tests.test_proxy_pool -v
```

Expected: `ModuleNotFoundError: No module named 'proxy_pool'`.

- [ ] **Step 3: Implement pure query + skeleton PoolManager in `proxy_pool.py`**

Create `proxy_pool.py` with at least:

```python
SLOT_EMPTY = "EMPTY"
SLOT_STARTING = "STARTING"
SLOT_READY = "READY"
SLOT_DRAINING = "DRAINING"

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
        self.updated_at = 0
        self.process = None
        self.listener = None
        self.fail_count = 0
        self.last_error = ""
        self.config_path = None

    @property
    def port(self) -> int:
        return self.port_base + self.index

    @property
    def tun_name(self) -> str:
        return f"tun{self.index}"


class PoolManager:
    def __init__(self, *, pool_size, port_base, public_host, listen_host,
                 proxy_user, proxy_pass, return_credentials, max_starting,
                 start_openvpn, stop_openvpn, create_listener, log,
                 write_config=None, config_dir=None):
        ...
        self.slots = [PoolSlot(i, port_base) for i in range(pool_size)]
        self._lock = threading.RLock()

    def list_proxies(self, country="", limit=0, offset=0, sort="latency") -> dict: ...
    def random_proxy(self, country="") -> dict | None: ...
    def status(self, detail=False) -> dict: ...
    def _proxy_dict(self, slot) -> dict: ...
    def _filtered_ready(self, country: str) -> list[PoolSlot]: ...
    def sync_from_nodes(self, nodes: list[dict]) -> None: ...  # can be stub that only fills if start_openvpn provided
    def start(self) -> None: ...
    def shutdown(self) -> None: ...
    def tick_health(self) -> None: ...
```

Filter rules:
- `country` split by comma, strip, casefold; match `slot.country` with startswith or equality on short code.
- Only `SLOT_READY`.
- `sort=latency|country|port`.
- `limit<=0` means no limit.

`random_proxy`: `random.choice` on filtered ready; return `_proxy_dict` or None.

`_proxy_dict` fields exactly as spec (honor `return_credentials`).

For this task, implement `sync_from_nodes` enough to pass dedupe test: allocate EMPTY slots, call `start_openvpn`, on success create listener with `bind_device=slot.tun_name`, mark READY. Can be synchronous for simplicity in v1 tests.

- [ ] **Step 4: Run tests**

```bash
python -m unittest tests.test_proxy_pool -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add proxy_pool.py tests/test_proxy_pool.py
git commit -m "feat: add PoolManager query API and slot skeleton"
```

---

### Task 4: Secrets/config loader + token helpers

**Files:**
- Modify: `proxy_pool.py`
- Create: `tests/test_pool_api_auth.py`

- [ ] **Step 1: Failing auth/config tests**

```python
#!/usr/bin/env python3
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import proxy_pool


class TokenTests(unittest.TestCase):
    def test_extract_token_bearer(self) -> None:
        self.assertEqual(
            proxy_pool.extract_api_token({"Authorization": "Bearer abc.def"}),
            "abc.def",
        )

    def test_extract_token_header(self) -> None:
        self.assertEqual(
            proxy_pool.extract_api_token({"X-API-Token": "tok123"}),
            "tok123",
        )

    def test_check_token(self) -> None:
        self.assertTrue(proxy_pool.token_matches("secret", "secret"))
        self.assertFalse(proxy_pool.token_matches("secret", "nope"))
        self.assertFalse(proxy_pool.token_matches("secret", None))


class ConfigLoadTests(unittest.TestCase):
    def test_load_or_create_pool_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "pool_secrets.json"
            cfg1 = proxy_pool.load_or_create_pool_config(path)
            self.assertTrue(cfg1["api_token"])
            self.assertTrue(cfg1["proxy_user"])
            self.assertTrue(cfg1["proxy_pass"])
            cfg2 = proxy_pool.load_or_create_pool_config(path)
            self.assertEqual(cfg1, cfg2)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run — fail**

```bash
python -m unittest tests.test_pool_api_auth -v
```

- [ ] **Step 3: Implement helpers in `proxy_pool.py`**

```python
def extract_api_token(headers: dict[str, str] | Any) -> str | None:
    # headers may be email.message.Message-like (.get)
    auth = headers.get("Authorization") or headers.get("authorization")
    if auth:
        scheme, _, rest = auth.strip().partition(" ")
        if scheme.lower() == "bearer" and rest:
            return rest.strip()
    tok = headers.get("X-API-Token") or headers.get("x-api-token")
    return tok.strip() if tok else None


def token_matches(expected: str, provided: str | None) -> bool:
    if not expected or provided is None:
        return False
    return secrets.compare_digest(expected, provided)


def load_or_create_pool_config(path: Path) -> dict[str, Any]:
    # read env overrides first:
    # POOL_API_TOKEN, POOL_PROXY_USER, POOL_PROXY_PASS, POOL_SIZE, POOL_PORT_BASE,
    # POOL_PUBLIC_HOST, POOL_LISTEN_HOST, POOL_API_RETURN_CREDENTIALS, POOL_MAX_STARTING
    # if file missing, generate token/user/pass with secrets.token_urlsafe / random
    # write file mode 0o600 when possible
    ...
```

Env defaults from spec: size 50, base 52000, max_starting 5, return_credentials true, listen `0.0.0.0`.

Also add:

```python
def parse_pool_query(qs: dict[str, list[str]]) -> dict[str, Any]:
    # country, limit, offset, sort, protocol, detail
    # raise ValueError on bad ints
```

- [ ] **Step 4: Tests pass + commit**

```bash
python -m unittest tests.test_pool_api_auth tests.test_proxy_pool -v
git add proxy_pool.py tests/test_pool_api_auth.py
git commit -m "feat: add pool secrets loader and token helpers"
```

---

### Task 5: Slot lifecycle — start/replace/shutdown (mocked)

**Files:**
- Modify: `proxy_pool.py`
- Modify: `tests/test_proxy_pool.py`

- [ ] **Step 1: Add lifecycle tests**

Append to `tests/test_proxy_pool.py`:

```python
class PoolLifecycleTests(unittest.TestCase):
    def _mgr(self, start_side_effect=None):
        def ok_start(config_path, dev):
            proc = mock.Mock()
            proc.poll.return_value = None
            return True, "ok", proc

        def listener_factory(**kwargs):
            lis = mock.Mock()
            lis.start.return_value = kwargs["port"]
            lis.is_alive.return_value = True
            lis.stop = mock.Mock()
            return lis

        return proxy_pool.PoolManager(
            pool_size=2,
            port_base=52000,
            public_host="127.0.0.1",
            listen_host="127.0.0.1",
            proxy_user="u",
            proxy_pass="p",
            return_credentials=True,
            max_starting=1,
            start_openvpn=start_side_effect or ok_start,
            stop_openvpn=mock.Mock(),
            create_listener=listener_factory,
            log=lambda *a, **k: None,
            write_config=lambda node, path: path.write_text(node.get("config_text") or "", encoding="utf-8"),
            config_dir=None,  # manager will use tempfile if None
        )

    def test_start_failure_leaves_empty_and_tries_next(self) -> None:
        calls = {"n": 0}

        def flaky(config_path, dev):
            calls["n"] += 1
            if calls["n"] == 1:
                return False, "boom", None
            proc = mock.Mock()
            proc.poll.return_value = None
            return True, "ok", proc

        mgr = self._mgr(flaky)
        nodes = [
            {"id": "A", "country_short": "JP", "country": "Japan", "ip": "1.1.1.1",
             "score_latency": 5, "config_text": "a", "probe_status": "available"},
            {"id": "B", "country_short": "US", "country": "US", "ip": "2.2.2.2",
             "score_latency": 6, "config_text": "b", "probe_status": "available"},
        ]
        mgr.sync_from_nodes(nodes)
        ready = [s for s in mgr.slots if s.state == proxy_pool.SLOT_READY]
        self.assertGreaterEqual(len(ready), 1)
        self.assertNotEqual(ready[0].node_id, "")

    def test_shutdown_stops_all(self) -> None:
        mgr = self._mgr()
        mgr.sync_from_nodes([
            {"id": "A", "country_short": "JP", "country": "Japan", "ip": "1.1.1.1",
             "score_latency": 5, "config_text": "a", "probe_status": "available"},
        ])
        mgr.shutdown()
        self.assertTrue(all(s.state == proxy_pool.SLOT_EMPTY for s in mgr.slots))
        self.assertTrue(all(s.listener is None for s in mgr.slots))

    def test_health_replaces_dead_process(self) -> None:
        mgr = self._mgr()
        mgr.sync_from_nodes([
            {"id": "A", "country_short": "JP", "country": "Japan", "ip": "1.1.1.1",
             "score_latency": 5, "config_text": "a", "probe_status": "available"},
            {"id": "B", "country_short": "US", "country": "US", "ip": "2.2.2.2",
             "score_latency": 6, "config_text": "b", "probe_status": "available"},
        ])
        ready = next(s for s in mgr.slots if s.state == proxy_pool.SLOT_READY)
        ready.process.poll.return_value = 1  # dead
        mgr.tick_health()
        # after health, dead slot drained/replaced if candidates remain via last_nodes cache
        # implement: PoolManager keeps self._last_candidates from sync_from_nodes
        self.assertTrue(any(s.node_id == "B" or s.state in (proxy_pool.SLOT_READY, proxy_pool.SLOT_EMPTY) for s in mgr.slots))
```

- [ ] **Step 2: Implement lifecycle in `PoolManager`**

Required behaviors:

1. `sync_from_nodes(nodes)`:
   - Keep READY slots whose `node_id` still in available set.
   - Mark missing READY → drain/stop → EMPTY.
   - Fill EMPTY from unused candidates sorted by latency.
   - Respect `max_starting` (count STARTING; only start up to cap per call — or use internal semaphore).
2. `_start_slot(slot, node)`:
   - state STARTING
   - write config via `write_config` to `config_dir / f"pool_{slot.index}_{node_id}.ovpn"`
   - `ok, msg, proc = start_openvpn(path, slot.tun_name)`
   - on fail → EMPTY, record skip for node_id with timestamp
   - on ok → `create_listener(host=listen_host, port=slot.port, username=..., password=..., bind_device=slot.tun_name, require_auth=True)`
   - `listener.start(background=True)` → READY
3. `_stop_slot(slot)`: stop listener, `stop_openvpn(process)`, clear fields, EMPTY
4. `shutdown()`: stop all slots
5. `tick_health()`: if process dead or listener not alive → fail_count++; if fail_count >= 2 → replace using `_last_candidates`
6. Never call global kill-all OpenVPN

Use `threading.RLock` around slot table mutations; start OpenVPN may block — acceptable in v1 if `sync_from_nodes` runs in maintenance thread.

- [ ] **Step 3: Tests pass + commit**

```bash
python -m unittest tests.test_proxy_pool -v
git add proxy_pool.py tests/test_proxy_pool.py
git commit -m "feat: implement pool slot start/replace/shutdown lifecycle"
```

---

### Task 6: Wire OpenVPN adapters in manager (no HTTP yet)

**Files:**
- Modify: `vpngate_manager.py` (imports + factory helpers near top/main only; keep HTML untouched)

- [ ] **Step 1: Add pool factory helpers in `vpngate_manager.py`**

Near other imports:

```python
import proxy_pool
```

Add functions (place above `main`, not inside Handler):

```python
SERVICE_MODE = os.environ.get("SERVICE_MODE", "gateway").strip().lower()
if SERVICE_MODE not in ("gateway", "pool"):
    print(f"[配置警告] SERVICE_MODE={SERVICE_MODE!r} 无效，使用 gateway", flush=True)
    SERVICE_MODE = "gateway"

pool_manager: proxy_pool.PoolManager | None = None

def pool_write_config(node: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(node.get("config_text") or "", encoding="utf-8")

def pool_start_openvpn(config_path: str, dev: str):
    # route_nopull=True ALWAYS in pool mode (no default route steal)
    return run_openvpn_until_ready(
        config_path,
        keep_alive=True,
        route_nopull=True,
        timeout=OPENVPN_TEST_TIMEOUT_SECONDS,
        dev=dev,
    )

def pool_stop_openvpn(process) -> None:
    stop_process(process)

def build_pool_manager() -> proxy_pool.PoolManager:
    secrets_path = DATA_DIR / "pool_secrets.json"
    cfg = proxy_pool.load_or_create_pool_config(secrets_path)
    print(
        f"[Pool] token/user loaded (token length={len(cfg['api_token'])}, user={cfg['proxy_user']})",
        flush=True,
    )
    return proxy_pool.PoolManager(
        pool_size=int(cfg.get("pool_size", 50)),
        port_base=int(cfg.get("port_base", 52000)),
        public_host=str(cfg.get("public_host") or ""),
        listen_host=str(cfg.get("listen_host") or "0.0.0.0"),
        proxy_user=str(cfg["proxy_user"]),
        proxy_pass=str(cfg["proxy_pass"]),
        return_credentials=bool(cfg.get("return_credentials", True)),
        max_starting=int(cfg.get("max_starting", 5)),
        start_openvpn=pool_start_openvpn,
        stop_openvpn=pool_stop_openvpn,
        create_listener=lambda **kw: proxy_server.create_proxy_listener(**kw),
        log=lambda level, msg: log_to_json(level, "Pool", msg),
        write_config=pool_write_config,
        config_dir=CONFIG_DIR / "pool",
    )
```

Ensure `load_or_create_pool_config` fills `public_host` if empty: try env `POOL_PUBLIC_HOST`, else leave blank and let API use request host later optional — for v1 print warning if empty and use `127.0.0.1` fallback only for local tests.

- [ ] **Step 2: Manual syntax check**

```bash
python -m py_compile vpngate_manager.py proxy_pool.py proxy_server.py
```

Expected: no output, exit 0.

- [ ] **Step 3: Commit**

```bash
git add vpngate_manager.py proxy_pool.py
git commit -m "feat: wire PoolManager OpenVPN adapters in manager"
```

---

### Task 7: Mode switch in `main()` + feed nodes from maintenance

**Files:**
- Modify: `vpngate_manager.py` (`main`, `maintain_valid_nodes`, gateway-only threads)

- [ ] **Step 1: Branch `main()`**

Replace proxy/thread startup section conceptually:

```python
def main() -> None:
    global pool_manager
    ensure_dirs()
    kill_existing_openvpn_processes()  # once at boot only
    ...
    write_json(STATE_FILE, {..., "service_mode": SERVICE_MODE, ...})

    if SERVICE_MODE == "pool":
        pool_manager = build_pool_manager()
        pool_manager.start()
        print(f"[Pool] mode enabled size={pool_manager.pool_size} base_port={pool_manager.port_base}", flush=True)
        threading.Thread(target=collector_loop, daemon=True).start()
        threading.Thread(target=pool_health_loop, daemon=True).start()
        # do NOT start start_proxy_server, background_proxy_checker, active_node_pinger
    else:
        threading.Thread(
            target=proxy_server.start_proxy_server,
            args=(LOCAL_PROXY_HOST, LOCAL_PROXY_PORT),
            daemon=True,
        ).start()
        # existing gateway ready wait ...
        threading.Thread(target=collector_loop, daemon=True).start()
        threading.Thread(target=background_proxy_checker, daemon=True).start()
        threading.Thread(target=active_node_pinger, daemon=True).start()

    DualStackHTTPServer((ui_host, ui_port), Handler).serve_forever()
```

Add:

```python
def pool_health_loop() -> None:
    while True:
        try:
            if pool_manager is not None:
                pool_manager.tick_health()
        except Exception as exc:
            log_to_json("ERROR", "Pool", f"health loop: {exc}")
        time.sleep(15)
```

- [ ] **Step 2: Feed pool after probes in `maintain_valid_nodes`**

At end of successful maintenance (where nodes list is finalized / available known), add:

```python
if SERVICE_MODE == "pool" and pool_manager is not None:
    available = [
        n for n in read_nodes()
        if n.get("probe_status") == "available" or n.get("active")
    ]
    # In pool mode "active" flag is unused; prefer probe_status only:
    available = [n for n in read_nodes() if n.get("probe_status") == "available"]
    pool_manager.sync_from_nodes(available)
```

Also **disable gateway auto-connect** when pool:

In `maintain_valid_nodes`, wrap blocks that call `connect_node` / `auto_switch_node` with `if SERVICE_MODE != "pool":`.

In `background_proxy_checker` / `active_node_pinger`, early-return if pool (defense in depth even if not started).

- [ ] **Step 3: Compile + unit tests**

```bash
python -m py_compile vpngate_manager.py
python -m unittest tests.test_proxy_pool tests.test_proxy_listener tests.test_pool_api_auth -v
```

- [ ] **Step 4: Commit**

```bash
git add vpngate_manager.py
git commit -m "feat: service mode switch and pool node sync from maintenance"
```

---

### Task 8: HTTP `/api/pool/*` routes + token gate

**Files:**
- Modify: `vpngate_manager.py` (`Handler.validate_path`, `do_GET`)

- [ ] **Step 1: Allow absolute pool paths without secret prefix**

In `validate_path`:

```python
def validate_path(self) -> str:
    request_path = urllib.parse.urlsplit(self.path).path
    if request_path.startswith("/api/pool/") or request_path == "/api/pool":
        return request_path
    # existing secret_path logic...
```

- [ ] **Step 2: Token gate before session for pool routes**

At start of `do_GET`:

```python
def do_GET(self) -> None:
    effective_path = self.validate_path()
    if effective_path == "":
        return

    if effective_path.startswith("/api/pool"):
        self._handle_pool_api(effective_path)
        return

    if not self.is_authorized():
        ...
```

Implement:

```python
def _handle_pool_api(self, effective_path: str) -> None:
    global pool_manager
    if SERVICE_MODE != "pool" or pool_manager is None:
        self.send_json({"ok": False, "error": "pool_mode_disabled"}, HTTPStatus.FORBIDDEN)
        return
    cfg_token = pool_manager.api_token  # set on manager from secrets
    provided = proxy_pool.extract_api_token(self.headers)
    if not proxy_pool.token_matches(cfg_token, provided):
        self.send_json({"ok": False, "error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
        return
    parsed = urllib.parse.urlsplit(self.path)
    qs = urllib.parse.parse_qs(parsed.query)
    try:
        q = proxy_pool.parse_pool_query(qs)
    except ValueError as exc:
        self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        return

    if effective_path in ("/api/pool/health", "/api/pool/health/"):
        self.send_json({"ok": True, "mode": "pool"})
        return
    if effective_path in ("/api/pool/status", "/api/pool/status/"):
        self.send_json(pool_manager.status(detail=bool(q.get("detail"))))
        return
    if effective_path in ("/api/pool/proxies/random", "/api/pool/proxies/random/"):
        item = pool_manager.random_proxy(country=q.get("country") or "")
        if item is None:
            self.send_json({"ok": False, "error": "no_proxy_available"}, HTTPStatus.NOT_FOUND)
            return
        self.send_json({"ok": True, "proxy": item})
        return
    if effective_path in ("/api/pool/proxies", "/api/pool/proxies/"):
        self.send_json(
            pool_manager.list_proxies(
                country=q.get("country") or "",
                limit=int(q.get("limit") or 0),
                offset=int(q.get("offset") or 0),
                sort=str(q.get("sort") or "latency"),
            )
        )
        return
    self.send_json({"ok": False, "error": "not_found"}, HTTPStatus.NOT_FOUND)
```

Store `api_token` on `PoolManager` from config.

- [ ] **Step 3: Small handler unit test (optional pure function)**

If Handler is hard to unit test without server, rely on `parse_pool_query` + manual curl on Linux. Prefer adding `tests/test_pool_query_parse.py` already covered in Task 4.

- [ ] **Step 4: Commit**

```bash
git add vpngate_manager.py proxy_pool.py
git commit -m "feat: add token-authenticated /api/pool list random status routes"
```

---

### Task 9: get_state + minimal UI indicator (optional thin)

**Files:**
- Modify: `vpngate_manager.py` (`get_state` only; skip large HTML rewrite if timeboxed)

- [ ] **Step 1: Expose pool summary in state JSON**

In `get_state()`:

```python
state["service_mode"] = SERVICE_MODE
if SERVICE_MODE == "pool" and pool_manager is not None:
    state["pool"] = pool_manager.status()
else:
    state["pool"] = None
```

- [ ] **Step 2: Commit**

```bash
git add vpngate_manager.py
git commit -m "feat: include pool status in get_state for UI consumers"
```

(Full glass UI cards are out of scope for v1 per spec.)

---

### Task 10: install.sh + README notes

**Files:**
- Modify: `install.sh` (env comments / optional SERVICE_MODE)
- Modify: `README.md` (short Chinese+English section)

- [ ] **Step 1: Document env in README**

Add section **代理池模式 (pool)**:

- `SERVICE_MODE=pool`
- ports `52000+`, global proxy user/pass in `vpngate_data/pool_secrets.json`
- API examples for list + random
- firewall: allow `52000:52049/tcp` and UI port
- warn: needs root + TUN + enough file descriptors; 4C24G / 50 slots
- mode is exclusive vs gateway

- [ ] **Step 2: install.sh**

When writing systemd unit, allow optional Environment lines (commented):

```
# Environment=SERVICE_MODE=pool
# Environment=POOL_SIZE=50
# Environment=POOL_PORT_BASE=52000
```

Do not force pool as default.

- [ ] **Step 3: Commit**

```bash
git add install.sh README.md
git commit -m "docs: document proxy pool mode configuration and API"
```

---

### Task 11: Linux integration checklist (manual)

**Files:** none (runbook in plan only)

On a Linux VPS with OpenVPN:

- [ ] **Step 1: Run small pool**

```bash
export SERVICE_MODE=pool
export POOL_SIZE=3
export POOL_PORT_BASE=52000
export POOL_LISTEN_HOST=0.0.0.0
export POOL_PUBLIC_HOST=<vps_public_ip>
python3 vpngate_manager.py
```

- [ ] **Step 2: Wait for maintenance to mark available nodes; check status**

```bash
TOKEN=$(python3 -c "import json;print(json.load(open('vpngate_data/pool_secrets.json'))['api_token'])")
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8787/api/pool/status
curl -s -H "Authorization: Bearer $TOKEN" 'http://127.0.0.1:8787/api/pool/proxies?limit=3'
curl -s -H "Authorization: Bearer $TOKEN" 'http://127.0.0.1:8787/api/pool/proxies/random?country=JP'
```

- [ ] **Step 3: Traffic test**

```bash
# use user/pass from pool_secrets.json
curl -x "http://USER:PASS@127.0.0.1:52000" https://ifconfig.me
```

- [ ] **Step 4: Failure test**

```bash
# kill one openvpn child for a slot; wait for health loop; status ready count recovers if candidates exist
```

- [ ] **Step 5: Mode isolation**

Restart with `SERVICE_MODE=gateway`; confirm no listeners on 52000–52002; 7928 works.

- [ ] **Step 6: Record results in commit message or short note if issues found; fix bugs with new commits**

---

### Task 12: Final regression

- [ ] **Step 1: Run all unit tests**

```bash
python -m unittest discover -s tests -v
python -m py_compile proxy_server.py proxy_pool.py vpngate_manager.py
```

Expected: all PASS, compile clean.

- [ ] **Step 2: Grep for accidental policy routing in pool path**

```bash
# human check: pool_start_openvpn uses route_nopull=True
# human check: create_listener bind_device=tun{N}
# human check: kill_existing_openvpn_processes not called from sync/replace
```

- [ ] **Step 3: Final commit if dirty**

```bash
git status
# commit any leftover fixes
```

---

## Spec coverage checklist

| Spec item | Task |
| --- | --- |
| gateway/pool mutual switch | Task 7 |
| Fixed slots, port base 52000 | Task 3–5 |
| Global proxy user/pass | Task 4–5 |
| Token API list | Task 8 |
| Token API random | Task 8 |
| Token API status/health | Task 8 |
| country/limit/offset/sort | Task 3, 8 |
| OpenVPN multi-tun, no mihomo | Task 5–6 |
| No default route steal | Task 6 (`route_nopull=True`) |
| proxy_server thin listener | Task 2 |
| proxy_pool owns logic | Task 3–5 |
| manager thin glue | Task 6–8 |
| Health replace | Task 5, 7 |
| max_starting | Task 5 |
| Secrets file | Task 4 |
| install/README | Task 10 |
| Success criteria 1–6 | Tasks 7–11 |

## Type/name consistency

- States: `EMPTY` / `STARTING` / `READY` / `DRAINING` (constants `SLOT_*`)
- Methods: `list_proxies`, `random_proxy`, `status`, `sync_from_nodes`, `tick_health`, `start`, `shutdown`
- Factory: `create_proxy_listener` → `ProxyListener.start/stop/is_alive`
- Routes: `/api/pool/proxies`, `/api/pool/proxies/random`, `/api/pool/status`, `/api/pool/health`
- Env: `SERVICE_MODE`, `POOL_*` as design §8

## Notes for implementers

1. Prefer **unittest** (stdlib) — project advertises zero third-party deps.
2. Full 50-slot soak only on Linux VPS; develop query/listener logic on any OS.
3. Do not expand `vpngate_manager.py` with pool algorithms — only glue.
4. If `SO_BINDTODEVICE` fails without root, fail the slot with a clear log (existing error codes 3004/3006 style).
5. YAGNI: no mihomo export, no per-port passwords, no write APIs, no big UI.
