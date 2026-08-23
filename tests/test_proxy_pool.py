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

    def test_rebuild_shadow_health_fail_keeps_old_slot(self) -> None:
        # shadow 验通失败时:老节点必须在岗、shadow 被清理、失败 target 进 _skipped,
        # 且不影响其它 slot 的 cutover。
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

        # slot 1/2 cutover 成功,slot 0 验通失败保留老节点。
        self.assertEqual(started, 2)
        self.assertEqual(mgr.slots[0].state, proxy_pool.SLOT_READY)
        self.assertEqual(mgr.slots[0].node_id, "old-0")
        self.assertIs(mgr.slots[0].process, old_proc0)
        self.assertIsNone(mgr.slots[0].shadow)
        self.assertEqual(mgr.slots[1].node_id, "new-1")
        self.assertEqual(mgr.slots[2].node_id, "new-2")
        # 失败的 new-0 进 skip 窗口,不会立刻被重试。
        self.assertGreater(mgr._skipped.get("new-0", 0), 0)
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



if __name__ == "__main__":
    unittest.main()
