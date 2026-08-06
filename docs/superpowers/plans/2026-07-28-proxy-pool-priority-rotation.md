# Proxy Pool Priority Rotation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make routine pool refresh replace 10% of formal slots per round using tested-good nodes only, with non-hosting candidates preferred over hosting candidates, and with deterministic slot rotation from `0` upward that resets after restart.

**Architecture:** Keep the existing shadow warmup and cutover path in `proxy_pool.py`, but change two selectors: candidate selection becomes preferred-tiered (`residential`/`mobile` before `hosting`) and slot selection becomes cursor-driven instead of `updated_at`-driven. Keep manager-side fetch/test flow intact; only remove the manager override that forces a fixed batch size so the pool manager can apply the new built-in 10% policy.

**Tech Stack:** Python 3.12, `unittest`, mocked OpenVPN/listener lifecycle in `proxy_pool.py`, existing fetch/test pipeline in `vpngate_manager.py`

## Global Constraints

- Use only nodes from the current tested-good refresh round for routine replacement.
- Prefer non-hosting exits (`residential` and `mobile`) before `hosting` exits.
- Replace exactly `max(1, floor(pool_size * 0.10))` formal slots per routine round when enough eligible candidates exist.
- Replace slots by sequential public slot order from `0` upward and wrap at the end of the pool.
- Reset routine replacement cursor to `0` on service restart; do not persist it.
- Keep the existing shadow startup + health validation + cutover safety rule; do not stop a healthy READY slot first.
- Do not add a TTL, max-slot-age policy, random slot selection, or routine full-pool rebuild.

---

## File Map

- `F:\officeProject\yigehui\aimili-vpngate\proxy_pool.py`
  - owns pool slot state, candidate dedupe, routine rolling replacement, shadow warmup, and cutover.
  - will gain the in-memory routine refresh cursor and preferred-candidate ordering helpers.
- `F:\officeProject\yigehui\aimili-vpngate\tests\test_proxy_pool.py`
  - already covers pool query logic, dedupe, and rolling shadow replacement.
  - will gain routine-rotation regression coverage.
- `F:\officeProject\yigehui\aimili-vpngate\vpngate_manager.py`
  - already fetches/tests nodes and passes tested-good nodes into pool sync/replacement.
  - will stop overriding batch size so the pool manager's 10% policy is authoritative.
- `F:\officeProject\yigehui\aimili-vpngate\tests\test_vpngate_manager_fetch.py`
  - already verifies manager calls `sync_from_nodes()` and `rolling_replace_from_nodes()` with tested-good nodes only.
  - will be updated to assert the manager no longer forces a custom batch-size override.
- `F:\officeProject\yigehui\aimili-vpngate\.env.example`
  - currently documents `POOL_REFRESH_BATCH_SIZE=5`.
  - should be cleaned so config docs do not advertise the removed routine-refresh knob.

### Task 1: Lock the new pool behavior with failing tests

**Files:**
- Modify: `F:\officeProject\yigehui\aimili-vpngate\tests\test_proxy_pool.py`
- Test: `F:\officeProject\yigehui\aimili-vpngate\tests\test_proxy_pool.py`

**Interfaces:**
- Consumes: existing `proxy_pool.PoolManager`, existing `_mgr(...)` helper in `PoolReplacementTests`, existing `rolling_replace_from_nodes(nodes: list[dict[str, Any]], batch_size: int | None = None) -> int`
- Produces: regression tests expecting `PoolManager.refresh_cursor: int`, preferred candidate ordering for `residential`/`mobile` before `hosting`, and sequential slot targeting across repeated routine rounds

- [ ] **Step 1: Write the failing tests for preferred-IP ordering and cursor rotation**

```python
    def test_rolling_replace_prefers_non_hosting_candidates_before_hosting(self) -> None:
        mgr = self._mgr(pool_size=4, max_starting=4, max_shadow_starting=4)
        mgr.health_check = mock.Mock(return_value=(True, "ok", {"exit_ip": "9.9.9.9", "latency_ms": 12}))
        mgr.start()
        mgr.sync_from_nodes([
            {"id": "A", "country_short": "JP", "country": "Japan", "ip": "1.1.1.1", "ip_type": "hosting", "score_latency": 5, "config_text": "a", "probe_status": "available"},
            {"id": "B", "country_short": "US", "country": "US", "ip": "2.2.2.2", "ip_type": "hosting", "score_latency": 6, "config_text": "b", "probe_status": "available"},
            {"id": "C", "country_short": "KR", "country": "Korea", "ip": "3.3.3.3", "ip_type": "hosting", "score_latency": 7, "config_text": "c", "probe_status": "available"},
            {"id": "D", "country_short": "SG", "country": "Singapore", "ip": "4.4.4.4", "ip_type": "hosting", "score_latency": 8, "config_text": "d", "probe_status": "available"},
        ])
        _wait_ready(mgr, 4)

        started = mgr.rolling_replace_from_nodes([
            {"id": "E", "country_short": "TH", "country": "Thailand", "ip": "5.5.5.5", "ip_type": "hosting", "score_latency": 1, "config_text": "e", "probe_status": "available"},
            {"id": "F", "country_short": "GB", "country": "United Kingdom", "ip": "6.6.6.6", "ip_type": "residential", "score_latency": 50, "config_text": "f", "probe_status": "available"},
            {"id": "G", "country_short": "NL", "country": "Netherlands", "ip": "7.7.7.7", "ip_type": "mobile", "score_latency": 60, "config_text": "g", "probe_status": "available"},
            {"id": "H", "country_short": "DE", "country": "Germany", "ip": "8.8.8.8", "ip_type": "hosting", "score_latency": 2, "config_text": "h", "probe_status": "available"},
        ], batch_size=2)

        deadline = time.time() + 2
        while time.time() < deadline:
            ids = [s.node_id for s in mgr.slots if s.state == proxy_pool.SLOT_READY]
            if len(set(ids) & {"F", "G"}) == 2:
                break
            time.sleep(0.01)

        ids = [s.node_id for s in mgr.slots if s.state == proxy_pool.SLOT_READY]
        self.assertEqual(started, 2)
        self.assertEqual(len(set(ids) & {"F", "G"}), 2)
        self.assertEqual(mgr.refresh_cursor, 2)
        mgr.shutdown()

    def test_rolling_replace_advances_slots_by_cursor_and_wraps(self) -> None:
        mgr = self._mgr(pool_size=4, max_starting=4, max_shadow_starting=4)
        mgr.health_check = mock.Mock(return_value=(True, "ok", {"exit_ip": "9.9.9.9", "latency_ms": 12}))
        mgr.start()
        mgr.sync_from_nodes([
            {"id": "A", "country_short": "JP", "country": "Japan", "ip": "1.1.1.1", "ip_type": "hosting", "score_latency": 5, "config_text": "a", "probe_status": "available"},
            {"id": "B", "country_short": "US", "country": "US", "ip": "2.2.2.2", "ip_type": "hosting", "score_latency": 6, "config_text": "b", "probe_status": "available"},
            {"id": "C", "country_short": "KR", "country": "Korea", "ip": "3.3.3.3", "ip_type": "hosting", "score_latency": 7, "config_text": "c", "probe_status": "available"},
            {"id": "D", "country_short": "SG", "country": "Singapore", "ip": "4.4.4.4", "ip_type": "hosting", "score_latency": 8, "config_text": "d", "probe_status": "available"},
        ])
        _wait_ready(mgr, 4)

        first = mgr.rolling_replace_from_nodes([
            {"id": "E", "country_short": "TH", "country": "Thailand", "ip": "5.5.5.5", "ip_type": "residential", "score_latency": 1, "config_text": "e", "probe_status": "available"},
            {"id": "F", "country_short": "GB", "country": "United Kingdom", "ip": "6.6.6.6", "ip_type": "residential", "score_latency": 2, "config_text": "f", "probe_status": "available"},
        ], batch_size=2)
        _wait_ready(mgr, 4)
        first_ids = [mgr.slots[i].node_id for i in range(4)]
        self.assertEqual(first, 2)
        self.assertEqual(first_ids[:2], ["E", "F"])
        self.assertEqual(first_ids[2:], ["C", "D"])
        self.assertEqual(mgr.refresh_cursor, 2)

        second = mgr.rolling_replace_from_nodes([
            {"id": "G", "country_short": "NL", "country": "Netherlands", "ip": "7.7.7.7", "ip_type": "residential", "score_latency": 3, "config_text": "g", "probe_status": "available"},
            {"id": "H", "country_short": "DE", "country": "Germany", "ip": "8.8.8.8", "ip_type": "residential", "score_latency": 4, "config_text": "h", "probe_status": "available"},
        ], batch_size=2)
        _wait_ready(mgr, 4)
        second_ids = [mgr.slots[i].node_id for i in range(4)]
        self.assertEqual(second, 2)
        self.assertEqual(second_ids, ["E", "F", "G", "H"])
        self.assertEqual(mgr.refresh_cursor, 0)
        mgr.shutdown()

    def test_new_manager_starts_refresh_cursor_from_zero(self) -> None:
        mgr = self._mgr(pool_size=4, max_starting=4, max_shadow_starting=4)
        self.assertEqual(mgr.refresh_cursor, 0)
```

- [ ] **Step 2: Run the new pool tests to verify they fail first**

Run:

```powershell
python -m unittest tests.test_proxy_pool.PoolReplacementTests.test_rolling_replace_prefers_non_hosting_candidates_before_hosting tests.test_proxy_pool.PoolReplacementTests.test_rolling_replace_advances_slots_by_cursor_and_wraps tests.test_proxy_pool.PoolReplacementTests.test_new_manager_starts_refresh_cursor_from_zero
```

Expected: FAIL because `PoolManager` does not expose `refresh_cursor`, candidate ordering still follows latency only, and slot replacement still follows `updated_at` order.

- [ ] **Step 3: Add one manager-side failing test for removal of forced batch override**

```python
    def test_test_multiple_nodes_uses_pool_default_rotation_batch(self) -> None:
        nodes_file = Path(tempfile.mkdtemp()) / "nodes.json"
        config_dir = Path(tempfile.mkdtemp()) / "cfg"
        config_dir.mkdir(parents=True, exist_ok=True)
        nodes_file.write_text("[]", encoding="utf-8")
        pool_manager = mock.Mock()

        with (
            mock.patch.object(vpngate_manager, "NODES_FILE", nodes_file),
            mock.patch.object(vpngate_manager, "CONFIG_DIR", config_dir),
            mock.patch.object(vpngate_manager, "SERVICE_MODE", "pool"),
            mock.patch.object(vpngate_manager, "pool_manager", pool_manager),
            mock.patch.object(vpngate_manager.vpn_utils, "ping_latency_ms", return_value=11),
            mock.patch.object(vpngate_manager.vpn_utils, "enrich_ip_info"),
            mock.patch.object(vpngate_manager, "get_free_test_index", return_value=7),
            mock.patch.object(vpngate_manager, "release_test_index"),
            mock.patch.object(vpngate_manager, "run_openvpn_until_ready", side_effect=[(True, "ok", None), (False, "fail", None)]),
            mock.patch.object(vpngate_manager.concurrent.futures, "ThreadPoolExecutor", ImmediateExecutor),
            mock.patch.object(vpngate_manager.concurrent.futures, "as_completed", side_effect=lambda futures: list(futures)),
        ):
            vpngate_manager.test_multiple_nodes(["node-1", "node-2"])

        pool_manager.rolling_replace_from_nodes.assert_called_once_with([
            mock.ANY,
        ])
```

- [ ] **Step 4: Run the manager test to verify it fails first**

Run:

```powershell
python -m unittest tests.test_vpngate_manager_fetch.VpnGateNodeTestIntegrationTests.test_test_multiple_nodes_uses_pool_default_rotation_batch
```

Expected: FAIL because the current manager still calls `rolling_replace_from_nodes(..., batch_size=POOL_REFRESH_BATCH_SIZE)`.

- [ ] **Step 5: Commit the red tests plan checkpoint**

```powershell
git add F:\officeProject\yigehui\aimili-vpngate\tests\test_proxy_pool.py F:\officeProject\yigehui\aimili-vpngate\tests\test_vpngate_manager_fetch.py
# Commit only after the implementation in later tasks makes the suite green.
```

### Task 2: Implement cursor-driven, preferred-tier routine replacement in `proxy_pool.py`

**Files:**
- Modify: `F:\officeProject\yigehui\aimili-vpngate\proxy_pool.py`
- Test: `F:\officeProject\yigehui\aimili-vpngate\tests\test_proxy_pool.py`

**Interfaces:**
- Consumes: existing `_dedupe_nodes(nodes) -> list[dict[str, Any]]`, existing `_node_id(node) -> str`, existing `_shadow_inflight_count_locked() -> int`, existing `_start_shadow_for_slot(slot, node) -> bool`
- Produces:
  - `PoolManager.refresh_cursor: int`
  - `PoolManager._routine_replace_count(self, batch_size: int | None = None) -> int`
  - `PoolManager._candidate_priority_key(self, node: dict[str, Any]) -> tuple[int, float, str]`
  - `PoolManager._select_refresh_candidates_locked(self, candidates: list[dict[str, Any]], consumed_ids: set[str]) -> list[dict[str, Any]]`
  - updated `PoolManager.rolling_replace_from_nodes(self, nodes: list[dict[str, Any]], batch_size: int | None = None) -> int`

- [ ] **Step 1: Add the failing cursor field and replacement-count helper expectation in code comments / skeleton**

```python
        self.refresh_cursor = 0

    def _routine_replace_count(self, batch_size: int | None = None) -> int:
        if batch_size is not None:
            return max(0, int(batch_size or 0))
        return max(1, self.pool_size // 10)
```

- [ ] **Step 2: Implement preferred candidate ranking with non-hosting first**

```python
    def _candidate_priority_key(self, node: dict[str, Any]) -> tuple[int, float, str]:
        ip_type = str(node.get("ip_type") or "").strip().lower()
        tier = 0 if ip_type in ("residential", "mobile") else 1
        return (tier, self._latency_key(node), self._node_id(node))

    def _select_refresh_candidates_locked(
        self,
        candidates: list[dict[str, Any]],
        consumed_ids: set[str],
    ) -> list[dict[str, Any]]:
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
        selected: list[dict[str, Any]] = []
        for node in sorted(candidates, key=self._candidate_priority_key):
            nid = self._node_id(node)
            if not nid or nid in used_ids or nid in consumed_ids:
                continue
            until = self._skipped.get(nid)
            if until is not None and until > now:
                continue
            consumed_ids.add(nid)
            selected.append(node)
        return selected
```

- [ ] **Step 3: Implement sequential slot selection with cursor advancement and wrap**

```python
    def rolling_replace_from_nodes(self, nodes: list[dict[str, Any]], batch_size: int | None = None) -> int:
        candidates = self._dedupe_nodes(list(nodes or []))
        now = time.time()
        tasks: list[tuple[PoolSlot, dict[str, Any]]] = []
        with self._lock:
            self._last_candidates = list(candidates)
            capacity = min(
                self._routine_replace_count(batch_size),
                self.max_shadow_starting - self._shadow_inflight_count_locked(),
            )
            if capacity <= 0 or not self.slots:
                return 0
            consumed_ids: set[str] = set()
            ordered_candidates = self._select_refresh_candidates_locked(candidates, consumed_ids)
            if not ordered_candidates:
                return 0
            candidate_index = 0
            scanned = 0
            while len(tasks) < capacity and candidate_index < len(ordered_candidates) and scanned < self.pool_size:
                slot = self.slots[self.refresh_cursor % self.pool_size]
                self.refresh_cursor = (self.refresh_cursor + 1) % self.pool_size
                scanned += 1
                if not (
                    slot.state == SLOT_READY
                    and not slot.replacement_pending
                    and slot.shadow is None
                    and slot.process is not None
                    and slot.listener is not None
                    and slot.node_id
                ):
                    continue
                node = ordered_candidates[candidate_index]
                candidate_index += 1
                shadow = ShadowCandidate(index=slot.index, tun_name=self._shadow_tun_name(slot), port=self._shadow_port(slot))
                self._shadow_meta_from_node(shadow, node)
                slot.replacement_pending = True
                slot.replacement_reason = "rolling refresh"
                slot.replacement_requested_at = now
                slot.replacement_deadline_at = now + self.replacement_grace_seconds
                slot.shadow = shadow
                tasks.append((slot, node))
        for slot, node in tasks:
            threading.Thread(
                target=self._start_shadow_for_slot,
                args=(slot, node),
                name=f"proxy-pool-rolling-{slot.index}",
                daemon=True,
            ).start()
        return len(tasks)
```

- [ ] **Step 4: Run the pool replacement tests and make the implementation green**

Run:

```powershell
python -m unittest tests.test_proxy_pool.PoolReplacementTests.test_rolling_replace_prefers_non_hosting_candidates_before_hosting tests.test_proxy_pool.PoolReplacementTests.test_rolling_replace_advances_slots_by_cursor_and_wraps tests.test_proxy_pool.PoolReplacementTests.test_new_manager_starts_refresh_cursor_from_zero -v
```

Expected: PASS.

- [ ] **Step 5: Run the broader pool test module to catch regressions**

Run:

```powershell
python -m unittest tests.test_proxy_pool -v
```

Expected: PASS for the full `tests.test_proxy_pool` module.

- [ ] **Step 6: Commit the pool-manager implementation**

```powershell
git add F:\officeProject\yigehui\aimili-vpngate\proxy_pool.py F:\officeProject\yigehui\aimili-vpngate\tests\test_proxy_pool.py
git commit -m "feat: rotate pool refresh slots sequentially"
```

### Task 3: Remove manager-side batch override and align docs/tests

**Files:**
- Modify: `F:\officeProject\yigehui\aimili-vpngate\vpngate_manager.py`
- Modify: `F:\officeProject\yigehui\aimili-vpngate\tests\test_vpngate_manager_fetch.py`
- Modify: `F:\officeProject\yigehui\aimili-vpngate\.env.example`
- Test: `F:\officeProject\yigehui\aimili-vpngate\tests\test_vpngate_manager_fetch.py`

**Interfaces:**
- Consumes: `pool_manager.rolling_replace_from_nodes(available_snapshot, batch_size: int | None = None) -> int`
- Produces: manager call path that uses `pool_manager.rolling_replace_from_nodes(available_snapshot)` with no override, and env docs that no longer advertise `POOL_REFRESH_BATCH_SIZE`

- [ ] **Step 1: Update the manager test to assert no explicit batch-size override**

```python
        pool_manager.sync_from_nodes.assert_called()
        pool_manager.replace_all_slots_from_nodes.assert_not_called()
        pool_manager.rolling_replace_from_nodes.assert_called_once()
        self.assertEqual(pool_manager.rolling_replace_from_nodes.call_args.args[0][0]["id"], "node-1")
        self.assertEqual(pool_manager.rolling_replace_from_nodes.call_args.kwargs, {})
```

- [ ] **Step 2: Remove the explicit batch-size override from `vpngate_manager.py`**

```python
    if available_snapshot is not None:
        try:
            pool_manager.sync_from_nodes(available_snapshot)
            pool_manager.rolling_replace_from_nodes(available_snapshot)
        except Exception as pool_exc:
            print(f"[test_multiple_nodes] pool rolling refresh failed: {pool_exc}", flush=True)
```

- [ ] **Step 3: Remove the stale routine-refresh env knob from `.env.example`**

```dotenv
# delete this stale line because routine replacement is now fixed at 10% of pool size
# POOL_REFRESH_BATCH_SIZE=5
```

- [ ] **Step 4: Run the focused manager tests**

Run:

```powershell
python -m unittest tests.test_vpngate_manager_fetch.VpnGateNodeTestIntegrationTests.test_test_multiple_nodes_updates_pool_without_replacing_ready_slots tests.test_vpngate_manager_fetch.VpnGateNodeTestIntegrationTests.test_test_multiple_nodes_uses_pool_default_rotation_batch -v
```

Expected: PASS.

- [ ] **Step 5: Run the combined regression suite for this feature**

Run:

```powershell
python -m unittest tests.test_proxy_pool tests.test_vpngate_manager_fetch -v
```

Expected: PASS for the combined pool + manager regression suite.

- [ ] **Step 6: Commit the manager integration and docs cleanup**

```powershell
git add F:\officeProject\yigehui\aimili-vpngate\vpngate_manager.py F:\officeProject\yigehui\aimili-vpngate\tests\test_vpngate_manager_fetch.py F:\officeProject\yigehui\aimili-vpngate\.env.example
git commit -m "refactor: use built-in pool refresh rotation"
```

## Self-Review

- Spec coverage check:
  - tested-good-only replacement flow is covered by Task 3 manager assertions and existing manager integration path.
  - non-hosting-first candidate ordering is covered by Task 1 tests and Task 2 implementation.
  - 10% routine replacement policy is covered by Task 2 `_routine_replace_count()` and Task 1/Task 2 tests.
  - sequential slot cursor rotation and wrap are covered by Task 1 tests and Task 2 implementation.
  - restart reset to `0` is covered by the new-manager test in Task 1 and `refresh_cursor` initialization in Task 2.
  - shadow safety rule is preserved by reusing existing `rolling_replace_from_nodes()` thread launch and `_start_shadow_for_slot()` path in Task 2.
- Placeholder scan: no `TBD`, `TODO`, or unspecified “add tests later” steps remain.
- Type consistency:
  - `refresh_cursor` is an `int` everywhere.
  - `rolling_replace_from_nodes(nodes, batch_size=None) -> int` stays stable for callers/tests.
  - new helper names are defined in Task 2 before later tasks rely on them.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-28-proxy-pool-priority-rotation.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
