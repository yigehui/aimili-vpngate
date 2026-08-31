#!/usr/bin/env python3
from __future__ import annotations

import time
import unittest
from unittest import mock

import proxy_pool


def _wait_ready(mgr: proxy_pool.PoolManager, min_ready: int, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if sum(1 for s in mgr.slots if s.state == proxy_pool.SLOT_READY) >= min_ready:
            return
        time.sleep(0.01)


def _ready_slot(index: int, country: str, latency: int, node_id: str | None = None) -> proxy_pool.PoolSlot:
    slot = proxy_pool.PoolSlot(index=index, port_base=52000)
    slot.state = proxy_pool.SLOT_READY
    slot.country = country
    slot.country_name = country
    slot.ip_type = "residential" if country == "JP" else "hosting"
    slot.latency_ms = latency
    slot.node_id = node_id or f"{country}_node_{index}"
    slot.node_ip = f"1.2.3.{index}"
    slot.exit_ip = f"1.2.3.{index}"
    slot.updated_at = 1000 + index
    return slot


def _seed_pool(mgr: proxy_pool.PoolManager, nodes: list[dict[str, object]]) -> int:
    return mgr.replace_all_slots_from_target_nodes(list(nodes), batch_size=mgr.pool_size)


def _set_candidates(mgr: proxy_pool.PoolManager, nodes: list[dict[str, object]]) -> None:
    candidates = mgr._dedupe_nodes(list(nodes))
    candidates.sort(key=mgr._candidate_priority_key)
    mgr._last_candidates = list(candidates)


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

    def test_proxy_dict_uses_exit_ip_as_node_ip(self) -> None:
        self.mgr.slots[0].exit_ip = "9.9.9.9"
        result = self.mgr.list_proxies(country="JP", limit=1, sort="port")
        item = result["proxies"][0]
        self.assertEqual(item["node_ip"], "9.9.9.9")
        self.assertEqual(item["proxy_ip"], "9.9.9.9")
        self.assertEqual(item["entry_ip"], "1.2.3.0")


    def test_require_exit_ip_filters_pending_health_slots(self) -> None:
        self.mgr.slots[0].exit_ip = ""
        strict = self.mgr.list_proxies(country="JP", sort="port")
        self.assertEqual(strict["require_exit_ip"], True)
        self.assertEqual(strict["total"], 1)
        self.assertEqual(strict["proxies"][0]["port"], 52002)
        loose = self.mgr.list_proxies(country="JP", sort="port", require_exit_ip=False)
        self.assertEqual(loose["require_exit_ip"], False)
        self.assertEqual(loose["total"], 2)

    def test_global_exit_ip_disabled_ignores_strict_request_param(self) -> None:
        self.mgr.require_exit_ip = False
        self.mgr.slots[0].exit_ip = ""
        result = self.mgr.list_proxies(country="JP", sort="port", require_exit_ip=True)
        self.assertEqual(result["require_exit_ip"], False)
        self.assertEqual(result["total"], 2)

    def test_list_skips_ready_slots_with_recent_health_failure(self) -> None:
        self.mgr.slots[0].fail_count = 1
        self.mgr.slots[0].last_error = "<urlopen error timed out>"

        result = self.mgr.list_proxies(country="JP", sort="port")

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["proxies"][0]["port"], 52002)

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

    def test_ip_type_filter(self) -> None:
        result = self.mgr.list_proxies(ip_type="residential")
        self.assertEqual(result["total"], 2)
        self.assertTrue(all(p["ip_type"] == "residential" for p in result["proxies"]))
        item = self.mgr.random_proxy(ip_type="hosting")
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item["country"], "US")
        mixed = self.mgr.list_proxies(ip_type="residential|hosting")
        self.assertEqual(mixed["total"], 3)
        mixed2 = self.mgr.list_proxies(ip_type="residential,mobile")
        self.assertEqual(mixed2["total"], 2)

    def test_health_check_updates_ip_type_from_exit_ip(self) -> None:
        self.mgr.health_check = mock.Mock(return_value=(
            True,
            "ok",
            {"exit_ip": "9.9.9.9", "ip_type": "hosting", "latency_ms": 12},
        ))
        self.mgr.slots[0].ip_type = "residential"
        self.mgr.slots[0].entry_ip_type = "residential"

        self.mgr._probe_ready_slot(self.mgr.slots[0])

        self.assertEqual(self.mgr.slots[0].exit_ip, "9.9.9.9")
        self.assertEqual(self.mgr.slots[0].ip_type, "hosting")
        self.assertEqual(self.mgr.slots[0].entry_ip_type, "residential")
        item = self.mgr.list_proxies(country="JP", sort="port", ip_type="hosting")["proxies"][0]
        self.assertEqual(item["port"], 52000)
        self.assertEqual(item["ip_type_source"], "exit_ip")
        self.assertEqual(item["entry_ip_type"], "residential")

    def test_ip_type_fallback_unknown(self) -> None:
        unknown = self.mgr.slots[3]
        unknown.state = proxy_pool.SLOT_READY
        unknown.country = "KR"
        unknown.country_name = "KR"
        unknown.ip_type = ""
        unknown.latency_ms = 30
        unknown.node_id = "KR_unknown"
        unknown.exit_ip = "3.3.3.3"
        strict = self.mgr.list_proxies(country="KR", ip_type="residential")
        self.assertEqual(strict["total"], 0)
        fallback = self.mgr.list_proxies(country="KR", ip_type="residential", fallback_unknown=True)
        self.assertEqual(fallback["total"], 1)
        self.assertEqual(fallback["proxies"][0]["id"], "KR_unknown")
        self.assertEqual(fallback["fallback_unknown_used"], True)
        item = self.mgr.random_proxy(country="KR", ip_type="residential", fallback_unknown=True)
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item["fallback_unknown_used"], True)

    def test_status_counts(self) -> None:
        st = self.mgr.status()
        self.assertEqual(st["slots"]["ready"], 3)
        self.assertEqual(st["slots"]["empty"], 2)
        self.assertEqual(st["port_base"], 52000)

    def test_status_reports_fixed_refresh_batch_size(self) -> None:
        mgr = proxy_pool.PoolManager(
            pool_size=1,
            port_base=52000,
            public_host="203.0.113.10",
            listen_host="127.0.0.1",
            proxy_user="user",
            proxy_pass="pass",
            return_credentials=True,
            max_starting=1,
            refresh_batch_size=5,
            start_openvpn=mock.Mock(),
            stop_openvpn=mock.Mock(),
            create_listener=mock.Mock(),
            log=lambda *a, **k: None,
        )
        self.assertEqual(mgr.status()["refresh_batch_size"], 30)

    def test_list_proxy_lines_defaults_to_http_multiline(self) -> None:
        text = self.mgr.list_proxy_lines(country="jp,us", sort="port")
        self.assertEqual(
            text.splitlines(),
            [
                "http://user:pass@203.0.113.10:52000",
                "http://user:pass@203.0.113.10:52001",
                "http://user:pass@203.0.113.10:52002",
            ],
        )

    def test_list_proxy_lines_supports_socks5(self) -> None:
        text = self.mgr.list_proxy_lines(ip_type="hosting", return_type="socks5")
        self.assertEqual(text, "socks5://user:pass@203.0.113.10:52001")


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
        mgr.start()
        _seed_pool(mgr, nodes)
        _wait_ready(mgr, 2)
        ready_ids = [s.node_id for s in mgr.slots if s.state == proxy_pool.SLOT_READY]
        self.assertEqual(len(ready_ids), 2)
        self.assertEqual(len(set(ready_ids)), 2)


class PoolLifecycleTests(unittest.TestCase):
    def _mgr(self, start_side_effect=None, **overrides):
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

        params = dict(
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
            config_dir=None,
        )
        params.update(overrides)
        return proxy_pool.PoolManager(**params)

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
        mgr.start()
        _seed_pool(mgr, nodes)
        _wait_ready(mgr, 1)
        ready = [s for s in mgr.slots if s.state == proxy_pool.SLOT_READY]
        self.assertGreaterEqual(len(ready), 1)
        self.assertNotEqual(ready[0].node_id, "")

    def test_start_failure_skips_same_failed_node_for_current_refresh_window(self) -> None:
        def flaky(config_path, dev):
            if "bad-node" in config_path:
                return False, "boom", None
            proc = mock.Mock()
            proc.poll.return_value = None
            return True, "ok", proc

        mgr = self._mgr(flaky, pool_size=1)
        bad = {
            "id": "bad-node",
            "country_short": "JP",
            "country": "Japan",
            "ip": "1.1.1.1",
            "score_latency": 1,
            "config_text": "bad",
            "probe_status": "available",
        }
        good = {
            "id": "good-node",
            "country_short": "US",
            "country": "US",
            "ip": "2.2.2.2",
            "score_latency": 2,
            "config_text": "good",
            "probe_status": "available",
        }
        mgr.start()
        _set_candidates(mgr, [bad, good])

        with mock.patch("proxy_pool.time.time", return_value=1000.0):
            task = mgr._reserve_start_task_locked()
            self.assertIsNotNone(task)
            slot, node = task
            self.assertEqual(node["id"], "bad-node")
            self.assertFalse(mgr._start_reserved_slot(slot, node))

        with mock.patch("proxy_pool.time.time", return_value=1061.0):
            retry_task = mgr._reserve_start_task_locked()

        self.assertIsNotNone(retry_task)
        assert retry_task is not None
        self.assertEqual(retry_task[1]["id"], "good-node")


    def test_refill_continues_past_max_starting_batch(self) -> None:
        mgr = self._mgr()
        mgr.start()
        _seed_pool(mgr, [
            {"id": "A", "country_short": "JP", "country": "Japan", "ip": "1.1.1.1",
             "score_latency": 5, "config_text": "a", "probe_status": "available"},
            {"id": "B", "country_short": "US", "country": "US", "ip": "2.2.2.2",
             "score_latency": 6, "config_text": "b", "probe_status": "available"},
        ])
        _wait_ready(mgr, 2)
        self.assertEqual(sum(1 for s in mgr.slots if s.state == proxy_pool.SLOT_READY), 2)

    def test_cold_start_sync_prefers_non_hosting_over_faster_hosting(self) -> None:
        mgr = self._mgr(pool_size=2, max_starting=2)
        mgr.start()
        _seed_pool(mgr, [
            {"id": "fast_hosting", "country_short": "US", "country": "US", "ip": "1.1.1.1",
             "score_latency": 5, "ip_type": "hosting", "config_text": "a", "probe_status": "available"},
            {"id": "slow_residential", "country_short": "JP", "country": "Japan", "ip": "2.2.2.2",
             "score_latency": 50, "ip_type": "residential", "config_text": "b", "probe_status": "available"},
        ])
        _wait_ready(mgr, 2)
        self.assertEqual([mgr.slots[i].node_id for i in range(2)], ["slow_residential", "fast_hosting"])
        mgr.shutdown()


    def test_refill_skips_empty_slot_when_port_is_occupied(self) -> None:
        cleanup_calls: list[int] = []
        mgr = proxy_pool.PoolManager(
            pool_size=2,
            port_base=52000,
            public_host="127.0.0.1",
            listen_host="127.0.0.1",
            proxy_user="u",
            proxy_pass="p",
            return_credentials=True,
            max_starting=2,
            start_openvpn=lambda config_path, dev: (True, "ok", mock.Mock(poll=mock.Mock(return_value=None))),
            stop_openvpn=mock.Mock(),
            create_listener=lambda **kwargs: mock.Mock(
                start=mock.Mock(return_value=kwargs["port"]),
                is_alive=mock.Mock(return_value=True),
                stop=mock.Mock(),
            ),
            log=lambda *a, **k: None,
            write_config=lambda node, path: path.write_text(node.get("config_text") or "", encoding="utf-8"),
            cleanup_port=lambda host, port: cleanup_calls.append(port) or False,
        )
        mgr._port_is_available = mock.Mock(side_effect=lambda port: int(port) != 52000)  # type: ignore[method-assign]
        mgr.start()
        _seed_pool(mgr, [
            {"id": "A", "country_short": "JP", "country": "Japan", "ip": "1.1.1.1",
             "score_latency": 5, "config_text": "a", "probe_status": "available"},
            {"id": "B", "country_short": "US", "country": "US", "ip": "2.2.2.2",
             "score_latency": 6, "config_text": "b", "probe_status": "available"},
        ])
        _wait_ready(mgr, 1)
        self.assertEqual(mgr.slots[0].state, proxy_pool.SLOT_EMPTY)
        self.assertIn("occupied", mgr.slots[0].last_error)
        self.assertEqual(mgr.slots[1].state, proxy_pool.SLOT_READY)
        self.assertIn(52000, cleanup_calls)

    def test_shutdown_stops_all(self) -> None:
        mgr = self._mgr()
        mgr.start()
        _seed_pool(mgr, [
            {"id": "A", "country_short": "JP", "country": "Japan", "ip": "1.1.1.1",
             "score_latency": 5, "config_text": "a", "probe_status": "available"},
        ])
        _wait_ready(mgr, 1)
        mgr.shutdown()
        self.assertTrue(all(s.state == proxy_pool.SLOT_EMPTY for s in mgr.slots))
        self.assertTrue(all(s.listener is None for s in mgr.slots))

    def test_health_drops_dead_process_without_refill(self) -> None:
        mgr = self._mgr()
        mgr.start()
        _seed_pool(mgr, [
            {"id": "A", "country_short": "JP", "country": "Japan", "ip": "1.1.1.1",
             "score_latency": 5, "config_text": "a", "probe_status": "available"},
            {"id": "B", "country_short": "US", "country": "US", "ip": "2.2.2.2",
             "score_latency": 6, "config_text": "b", "probe_status": "available"},
        ])
        _wait_ready(mgr, 1)
        ready = next(s for s in mgr.slots if s.state == proxy_pool.SLOT_READY)
        ready.process.poll.return_value = 1  # dead
        mgr.tick_health()
        self.assertEqual(ready.state, proxy_pool.SLOT_EMPTY)
        self.assertEqual(ready.node_id, "")

    def test_health_does_not_refill_from_latest_available_nodes(self) -> None:
        mgr = self._mgr()
        mgr.replacement_grace_seconds = 0
        mgr.start()

        mgr.slots[0] = _ready_slot(0, "JP", 10, node_id="A")
        mgr.slots[1] = _ready_slot(1, "US", 20, node_id="B")
        proc0 = mock.Mock()
        proc0.poll.return_value = None
        lis0 = mock.Mock()
        lis0.is_alive.return_value = True
        lis0.stop = mock.Mock()
        proc1 = mock.Mock()
        proc1.poll.return_value = None
        lis1 = mock.Mock()
        lis1.is_alive.return_value = True
        lis1.stop = mock.Mock()
        mgr.slots[0].process = proc0
        mgr.slots[0].listener = lis0
        mgr.slots[1].process = proc1
        mgr.slots[1].listener = lis1

        _set_candidates(mgr, [
            {"id": "B", "country_short": "US", "country": "US", "ip": "2.2.2.2",
             "score_latency": 5, "config_text": "b", "probe_status": "available"},
            {"id": "C", "country_short": "KR", "country": "Korea", "ip": "3.3.3.3",
             "score_latency": 6, "config_text": "c", "probe_status": "available"},
        ])

        mgr.slots[0].process.poll.return_value = 1
        mgr.tick_health()

        self.assertEqual(mgr.slots[0].state, proxy_pool.SLOT_EMPTY)
        self.assertEqual(mgr.slots[0].node_id, "")
        self.assertEqual(mgr.slots[1].node_id, "B")

    def test_health_failure_removes_active_slot_immediately(self) -> None:
        mgr = self._mgr(pool_size=1)
        mgr.health_check = mock.Mock(return_value=(False, "health_check failed", {}))
        mgr.start()
        _seed_pool(mgr, [
            {"id": "A", "country_short": "JP", "country": "Japan", "ip": "1.1.1.1",
             "score_latency": 5, "config_text": "a", "probe_status": "available"},
            {"id": "B", "country_short": "US", "country": "US", "ip": "2.2.2.2",
             "score_latency": 6, "config_text": "b", "probe_status": "available"},
        ])
        _wait_ready(mgr, 1)
        active = next(s for s in mgr.slots if s.state == proxy_pool.SLOT_READY)
        original_listener = active.listener
        original_process = active.process

        mgr.tick_health()

        self.assertEqual(active.state, proxy_pool.SLOT_EMPTY)
        self.assertEqual(active.node_id, "")
        original_listener.stop.assert_called_once()
        mgr.stop_openvpn.assert_any_call(original_process)
        self.assertFalse(active.replacement_pending)
        self.assertIsNone(active.shadow)
        mgr.shutdown()

    def test_removed_slot_clears_health_error_text(self) -> None:
        mgr = self._mgr(pool_size=1)
        mgr.health_check = mock.Mock(return_value=(False, "<urlopen error timed out>", {}))
        mgr.start()
        _seed_pool(mgr, [
            {"id": "A", "country_short": "JP", "country": "Japan", "ip": "1.1.1.1",
             "score_latency": 5, "config_text": "a", "probe_status": "available"},
        ])
        _wait_ready(mgr, 1)

        mgr.tick_health()

        self.assertEqual(mgr.slots[0].state, proxy_pool.SLOT_EMPTY)
        self.assertEqual(mgr.slots[0].last_error, "")
        self.assertEqual(mgr.slots[0].fail_count, 0)
        mgr.shutdown()

    def test_health_failure_drops_slot_without_shadow_or_refill(self) -> None:
        seen: dict[str, int] = {}

        def health_check(slot):
            node_id = getattr(slot, "node_id", "")
            seen[node_id] = seen.get(node_id, 0) + 1
            if node_id == "A":
                return False, "health_check failed", {}
            return True, "ok", {"exit_ip": "9.9.9.9", "latency_ms": 12}

        mgr = self._mgr(pool_size=1)
        mgr.health_check = mock.Mock(side_effect=health_check)
        mgr.start()
        _seed_pool(mgr, [
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

        self.assertEqual(active.state, proxy_pool.SLOT_EMPTY)
        self.assertEqual(active.node_id, "")
        old_listener.stop.assert_called_once()
        mgr.stop_openvpn.assert_any_call(old_process)
        self.assertFalse(active.replacement_pending)
        self.assertIsNone(active.shadow)
        mgr.shutdown()


    def test_health_failure_does_not_start_grace_refill(self) -> None:
        mgr = self._mgr(pool_size=1)
        mgr.health_check = mock.Mock(return_value=(False, "health_check failed", {}))
        mgr.replacement_grace_seconds = 0
        mgr.start()
        _seed_pool(mgr, [
            {"id": "A", "country_short": "JP", "country": "Japan", "ip": "1.1.1.1",
             "score_latency": 5, "config_text": "a", "probe_status": "available"},
            {"id": "B", "country_short": "US", "country": "US", "ip": "2.2.2.2",
             "score_latency": 6, "config_text": "b", "probe_status": "available"},
        ])
        _wait_ready(mgr, 1)
        active = next(s for s in mgr.slots if s.state == proxy_pool.SLOT_READY)

        mgr.tick_health()

        self.assertEqual(active.state, proxy_pool.SLOT_EMPTY)
        self.assertEqual(active.node_id, "")
        mgr.shutdown()

    def test_fatal_health_errors_drop_slot_without_refill(self) -> None:
        fatal_reasons = (
            "<urlopen error timed out>",
            "[错误代码 2005] [ERR_OVPN_AUTH_FAILED] OpenVPN 身份验证失败",
        )
        for reason in fatal_reasons:
            with self.subTest(reason=reason):
                mgr = self._mgr(pool_size=1)
                mgr.health_check = mock.Mock(return_value=(False, reason, {}))
                mgr.start()
                _seed_pool(mgr, [
                    {"id": "A", "country_short": "JP", "country": "Japan", "ip": "1.1.1.1",
                     "score_latency": 5, "config_text": "a", "probe_status": "available"},
                    {"id": "B", "country_short": "US", "country": "US", "ip": "2.2.2.2",
                     "score_latency": 6, "config_text": "b", "probe_status": "available"},
                ])
                _wait_ready(mgr, 1)
                active = next(s for s in mgr.slots if s.state == proxy_pool.SLOT_READY)
                original_process = active.process
                original_listener = active.listener

                mgr.tick_health()

                original_listener.stop.assert_called()
                mgr.stop_openvpn.assert_any_call(original_process)
                self.assertEqual(active.state, proxy_pool.SLOT_EMPTY)
                self.assertEqual(active.node_id, "")
                self.assertFalse(active.replacement_pending)
                mgr.shutdown()


    def test_replace_all_slots_from_target_nodes_rebuilds_exact_target_layout_by_batch(self) -> None:
        def health_check(slot):
            return True, "ok", {"exit_ip": getattr(slot, "node_ip", ""), "latency_ms": 12}

        mgr = self._mgr(pool_size=4, max_starting=1, max_shadow_starting=2)
        mgr.health_check = mock.Mock(side_effect=health_check)
        mgr.start()
        _seed_pool(mgr, [
            {"id": "old-a", "country_short": "JP", "country": "Japan", "ip": "1.1.1.1",
             "score_latency": 10, "ip_type": "hosting", "config_text": "a", "probe_status": "available"},
            {"id": "old-b", "country_short": "JP", "country": "Japan", "ip": "1.1.1.2",
             "score_latency": 11, "ip_type": "hosting", "config_text": "b", "probe_status": "available"},
            {"id": "old-c", "country_short": "JP", "country": "Japan", "ip": "1.1.1.3",
             "score_latency": 12, "ip_type": "hosting", "config_text": "c", "probe_status": "available"},
            {"id": "old-d", "country_short": "JP", "country": "Japan", "ip": "1.1.1.4",
             "score_latency": 13, "ip_type": "hosting", "config_text": "d", "probe_status": "available"},
        ])
        _wait_ready(mgr, 4)
        # Snapshot old processes so we can prove they outlive the rebuild.
        old_procs = [mgr.slots[i].process for i in range(4)]
        old_proc_stop = mock.Mock()
        for p in old_procs:
            p.poll.return_value = None

        started = mgr.replace_all_slots_from_target_nodes([
            {"id": "new-0", "country_short": "TH", "country": "Thailand", "ip": "2.2.2.1",
             "score_latency": 1, "ip_type": "residential", "config_text": "n0", "probe_status": "available"},
            {"id": "new-1", "country_short": "TH", "country": "Thailand", "ip": "2.2.2.2",
             "score_latency": 2, "ip_type": "residential", "config_text": "n1", "probe_status": "available"},
            {"id": "new-2", "country_short": "US", "country": "US", "ip": "2.2.2.3",
             "score_latency": 3, "ip_type": "hosting", "config_text": "n2", "probe_status": "available"},
        ], batch_size=2)

        # Three slots cut over to the new residential/mobile-prioritized targets.
        self.assertEqual(started, 3)
        self.assertEqual([mgr.slots[i].node_id for i in range(3)], ["new-0", "new-1", "new-2"])
        # Slot 3 had no target: full replacement — the old node is dropped.
        self.assertEqual(mgr.slots[3].state, proxy_pool.SLOT_EMPTY)
        self.assertEqual(mgr.slots[3].node_id, "")
        self.assertIsNone(mgr.slots[3].process)
        self.assertGreater(mgr._skipped.get("old-d", 0), 0)
        # Cutover is verify-then-switch: each replaced slot ends up serving its
        # target's exit ip reported by the health check.
        for i in range(3):
            self.assertEqual(mgr.slots[i].exit_ip, f"2.2.2.{i + 1}")
        mgr.shutdown()

    def test_rebuild_freezes_other_slots_during_replace_all(self) -> None:
        import threading as _threading

        slow_gate = _threading.Event()
        start_entered = _threading.Event()
        slow_on = {"v": False}

        def maybe_slow_start(config_path, dev):
            # 新方案下重建起的是 shadow,slot 0 的 shadow tun 是 tun{pool_size+0}=tun4。
            # 阻塞它让重建卡在第一批第一个 shadow 的 start_openvpn,从而观察冻结期行为。
            if slow_on["v"] and dev == "tun4":
                start_entered.set()
                slow_gate.wait(timeout=5)
            proc = mock.Mock()
            proc.poll.return_value = None
            return True, "ok", proc

        mgr = self._mgr(maybe_slow_start, pool_size=4, max_starting=4, max_shadow_starting=2)
        # health_check 总是失败:正常情况下 tick_health 会立即停掉 READY slot
        mgr.health_check = mock.Mock(return_value=(False, "dead", {}))
        mgr.start()
        _seed_pool(mgr, [
            {"id": f"old-{i}", "country_short": "JP", "country": "Japan",
             "ip": f"10.0.0.{i}", "score_latency": 10 + i, "ip_type": "hosting",
             "config_text": f"o{i}", "probe_status": "available"}
            for i in range(4)
        ])
        _wait_ready(mgr, 4)

        slow_on["v"] = True
        new_nodes = [
            {"id": f"new-{i}", "country_short": "TH", "country": "Thailand",
             "ip": f"20.0.0.{i}", "score_latency": i, "ip_type": "residential",
             "config_text": f"n{i}", "probe_status": "available"}
            for i in range(4)
        ]
        rebuild_thread = _threading.Thread(
            target=mgr.replace_all_slots_from_target_nodes,
            args=(new_nodes,),
            kwargs={"batch_size": 2},
            daemon=True,
        )
        rebuild_thread.start()

        # 等待重建进入(_rebuilding 置位)
        deadline = time.time() + 2.0
        while time.time() < deadline and not mgr._rebuilding:
            time.sleep(0.01)
        self.assertTrue(mgr._rebuilding, "rebuild flag should be set during replace_all")
        self.assertTrue(start_entered.wait(timeout=2.0), "first shadow start should be blocked")

        # 重建进行中:被替换 slot 的老节点仍在岗(shadow 起在独立 tun,不碰老 slot),
        # 未涉及到的 slot 2-3 也保持原样不动。
        slot2_node = mgr.slots[2].node_id
        slot2_proc = mgr.slots[2].process
        slot3_node = mgr.slots[3].node_id
        slot0_old_node = "old-0"
        self.assertEqual(mgr.slots[0].state, proxy_pool.SLOT_READY)
        self.assertEqual(mgr.slots[0].node_id, slot0_old_node)

        # tick_health 即使 health_check 会失败,重建期间也不得动任何 slot
        mgr.tick_health()
        self.assertEqual(mgr.health_check.call_count, 0)
        self.assertEqual(mgr.slots[2].state, proxy_pool.SLOT_READY)
        self.assertEqual(mgr.slots[2].node_id, slot2_node)
        self.assertIs(mgr.slots[2].process, slot2_proc)
        self.assertEqual(mgr.slots[3].state, proxy_pool.SLOT_READY)
        self.assertEqual(mgr.slots[3].node_id, slot3_node)

        # 释放第一批,让重建走完
        slow_gate.set()
        rebuild_thread.join(timeout=5)
        self.assertFalse(mgr._rebuilding, "rebuild flag should clear after replace_all")
        mgr.shutdown()

    def test_rebuild_shadow_health_fail_cutover_anyway(self) -> None:
        # health_check 不再是门控：验通失败时也照常 cutover 到新节点,
        # 老节点被下掉。验证结果仅用于填充 exit_ip 供查询展示。
        def health_check(slot):
            # slot 0 的 shadow 验通失败,其余通过。shadow 端口在 53000 段。
            is_shadow = getattr(slot, "port", 0) >= 53000
            shadow_index = getattr(slot, "index", -1)
            if is_shadow and shadow_index == 0:
                return False, "dead exit", {}
            return True, "ok", {"exit_ip": getattr(slot, "node_ip", ""), "latency_ms": 12}

        mgr = self._mgr(pool_size=3, max_starting=2, max_shadow_starting=3)
        mgr.health_check = mock.Mock(side_effect=health_check)
        mgr.start()
        _seed_pool(mgr, [
            {"id": f"old-{i}", "country_short": "JP", "country": "Japan",
             "ip": f"10.0.0.{i}", "score_latency": 10 + i, "ip_type": "hosting",
             "config_text": f"o{i}", "probe_status": "available"}
            for i in range(3)
        ])
        _wait_ready(mgr, 3)
        old_proc0 = mgr.slots[0].process

        new_nodes = [
            {"id": "new-0", "country_short": "TH", "country": "Thailand", "ip": "20.0.0.0",
             "score_latency": 1, "ip_type": "residential", "config_text": "n0", "probe_status": "available"},
            {"id": "new-1", "country_short": "TH", "country": "Thailand", "ip": "20.0.0.1",
             "score_latency": 2, "ip_type": "residential", "config_text": "n1", "probe_status": "available"},
            {"id": "new-2", "country_short": "TH", "country": "Thailand", "ip": "20.0.0.2",
             "score_latency": 3, "ip_type": "residential", "config_text": "n2", "probe_status": "available"},
        ]
        started = mgr.replace_all_slots_from_target_nodes(new_nodes, batch_size=3)

        # 三个 slot 全部 cutover 到新节点 —— 验通失败的 new-0 也切了。
        self.assertEqual(started, 3)
        self.assertEqual(mgr.slots[0].state, proxy_pool.SLOT_READY)
        self.assertEqual(mgr.slots[0].node_id, "new-0")
        # 老节点 old-0 的进程在 cutover 时被下掉,不再是原进程。
        self.assertIsNot(mgr.slots[0].process, old_proc0)
        self.assertIsNone(mgr.slots[0].shadow)
        self.assertEqual(mgr.slots[1].node_id, "new-1")
        self.assertEqual(mgr.slots[2].node_id, "new-2")
        # 验通失败不再阻止 cutover,new-0 已上线,不进 _skipped。
        self.assertNotIn("new-0", mgr._skipped)
        mgr.shutdown()

    def test_rebuild_cutover_failure_keeps_old_slot_and_no_orphan(self) -> None:
        # cutover 时 create_listener 抛异常:shadow 进程必须被停掉(无孤儿),
        # 老节点保留在岗,replacement 字段清空,slot 不卡死(下轮可重试)。
        def health_check(slot):
            return True, "ok", {"exit_ip": getattr(slot, "node_ip", ""), "latency_ms": 12}

        call_count = {"n": 0}

        def listener_factory(**kwargs):
            port = kwargs["port"]
            call_count["n"] += 1
            # 前 pool_size 次是 seed 首填充(slot 公开端口),正常起;
            # 之后是 cutover 切公开端口,抛异常触发回滚。
            if call_count["n"] > 2:
                raise OSError("cutover listener bind failed")
            lis = mock.Mock()
            lis.start.return_value = port
            lis.is_alive.return_value = True
            lis.stop = mock.Mock()
            return lis

        stopped_procs = []

        def stop_openvpn(proc):
            stopped_procs.append(proc)

        mgr = self._mgr(pool_size=2, max_starting=2, max_shadow_starting=2)
        mgr.health_check = mock.Mock(side_effect=health_check)
        mgr.create_listener = listener_factory
        mgr.stop_openvpn = stop_openvpn
        mgr.start()
        _seed_pool(mgr, [
            {"id": f"old-{i}", "country_short": "JP", "country": "Japan",
             "ip": f"10.0.0.{i}", "score_latency": 10 + i, "ip_type": "hosting",
             "config_text": f"o{i}", "probe_status": "available"}
            for i in range(2)
        ])
        _wait_ready(mgr, 2)
        old_procs = [mgr.slots[i].process for i in range(2)]

        new_nodes = [
            {"id": f"new-{i}", "country_short": "TH", "country": "Thailand",
             "ip": f"20.0.0.{i}", "score_latency": i, "ip_type": "residential",
             "config_text": f"n{i}", "probe_status": "available"}
            for i in range(2)
        ]
        started = mgr.replace_all_slots_from_target_nodes(new_nodes, batch_size=2)

        # cutover 全失败,没有 slot 成功切到 target。
        self.assertEqual(started, 0)
        for i in range(2):
            slot = mgr.slots[i]
            # 老节点保留在岗。
            self.assertEqual(slot.state, proxy_pool.SLOT_READY)
            self.assertEqual(slot.node_id, f"old-{i}")
            self.assertIs(slot.process, old_procs[i])
            # replacement 状态已清空,shadow 已清 —— 不卡死,下轮可重试。
            self.assertIsNone(slot.shadow)
            self.assertFalse(slot.replacement_pending)
        # shadow 起的 OpenVPN 进程已被停掉(无孤儿)。
        self.assertEqual(len(stopped_procs), 2)
        mgr.shutdown()

    def test_rebuild_shadow_health_check_timeout_cutover_anyway(self) -> None:
        # health_check 永久阻塞超过硬超时:不再按"验证失败"保留老节点,
        # 而是照常 cutover 到新节点。硬超时的作用是防止 rebuild 线程被卡死,
        # 而不是阻止切换。老节点在 cutover 时被下掉。
        import threading as _t

        block_evt = _t.Event()

        def health_check(slot):
            block_evt.wait(30)  # 永远不 set → 阻塞
            return True, "ok", {"exit_ip": getattr(slot, "node_ip", ""), "latency_ms": 1}

        stopped_procs = []

        def stop_openvpn(proc):
            stopped_procs.append(proc)

        mgr = self._mgr(pool_size=2, max_starting=2, max_shadow_starting=2,
                        health_check_timeout=0.3)
        mgr.health_check = mock.Mock(side_effect=health_check)
        mgr.stop_openvpn = stop_openvpn
        mgr.start()
        _seed_pool(mgr, [
            {"id": f"old-{i}", "country_short": "JP", "country": "Japan",
             "ip": f"10.0.0.{i}", "score_latency": 10 + i, "ip_type": "hosting",
             "config_text": f"o{i}", "probe_status": "available"}
            for i in range(2)
        ])
        _wait_ready(mgr, 2)
        old_procs = [mgr.slots[i].process for i in range(2)]

        new_nodes = [
            {"id": f"new-{i}", "country_short": "TH", "country": "Thailand",
             "ip": f"20.0.0.{i}", "score_latency": i, "ip_type": "residential",
             "config_text": f"n{i}", "probe_status": "available"}
            for i in range(2)
        ]
        started = mgr.replace_all_slots_from_target_nodes(new_nodes, batch_size=2)

        # 硬超时触发,但两个 shadow 仍 cutover 到新节点 —— 超时不阻止切换。
        self.assertEqual(started, 2)
        for i in range(2):
            slot = mgr.slots[i]
            self.assertEqual(slot.state, proxy_pool.SLOT_READY)
            self.assertEqual(slot.node_id, f"new-{i}")
            # 老节点进程在 cutover 时被下掉,不再是原进程。
            self.assertIsNot(slot.process, old_procs[i])
            # shadow 已清,不卡死。
            self.assertIsNone(slot.shadow)
            self.assertFalse(slot.replacement_pending)
        # 两个老 OpenVPN 进程在 cutover 时被停掉。
        self.assertEqual(len(stopped_procs), 2)
        # cutover 成功,new-0 已上线,不进 skip 窗口。
        self.assertNotIn("new-0", mgr._skipped)
        block_evt.set()  # 释放卡住的 health_check 线程
        mgr.shutdown()

    def test_rebuild_shadow_join_timeout_reaps_slot(self) -> None:
        # join 超时(线程仍卡在 health_check 且硬超时还没到):rebuild 必须强制
        # 回收 shadow(杀进程、清 slot.shadow),让该 slot 下轮可重试,而不是
        # 被 "slot.shadow is not None" 永久跳过 —— 这正是服务器上泄漏的修复点。
        import threading as _t

        block_evt = _t.Event()

        def health_check(slot):
            block_evt.wait(30)  # 阻塞到测试结束
            return True, "ok", {"exit_ip": getattr(slot, "node_ip", ""), "latency_ms": 1}

        stopped_procs = []

        def stop_openvpn(proc):
            stopped_procs.append(proc)

        mgr = self._mgr(pool_size=1, max_starting=1, max_shadow_starting=1,
                        health_check_timeout=30)  # 硬超时 30s,远大于 join 超时
        # 绕过 __init__ 的 max(30, ...) 下限,让 join 超时快速触发。
        mgr.slot_start_timeout = 0.3
        mgr.health_check = mock.Mock(side_effect=health_check)
        mgr.stop_openvpn = stop_openvpn
        mgr.start()
        _seed_pool(mgr, [
            {"id": "old-0", "country_short": "JP", "country": "Japan", "ip": "10.0.0.0",
             "score_latency": 10, "ip_type": "hosting", "config_text": "o0",
             "probe_status": "available"}
        ])
        _wait_ready(mgr, 1)
        old_proc = mgr.slots[0].process

        new_nodes = [
            {"id": "new-0", "country_short": "TH", "country": "Thailand", "ip": "20.0.0.0",
             "score_latency": 1, "ip_type": "residential", "config_text": "n0",
             "probe_status": "available"}
        ]
        started = mgr.replace_all_slots_from_target_nodes(new_nodes, batch_size=1)

        # join 超时强制回收 → 没有 slot 切到 target。
        self.assertEqual(started, 0)
        slot = mgr.slots[0]
        # 老节点保留在岗。
        self.assertEqual(slot.state, proxy_pool.SLOT_READY)
        self.assertEqual(slot.node_id, "old-0")
        self.assertIs(slot.process, old_proc)
        # shadow 被强制清空 —— 下轮可重试,不永久跳过。
        self.assertIsNone(slot.shadow)
        self.assertFalse(slot.replacement_pending)
        # 失败 target 进 skip 窗口。
        self.assertGreater(mgr._skipped.get("new-0", 0), 0)
        block_evt.set()  # 释放卡住的 health_check 线程
        mgr.shutdown()

    def test_wait_fill_idle_bounded_orphans_stuck_fill_thread(self) -> None:
        # _wait_fill_idle 必须有界:fill 线程卡死时不能无限等(否则 _rebuilding
        # 永真、tick_health 永久冻结,代理池停止更新)。超时后应孤儿化卡住的
        # fill 线程(置 None)让 rebuild 推进、_rebuilding 能复位。
        import threading as _t

        mgr = self._mgr(pool_size=1, max_starting=1)
        mgr.start()

        block_evt = _t.Event()

        def stuck_fill():
            block_evt.wait(30)  # 永不 set,模拟 fill 线程卡死

        stuck = _t.Thread(target=stuck_fill, daemon=True)
        stuck.start()
        mgr._fill_thread = stuck

        ok = mgr._wait_fill_idle(timeout=0.2)
        # 超时返回 False,卡住的 fill 线程被孤儿化。
        self.assertFalse(ok)
        self.assertIsNone(mgr._fill_thread)

        block_evt.set()  # 释放卡住的线程
        mgr.shutdown()

    def test_wait_fill_idle_releases_rebuilding_on_stuck_fill(self) -> None:
        # 服务器现象的核心:fill 卡住时 replace_all 仍能让 _rebuilding 复位,
        # 否则 tick_health 被永久冻结。这是"运行长就不更新"的直接回归点。
        import threading as _t

        mgr = self._mgr(pool_size=1, max_starting=1, max_shadow_starting=1)
        mgr.start()
        _seed_pool(mgr, [
            {"id": "old-0", "country_short": "JP", "country": "Japan", "ip": "10.0.0.0",
             "score_latency": 10, "ip_type": "hosting", "config_text": "o0",
             "probe_status": "available"}
        ])
        _wait_ready(mgr, 1)

        block_evt = _t.Event()

        def stuck_fill():
            block_evt.wait(30)

        stuck = _t.Thread(target=stuck_fill, daemon=True)
        stuck.start()
        mgr._fill_thread = stuck

        # 用一个有新 target 的节点触发 replace_all,但 fill 卡住。
        new_nodes = [
            {"id": "new-0", "country_short": "TH", "country": "Thailand", "ip": "20.0.0.0",
             "score_latency": 1, "ip_type": "residential", "config_text": "n0",
             "probe_status": "available"}
        ]
        # 缩短等待上限,让测试快速完成。
        mgr.replacement_grace_seconds = 0.3
        mgr.replace_all_slots_from_target_nodes(new_nodes, batch_size=1)

        # _rebuilding 必然复位 —— replace_all 不会因 fill 卡死而永远阻塞。
        self.assertFalse(mgr._rebuilding)
        # 卡住的 fill 线程被孤儿化。
        self.assertIsNone(mgr._fill_thread)

        block_evt.set()
        mgr.shutdown()

    def test_fill_loop_reaps_stuck_starting_slot(self) -> None:
        # fill 路径 _start_reserved_slot 卡过 slot_start_timeout:_run_fill_loop
        # 的 join 超时后必须把 STARTING slot 复位回 EMPTY 并进 _skipped,
        # 否则该 slot 被 _reserve_start_task_locked(只挑 EMPTY)永久漏掉。
        import threading as _t

        gate = _t.Event()

        def slow_start(config_path, dev):
            gate.wait(30)  # 卡住,超过 slot_start_timeout
            proc = mock.Mock()
            proc.poll.return_value = None
            return True, "ok", proc

        mgr = self._mgr(start_side_effect=slow_start, pool_size=2, max_starting=2)
        mgr.slot_start_timeout = 0.3
        mgr.start()
        _seed_pool(mgr, [
            {"id": "A", "country_short": "JP", "country": "Japan", "ip": "1.1.1.1",
             "score_latency": 5, "ip_type": "hosting", "config_text": "a",
             "probe_status": "available"},
            {"id": "B", "country_short": "US", "country": "US", "ip": "2.2.2.2",
             "score_latency": 6, "ip_type": "hosting", "config_text": "b",
             "probe_status": "available"},
        ])

        # 触发 fill:两个 EMPTY slot 都进 STARTING,但 _start_reserved_slot 卡在
        # start_openvpn,超过 slot_start_timeout(0.3s)后被 reap。
        mgr._request_fill_slots()
        # 等 fill 线程跑完一轮 join(含 reap)。
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if all(s.state == proxy_pool.SLOT_EMPTY for s in mgr.slots):
                break
            time.sleep(0.02)

        for slot in mgr.slots:
            self.assertEqual(slot.state, proxy_pool.SLOT_EMPTY,
                             f"slot {slot.index} should be reaped back to EMPTY")
        # 被 reap 的节点进 skip 窗口。
        self.assertGreater(mgr._skipped.get("A", 0), 0)
        self.assertGreater(mgr._skipped.get("B", 0), 0)

        gate.set()
        mgr.shutdown()



def _wait_node_ids(mgr: proxy_pool.PoolManager, expect_ids: set[str], timeout: float = 2.0) -> None:
    """轮询直到 READY slot 的 node_id 集合包含 expect_ids(异步 cutover 完成后)。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        ids = {s.node_id for s in mgr.slots if s.state == proxy_pool.SLOT_READY and s.node_id}
        if expect_ids <= ids:
            return
        time.sleep(0.01)


def _rolling_mgr(pool_size: int = 10, max_starting: int = 10, max_shadow_starting: int = 10,
                 health_ok: bool = True) -> proxy_pool.PoolManager:
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

    mgr = proxy_pool.PoolManager(
        pool_size=pool_size,
        port_base=52000,
        public_host="127.0.0.1",
        listen_host="127.0.0.1",
        proxy_user="u",
        proxy_pass="p",
        return_credentials=True,
        max_starting=max_starting,
        max_shadow_starting=max_shadow_starting,
        start_openvpn=ok_start,
        stop_openvpn=mock.Mock(),
        create_listener=listener_factory,
        log=lambda *a, **k: None,
        write_config=lambda node, path: path.write_text(node.get("config_text") or "", encoding="utf-8"),
        config_dir=None,
    )
    # health_check 返回 slot 自己的 node_ip 作为 exit_ip,让 cutover 拿得到 exit_ip
    def health_check(slot):
        return (True, "ok", {"exit_ip": getattr(slot, "node_ip", ""), "latency_ms": 12}) if health_ok \
            else (False, "bad", {})
    mgr.health_check = mock.Mock(side_effect=health_check)
    return mgr


def _hosting_node(node_id: str, ip: str, latency: int = 10, cfg: str = "x") -> dict[str, object]:
    return {"id": node_id, "country_short": "JP", "country": "Japan", "ip": ip,
            "score_latency": latency, "ip_type": "hosting", "config_text": cfg,
            "probe_status": "available"}


def _resi_node(node_id: str, ip: str, latency: int = 10, cfg: str = "x") -> dict[str, object]:
    return {"id": node_id, "country_short": "US", "country": "US", "ip": ip,
            "score_latency": latency, "ip_type": "residential", "config_text": cfg,
            "probe_status": "available"}


def _seeded_candidates(pool_nodes: list[dict[str, object]]) -> list[dict[str, object]]:
    """把池内节点转成候选(在列),让两阶段对账不把它们判为历史节点。"""
    return [_hosting_node(str(n["id"]), str(n["ip"])) for n in pool_nodes]


class PoolRollingReplaceTests(unittest.TestCase):
    def test_rolling_replace_replaces_only_ten_percent(self) -> None:
        mgr = _rolling_mgr(pool_size=10, max_starting=10, max_shadow_starting=10)
        mgr.start()
        pool_nodes = [_hosting_node(f"old-{i}", f"10.0.0.{i}") for i in range(10)]
        _seed_pool(mgr, pool_nodes)
        _wait_ready(mgr, 10)

        # 候选含池内在列节点(old-*)+ 新节点:在列节点只按 10% 配额轮换
        cands = list(pool_nodes) + [_resi_node(f"new-{i}", f"20.0.0.{i}") for i in range(3)]
        started = mgr.rolling_replace_from_nodes(cands)
        _wait_node_ids(mgr, {"new-0"})

        # 10% of 10 = 1 个被换;其余 9 个仍是 old
        self.assertEqual(started, 1)
        ready_ids = {s.node_id for s in mgr.slots if s.state == proxy_pool.SLOT_READY}
        self.assertIn("new-0", ready_ids)
        self.assertEqual(len(ready_ids & {f"old-{i}" for i in range(10)}), 9)
        mgr.shutdown()

    def test_rolling_replace_cursor_advances_and_wraps(self) -> None:
        mgr = _rolling_mgr(pool_size=4, max_starting=4, max_shadow_starting=4)
        mgr.start()
        pool_nodes = [_hosting_node(f"old-{i}", f"10.0.0.{i}") for i in range(4)]
        _seed_pool(mgr, pool_nodes)
        _wait_ready(mgr, 4)

        # 每次只换 1 个(4//10 -> max(1,0)=1)。候选始终含在列节点(含上轮换进
        # 来的),阶段一对账不触发,纯测阶段二轮换游标依次 0,1,2,3,0...
        listed = list(pool_nodes)
        for i in range(5):
            cands = list(listed) + [_resi_node(f"new-{i}", f"20.0.0.{i}")]
            mgr.rolling_replace_from_nodes(cands)
            _wait_node_ids(mgr, {f"new-{i}"})
            # 把上一轮换进来的 new-i 挂回在列名单,防止下一轮被判历史节点
            listed = [_hosting_node(n["id"], f"20.0.0.{i}") if n["id"] == f"new-{i}" else n
                      for n in listed + [_resi_node(f"new-{i}", f"20.0.0.{i}")]]

        # 5 次后 cursor = 5 % 4 = 1
        self.assertEqual(mgr.refresh_cursor, 5 % 4)
        ready = [s.node_id for s in mgr.slots if s.state == proxy_pool.SLOT_READY]
        # slot 0 被 new-4 覆盖(第5次 wrap 回 slot 0)
        self.assertEqual(ready[0], "new-4")
        mgr.shutdown()

    def test_rolling_replace_prefers_residential_over_hosting(self) -> None:
        mgr = _rolling_mgr(pool_size=4, max_starting=4, max_shadow_starting=4)
        mgr.start()
        pool_nodes = [_hosting_node(f"old-{i}", f"10.0.0.{i}") for i in range(4)]
        _seed_pool(mgr, pool_nodes)
        _wait_ready(mgr, 4)

        # 候选混 residential + hosting + 在列 old-*;_candidate_priority_key 让 residential 排前
        cands = [_resi_node("resi-0", "20.0.0.0"), _hosting_node("h-0", "30.0.0.0"), *pool_nodes]
        mgr.rolling_replace_from_nodes(cands)
        _wait_node_ids(mgr, {"resi-0"})

        ready_ids = {s.node_id for s in mgr.slots if s.state == proxy_pool.SLOT_READY}
        self.assertIn("resi-0", ready_ids)
        self.assertNotIn("h-0", ready_ids)
        mgr.shutdown()

    def test_rolling_replace_skips_non_ready_slots(self) -> None:
        mgr = _rolling_mgr(pool_size=4, max_starting=4, max_shadow_starting=4)
        mgr.start()
        pool_nodes = [_hosting_node(f"old-{i}", f"10.0.0.{i}") for i in range(4)]
        _seed_pool(mgr, pool_nodes)
        _wait_ready(mgr, 4)
        # 把 slot 0 置成 STARTING(fill 不挑 STARTING,滚动也跳过非 READY),
        # 测纯滚动跳过逻辑:slot 0 不参与 cutover,换的是下一个 READY slot。
        mgr.slots[0].state = proxy_pool.SLOT_STARTING
        mgr.slots[0].process = mock.Mock()
        mgr.slots[0].process.poll.return_value = None

        mgr.rolling_replace_from_nodes([*_seeded_candidates(pool_nodes), _resi_node("new-0", "20.0.0.0")])
        _wait_node_ids(mgr, {"new-0"})

        # slot 0 仍是 old-0(STARTING,被跳过),换的是 slot 1
        self.assertEqual(mgr.slots[0].node_id, "old-0")
        self.assertEqual(mgr.slots[1].node_id, "new-0")
        self.assertGreaterEqual(mgr.refresh_cursor, 2)
        mgr.shutdown()

    def test_rolling_replace_skips_slots_with_pending_replacement(self) -> None:
        mgr = _rolling_mgr(pool_size=4, max_starting=4, max_shadow_starting=4)
        mgr.start()
        pool_nodes = [_hosting_node(f"old-{i}", f"10.0.0.{i}") for i in range(4)]
        _seed_pool(mgr, pool_nodes)
        _wait_ready(mgr, 4)
        # slot 0 预置 replacement_pending + shadow
        mgr.slots[0].replacement_pending = True
        mgr.slots[0].shadow = proxy_pool.ShadowCandidate(index=0, tun_name="tun4", port=53000)

        mgr.rolling_replace_from_nodes([*_seeded_candidates(pool_nodes), _resi_node("new-0", "20.0.0.0")])
        _wait_node_ids(mgr, {"new-0"})

        # slot 0 被跳过,换的是 slot 1
        self.assertEqual(mgr.slots[1].node_id, "new-0")
        self.assertGreaterEqual(mgr.refresh_cursor, 2)
        mgr.shutdown()

    def test_rolling_replace_resets_cursor_on_new_instance(self) -> None:
        a = _rolling_mgr(pool_size=4)
        b = _rolling_mgr(pool_size=4)
        self.assertEqual(a.refresh_cursor, 0)
        self.assertEqual(b.refresh_cursor, 0)
        a.shutdown()
        b.shutdown()

    def test_rolling_replace_caps_at_max_shadow_starting(self) -> None:
        # pool 30 -> routine_floor = 30//10 = 3,max_shadow_starting 被 floor 拉到 3。
        # 显式 batch_size=5 把 quota 拉到 5,超过并发预算 3 -> 只起 3 个。
        mgr = _rolling_mgr(pool_size=30, max_starting=3, max_shadow_starting=3)
        mgr.start()
        _seed_pool(mgr, [_hosting_node(f"old-{i}", f"10.0.0.{i}") for i in range(30)])
        _wait_ready(mgr, 30)

        cands = [_resi_node(f"new-{i}", f"20.0.0.{i}") for i in range(30)]
        started = mgr.rolling_replace_from_nodes(cands, batch_size=5)
        # quota=5 但并发预算=3 -> 只发起 3 个
        self.assertEqual(started, 3)
        _wait_node_ids(mgr, {"new-0", "new-1", "new-2"})
        # 再调:前 3 个 shadow 已 cutover 释放,又能起 3 个
        started2 = mgr.rolling_replace_from_nodes(cands, batch_size=5)
        self.assertEqual(started2, 3)
        mgr.shutdown()

    def test_rolling_replace_updates_last_candidates(self) -> None:
        mgr = _rolling_mgr(pool_size=4, max_starting=4, max_shadow_starting=4)
        mgr.start()
        _seed_pool(mgr, [_hosting_node(f"old-{i}", f"10.0.0.{i}") for i in range(4)])
        _wait_ready(mgr, 4)

        new_nodes = [_resi_node("new-0", "20.0.0.0"), _resi_node("new-1", "20.0.0.1")]
        mgr.rolling_replace_from_nodes(new_nodes)

        # _last_candidates 更新为 dedupe+排序后的候选,供 fill 空槽补位用
        self.assertEqual([n["id"] for n in mgr._last_candidates], ["new-0", "new-1"])
        mgr.shutdown()

    def test_rolling_replace_fills_empty_pool(self) -> None:
        # 空池调 rolling_replace 应先把 EMPTY 填到 READY(fill 路径),不能只滚不填。
        mgr = _rolling_mgr(pool_size=4, max_starting=4, max_shadow_starting=4)
        mgr.start()
        # 不 seed,池子全 EMPTY
        nodes = [_hosting_node(f"fill-{i}", f"10.0.0.{i}") for i in range(4)]
        mgr.rolling_replace_from_nodes(nodes)
        _wait_ready(mgr, 4)

        ready_ids = {s.node_id for s in mgr.slots if s.state == proxy_pool.SLOT_READY}
        self.assertEqual(ready_ids, {f"fill-{i}" for i in range(4)})
        # 全是 fill 来的,没有 shadow cutover,cursor 仍 0
        self.assertEqual(mgr.refresh_cursor, 0)
        mgr.shutdown()

    def test_shadow_tun_name_ping_pongs_after_cutover(self) -> None:
        # cutover 后 shadow 进程转正占着本轮影子名(slot.device_name),下一轮
        # shadow 必须换用另一个名字,否则 TUNSETIFF 撞自己主进程(errno=16),
        # cutover 永远不发生,slot 卡死在老节点上(生产 188 实测)。
        # pool_size=4 保证 seed 后无空槽,fill 不会抢走滚动候选。
        mgr = _rolling_mgr(pool_size=4, max_starting=4, max_shadow_starting=4)
        mgr.start()
        _seed_pool(mgr, [_hosting_node("old-0", "10.0.0.0"), _hosting_node("old-1", "10.0.0.1"),
                         _hosting_node("old-2", "10.0.0.2"), _hosting_node("old-3", "10.0.0.3")])
        _wait_ready(mgr, 4)

        slot = mgr.slots[0]
        # 初始:主进程在 tun0,影子名 tun4(pool_size+index)
        self.assertEqual(slot.device_name, "tun0")
        self.assertEqual(mgr._shadow_tun_name(slot), "tun4")

        # 第一次滚动 cutover:shadow 用 tun4,转正后 device_name=tun4
        mgr.rolling_replace_from_nodes([_hosting_node("old-0", "10.0.0.0"), _hosting_node("old-1", "10.0.0.1"),
                                       _hosting_node("old-2", "10.0.0.2"), _hosting_node("old-3", "10.0.0.3"),
                                       _resi_node("new-0", "20.0.0.0")])
        _wait_node_ids(mgr, {"new-0"})
        self.assertEqual(slot.node_id, "new-0")
        self.assertEqual(slot.device_name, "tun4")

        # 归零 cursor 让第二轮仍从 slot 0 开始(否则 quota=1 会滚到 slot 1)
        mgr.refresh_cursor = 0
        # 第二次滚动 cutover:影子名必须乒乓到 tun8(2*pool_size),不能再用 tun4
        mgr.rolling_replace_from_nodes([_hosting_node("old-1", "10.0.0.1"), _hosting_node("old-2", "10.0.0.2"),
                                       _hosting_node("old-3", "10.0.0.3"),
                                       _resi_node("new-1", "20.0.0.1")])
        _wait_node_ids(mgr, {"new-1"})
        self.assertEqual(slot.node_id, "new-1")
        self.assertEqual(slot.device_name, "tun8")

        # 第三次:乒乓回 tun4(tun4 已随第一代进程停止而释放)
        mgr.refresh_cursor = 0
        mgr.rolling_replace_from_nodes([_hosting_node("old-1", "10.0.0.1"), _hosting_node("old-2", "10.0.0.2"),
                                       _hosting_node("old-3", "10.0.0.3"),
                                       _resi_node("new-2", "20.0.0.2")])
        _wait_node_ids(mgr, {"new-2"})
        self.assertEqual(slot.device_name, "tun4")
        mgr.shutdown()

    def test_rolling_replace_reaps_all_stale_nodes_beyond_quota(self) -> None:
        # 阶段一(全量对账):node_id 不在当前候选列表的 READY slot 是历史节点,
        # 全部替换,不受 10% 配额限制——用户要求池里不允许残留历史节点。
        mgr = _rolling_mgr(pool_size=10, max_starting=10, max_shadow_starting=10)
        mgr.start()
        # seed 5 个历史节点 + 5 个在列节点;候选只含 5 个在列节点 + 5 个新节点
        pool_nodes = [_hosting_node(f"gone-{i}", f"10.1.0.{i}") for i in range(5)] + \
                     [_hosting_node(f"keep-{i}", f"10.2.0.{i}") for i in range(5)]
        _seed_pool(mgr, pool_nodes)
        _wait_ready(mgr, 10)

        candidates = [_hosting_node(f"keep-{i}", f"10.2.0.{i}") for i in range(5)] + \
                     [_resi_node(f"fresh-{i}", f"20.0.0.{i}") for i in range(5)]
        started = mgr.rolling_replace_from_nodes(candidates)
        # 10% 配额只有 1,但 5 个历史节点必须全部清退
        self.assertEqual(started, 5)
        _wait_node_ids(mgr, {f"fresh-{i}" for i in range(5)})

        ready_ids = {s.node_id for s in mgr.slots if s.state == proxy_pool.SLOT_READY}
        # 历史节点全部下线,在列节点保留
        self.assertFalse(ready_ids & {f"gone-{i}" for i in range(5)})
        self.assertEqual(ready_ids & {f"keep-{i}" for i in range(5)}, {f"keep-{i}" for i in range(5)})
        # 替换历史节点用的原因是 rolling-stale(诊断可辨)
        self.assertTrue(all(s.replacement_reason != "rolling-stale" or s.node_id == "fresh" for s in mgr.slots))
        mgr.shutdown()

    def test_rolling_replace_rotates_listed_nodes_within_quota(self) -> None:
        # 阶段二(防老化轮换):在列健康 slot 只按 10% 配额换,不会全量换掉。
        mgr = _rolling_mgr(pool_size=10, max_starting=10, max_shadow_starting=10)
        mgr.start()
        pool_nodes = [_hosting_node(f"keep-{i}", f"10.0.0.{i}") for i in range(10)]
        _seed_pool(mgr, pool_nodes)
        _wait_ready(mgr, 10)

        candidates = list(pool_nodes) + [_resi_node(f"fresh-{i}", f"20.0.0.{i}") for i in range(3)]
        started = mgr.rolling_replace_from_nodes(candidates)
        # 无历史节点;10% 配额 = 1,只换 1 个
        self.assertEqual(started, 1)
        _wait_node_ids(mgr, {"fresh-0"})

        ready_ids = {s.node_id for s in mgr.slots if s.state == proxy_pool.SLOT_READY}
        self.assertIn("fresh-0", ready_ids)
        # 其余 9 个在列节点纹丝不动
        self.assertEqual(len(ready_ids & {f"keep-{i}" for i in range(10)}), 9)
        mgr.shutdown()

    def test_rolling_replace_stale_takes_priority_over_rotation(self) -> None:
        # 历史节点清退优先消耗候选与并发预算;预算内轮换排在清退之后。
        mgr = _rolling_mgr(pool_size=4, max_starting=4, max_shadow_starting=4)
        mgr.start()
        pool_nodes = [_hosting_node("gone-0", "10.1.0.0")] + \
                     [_hosting_node(f"keep-{i}", f"10.2.0.{i}") for i in range(3)]
        _seed_pool(mgr, pool_nodes)
        _wait_ready(mgr, 4)

        candidates = [_hosting_node(f"keep-{i}", f"10.2.0.{i}") for i in range(3)] + \
                     [_resi_node(f"fresh-{i}", f"20.0.0.{i}") for i in range(3)]
        mgr.rolling_replace_from_nodes(candidates)
        _wait_node_ids(mgr, {"fresh-0"})

        # gone-0 被换成 fresh-0(清退优先拿走第一个候选)
        self.assertEqual(mgr.slots[0].node_id, "fresh-0")
        mgr.shutdown()


if __name__ == "__main__":
    unittest.main()
