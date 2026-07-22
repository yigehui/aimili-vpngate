# Pool Shadow Warmup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add shadow warmup replacement so unhealthy READY pool slots keep serving until a background replacement has started, passed the current health check, and is ready for cutover.

**Architecture:** Keep the existing fixed public slot model and add replacement metadata plus a shadow candidate per slot. Health-triggered replacement no longer stops the active slot first; instead it creates a temporary shadow tunnel/listener, validates it with the existing health callback, and only then cuts over the public slot.

**Tech Stack:** Python 3.12, existing `proxy_pool.py` slot manager, existing `vpngate_manager.py` config wiring, `unittest` + `unittest.mock`

## Global Constraints

- Work directly in `F:\officeProject\yigehui\aimili-vpngate` on the current branch; do not create a worktree.
- Reuse the existing OpenVPN startup path, listener creation path, and `health_check` callback semantics.
- Keep `/api/pool/proxies` and `/api/pool/proxies/random` backward-compatible.
- Shadow candidates must never appear in public proxy list/random API output.
- Public slot identity must remain stable: same slot index, same public port, same credentials.
- Add only the minimal config needed for shadow warmup: grace window, shadow concurrency, temporary shadow port range.

---

### Task 1: Add regression tests for shadow warmup behavior

**Files:**
- Modify: `F:\officeProject\yigehui\aimili-vpngate\tests\test_proxy_pool.py`

**Interfaces:**
- Consumes: `proxy_pool.PoolManager`, `proxy_pool.PoolSlot`, `proxy_pool.SLOT_READY`, `proxy_pool.SLOT_EMPTY`
- Produces: regression coverage for `PoolManager.tick_health()`, `PoolManager.status(detail=True)`, and shadow cutover behavior

- [ ] **Step 1: Write the failing tests**

```python
    def test_health_starts_shadow_without_stopping_active_slot(self) -> None:
        mgr = self._mgr()
        mgr.health_check = mock.Mock(return_value=(False, "health_check failed", {}))
        mgr.start()
        mgr.sync_from_nodes([
            {"id": "A", "country_short": "JP", "country": "Japan", "ip": "1.1.1.1",
             "score_latency": 5, "config_text": "a", "probe_status": "available"},
            {"id": "B", "country_short": "US", "country": "US", "ip": "2.2.2.2",
             "score_latency": 6, "config_text": "b", "probe_status": "available"},
        ])
        _wait_ready(mgr, 1)
        active = next(s for s in mgr.slots if s.state == proxy_pool.SLOT_READY)
        original_node = active.node_id
        original_listener = active.listener
        original_process = active.process

        mgr.tick_health()
        mgr.tick_health()

        self.assertEqual(active.state, proxy_pool.SLOT_READY)
        self.assertEqual(active.node_id, original_node)
        self.assertIs(active.listener, original_listener)
        self.assertIs(active.process, original_process)
        self.assertTrue(active.replacement_pending)
        self.assertIsNotNone(active.shadow)

    def test_shadow_cutover_replaces_slot_after_shadow_health_passes(self) -> None:
        health_results = iter([
            (False, "health_check failed", {}),
            (False, "health_check failed", {}),
            (True, "ok", {"exit_ip": "9.9.9.9", "latency_ms": 12}),
        ])
        mgr = self._mgr()
        mgr.health_check = mock.Mock(side_effect=lambda slot: next(health_results))
        mgr.start()
        mgr.sync_from_nodes([
            {"id": "A", "country_short": "JP", "country": "Japan", "ip": "1.1.1.1",
             "score_latency": 5, "config_text": "a", "probe_status": "available"},
            {"id": "B", "country_short": "US", "country": "US", "ip": "2.2.2.2",
             "score_latency": 6, "config_text": "b", "probe_status": "available"},
        ])
        _wait_ready(mgr, 1)
        active = next(s for s in mgr.slots if s.state == proxy_pool.SLOT_READY)
        old_listener = active.listener
        old_process = active.process

        mgr.tick_health()
        mgr.tick_health()
        deadline = time.time() + 2
        while time.time() < deadline and active.node_id == "A":
            time.sleep(0.01)

        self.assertEqual(active.node_id, "B")
        self.assertNotEqual(active.listener, old_listener)
        self.assertNotEqual(active.process, old_process)
        self.assertFalse(active.replacement_pending)
        self.assertIsNone(active.shadow)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_proxy_pool.PoolLifecycleTests.test_health_starts_shadow_without_stopping_active_slot tests.test_proxy_pool.PoolLifecycleTests.test_shadow_cutover_replaces_slot_after_shadow_health_passes -v`

Expected: FAIL because `PoolSlot` does not yet have replacement/shadow state and `tick_health()` still stops the slot directly.

- [ ] **Step 3: Add one more failing edge-case test for grace expiry**

```python
    def test_grace_expiry_falls_back_to_stop_and_refill(self) -> None:
        mgr = self._mgr()
        mgr.health_check = mock.Mock(return_value=(False, "health_check failed", {}))
        mgr.replacement_grace_seconds = 0
        mgr.start()
        mgr.sync_from_nodes([
            {"id": "A", "country_short": "JP", "country": "Japan", "ip": "1.1.1.1",
             "score_latency": 5, "config_text": "a", "probe_status": "available"},
            {"id": "B", "country_short": "US", "country": "US", "ip": "2.2.2.2",
             "score_latency": 6, "config_text": "b", "probe_status": "available"},
        ])
        _wait_ready(mgr, 1)
        active = next(s for s in mgr.slots if s.state == proxy_pool.SLOT_READY)
        mgr.tick_health()
        mgr.tick_health()
        self.assertIn(active.state, (proxy_pool.SLOT_READY, proxy_pool.SLOT_EMPTY))
```

- [ ] **Step 4: Run the targeted tests again**

Run: `python -m unittest tests.test_proxy_pool.PoolLifecycleTests -v`

Expected: FAIL with missing replacement/grace behavior.

- [ ] **Step 5: Commit after the full task is green**

```bash
git add tests/test_proxy_pool.py
git commit -m "test: add shadow warmup pool lifecycle coverage"
```

### Task 2: Implement shadow candidate metadata and background warmup in `proxy_pool.py`

**Files:**
- Modify: `F:\officeProject\yigehui\aimili-vpngate\proxy_pool.py`

**Interfaces:**
- Consumes: existing `start_openvpn`, `stop_openvpn`, `create_listener`, `health_check`, `_last_candidates`, `_skipped`
- Produces:
  - `class ShadowCandidate`
  - `PoolSlot.replacement_pending: bool`
  - `PoolSlot.replacement_reason: str`
  - `PoolSlot.replacement_requested_at: float`
  - `PoolSlot.replacement_deadline_at: float`
  - `PoolSlot.shadow: ShadowCandidate | None`
  - `PoolManager._start_shadow_for_slot(slot: PoolSlot) -> None`
  - `PoolManager._cutover_shadow(slot: PoolSlot) -> bool`

- [ ] **Step 1: Add minimal replacement/shadow fields to `PoolSlot` and manager config**

```python
class ShadowCandidate:
    def __init__(self, *, tun_name: str, port: int) -> None:
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
```

- [ ] **Step 2: Add failing-safe helpers for shadow resource cleanup and metadata reset**

```python
    def _reset_replacement_fields(self, slot: PoolSlot) -> None:
        slot.replacement_pending = False
        slot.replacement_reason = ""
        slot.replacement_requested_at = 0.0
        slot.replacement_deadline_at = 0.0
        slot.shadow = None
```

- [ ] **Step 3: Change `tick_health()` so READY slots request replacement instead of immediate stop**

```python
                if unhealthy:
                    slot.fail_count += 1
                    slot.last_error = reason
                    if slot.fail_count >= 2:
                        self._request_slot_replacement_locked(slot, reason, now)
                    continue
```

- [ ] **Step 4: Implement shadow warmup using existing startup and health-check logic**

```python
    def _start_shadow_for_slot(self, slot: PoolSlot) -> None:
        shadow = slot.shadow
        if shadow is None:
            return
        ok, msg, process = self.start_openvpn(str(path), shadow.tun_name)
        ...
        listener = self.create_listener(
            host=self.listen_host,
            port=shadow.port,
            username=self.proxy_user,
            password=self.proxy_pass,
            bind_device=shadow.tun_name,
            require_auth=True,
            max_connections=None,
        )
        ...
        checked = self.health_check(_ShadowHealthSlotView(slot, shadow))
```

- [ ] **Step 5: Implement cutover**

```python
    def _cutover_shadow(self, slot: PoolSlot) -> bool:
        shadow = slot.shadow
        if shadow is None or shadow.listener is None or shadow.process is None:
            return False
        old_listener = slot.listener
        old_process = slot.process
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
        slot.listener = shadow.listener
        slot.config_path = shadow.config_path
        slot.updated_at = time.time()
        self._reset_replacement_fields(slot)
        ...
```

- [ ] **Step 6: Run targeted tests**

Run: `python -m unittest tests.test_proxy_pool.PoolLifecycleTests -v`

Expected: new shadow-warmup lifecycle tests PASS.

- [ ] **Step 7: Commit**

```bash
git add proxy_pool.py tests/test_proxy_pool.py
git commit -m "feat: add pool shadow warmup cutover"
```

### Task 3: Wire config/status output and verify full suite

**Files:**
- Modify: `F:\officeProject\yigehui\aimili-vpngate\vpngate_manager.py`
- Modify: `F:\officeProject\yigehui\aimili-vpngate\.env.example`

**Interfaces:**
- Consumes: `proxy_pool.load_or_create_pool_config`, `PoolManager.status(detail=True)`
- Produces:
  - `POOL_MAX_SHADOW_STARTING`
  - `POOL_REPLACEMENT_GRACE_SECONDS`
  - `POOL_SHADOW_PORT_BASE`
  - `POOL_SHADOW_PORT_COUNT`

- [ ] **Step 1: Add config defaults to `.env.example`**

```env
POOL_SIZE=100
POOL_MAX_STARTING=10
POOL_MAX_SHADOW_STARTING=5
POOL_REPLACEMENT_GRACE_SECONDS=180
POOL_SHADOW_PORT_BASE=53000
POOL_SHADOW_PORT_COUNT=200
```

- [ ] **Step 2: Pass new config into `build_pool_manager()` and expose detail status**

```python
        max_shadow_starting=int(cfg.get("max_shadow_starting", 5)),
        replacement_grace_seconds=int(cfg.get("replacement_grace_seconds", 180)),
        shadow_port_base=int(cfg.get("shadow_port_base", 53000)),
        shadow_port_count=int(cfg.get("shadow_port_count", 200)),
```

- [ ] **Step 3: Extend `status(detail=True)` assertions in tests if needed**

```python
        self.assertIn("replacement_pending", st["details"][0])
```

- [ ] **Step 4: Run full verification**

Run:

```bash
python -m py_compile proxy_pool.py vpngate_manager.py
python -m unittest discover -v
git diff --check
```

Expected:
- `py_compile` exits 0
- all unit tests pass
- `git diff --check` outputs nothing

- [ ] **Step 5: Commit**

```bash
git add .env.example proxy_pool.py vpngate_manager.py tests/test_proxy_pool.py
git commit -m "feat: wire shadow warmup pool config and status"
```

## Self-Review

- Spec coverage: tasks cover replacement metadata, shadow warmup, cutover, grace fallback, status/config, and regression tests.
- Placeholder scan: no `TODO`/`TBD` placeholders remain.
- Type consistency: `ShadowCandidate`, `replacement_pending`, `shadow`, `_start_shadow_for_slot`, and `_cutover_shadow` names are used consistently across tasks.
