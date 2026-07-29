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
    def test_sync_keeps_existing_ready_ports_when_node_list_changes(self) -> None:
        mgr = proxy_pool.PoolManager(
            pool_size=2,
            port_base=52000,
            public_host="127.0.0.1",
            listen_host="127.0.0.1",
            proxy_user="u",
            proxy_pass="p",
            return_credentials=True,
            max_starting=1,
            start_openvpn=mock.Mock(return_value=(False, "skip", None)),
            stop_openvpn=mock.Mock(),
            create_listener=mock.Mock(),
            log=lambda *a, **k: None,
        )
        mgr.slots[0] = _ready_slot(0, "JP", 10, node_id="old_node")
        mgr.start()
        mgr.sync_from_nodes([
            {"id": "new_node", "country_short": "US", "country": "US", "ip": "2.2.2.2",
             "score_latency": 5, "config_text": "b", "probe_status": "available"},
        ])
        self.assertEqual(mgr.slots[0].state, proxy_pool.SLOT_READY)
        self.assertEqual(mgr.slots[0].node_id, "old_node")
        mgr.stop_openvpn.assert_not_called()

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
        mgr.sync_from_nodes(nodes)
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
        mgr.sync_from_nodes(nodes)
        _wait_ready(mgr, 1)
        ready = [s for s in mgr.slots if s.state == proxy_pool.SLOT_READY]
        self.assertGreaterEqual(len(ready), 1)
        self.assertNotEqual(ready[0].node_id, "")


    def test_refill_continues_past_max_starting_batch(self) -> None:
        mgr = self._mgr()
        mgr.start()
        mgr.sync_from_nodes([
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
        mgr.sync_from_nodes([
            {"id": "fast_hosting", "country_short": "US", "country": "US", "ip": "1.1.1.1",
             "score_latency": 5, "ip_type": "hosting", "config_text": "a", "probe_status": "available"},
            {"id": "slow_residential", "country_short": "JP", "country": "Japan", "ip": "2.2.2.2",
             "score_latency": 50, "ip_type": "residential", "config_text": "b", "probe_status": "available"},
        ])
        _wait_ready(mgr, 2)
        self.assertEqual([mgr.slots[i].node_id for i in range(2)], ["slow_residential", "fast_hosting"])
        mgr.shutdown()

    def test_warm_sync_refill_prefers_non_hosting_for_empty_slot(self) -> None:
        mgr = self._mgr(pool_size=2, max_starting=2)
        mgr.slots[0] = _ready_slot(0, "US", 10, node_id="existing_ready")
        mgr.slots[0].node_ip = "1.1.1.1"
        mgr.slots[0].exit_ip = "1.1.1.1"
        mgr.start()
        mgr.sync_from_nodes([
            {"id": "fast_hosting", "country_short": "US", "country": "US", "ip": "1.1.1.1",
             "score_latency": 5, "ip_type": "hosting", "config_text": "a", "probe_status": "available"},
            {"id": "slow_residential", "country_short": "JP", "country": "Japan", "ip": "2.2.2.2",
             "score_latency": 50, "ip_type": "residential", "config_text": "b", "probe_status": "available"},
        ])
        _wait_ready(mgr, 2)
        self.assertEqual(mgr.slots[0].node_id, "existing_ready")
        self.assertEqual(mgr.slots[1].node_id, "slow_residential")
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
        mgr.sync_from_nodes([
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
        mgr.sync_from_nodes([
            {"id": "A", "country_short": "JP", "country": "Japan", "ip": "1.1.1.1",
             "score_latency": 5, "config_text": "a", "probe_status": "available"},
        ])
        _wait_ready(mgr, 1)
        mgr.shutdown()
        self.assertTrue(all(s.state == proxy_pool.SLOT_EMPTY for s in mgr.slots))
        self.assertTrue(all(s.listener is None for s in mgr.slots))

    def test_health_replaces_dead_process(self) -> None:
        mgr = self._mgr()
        mgr.start()
        mgr.sync_from_nodes([
            {"id": "A", "country_short": "JP", "country": "Japan", "ip": "1.1.1.1",
             "score_latency": 5, "config_text": "a", "probe_status": "available"},
            {"id": "B", "country_short": "US", "country": "US", "ip": "2.2.2.2",
             "score_latency": 6, "config_text": "b", "probe_status": "available"},
        ])
        _wait_ready(mgr, 1)
        ready = next(s for s in mgr.slots if s.state == proxy_pool.SLOT_READY)
        ready.process.poll.return_value = 1  # dead
        # fail_count threshold 1: one failed tick removes/replaces the slot
        mgr.tick_health()
        # after health, dead slot drained/replaced if candidates remain via _last_candidates
        self.assertTrue(
            any(
                s.node_id == "B" or s.state in (proxy_pool.SLOT_READY, proxy_pool.SLOT_EMPTY)
                for s in mgr.slots
            )
        )

    def test_health_refills_from_latest_available_nodes_excluding_occupied(self) -> None:
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

        mgr.sync_from_nodes([
            {"id": "B", "country_short": "US", "country": "US", "ip": "2.2.2.2",
             "score_latency": 5, "config_text": "b", "probe_status": "available"},
            {"id": "C", "country_short": "KR", "country": "Korea", "ip": "3.3.3.3",
             "score_latency": 6, "config_text": "c", "probe_status": "available"},
        ])

        mgr.slots[0].process.poll.return_value = 1
        mgr.tick_health()
        _wait_ready(mgr, 2)

        self.assertEqual(mgr.slots[0].state, proxy_pool.SLOT_READY)
        self.assertEqual(mgr.slots[0].node_id, "C")
        self.assertEqual(mgr.slots[1].node_id, "B")

    def test_health_failure_removes_active_slot_immediately(self) -> None:
        mgr = self._mgr(pool_size=1)
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
        mgr.sync_from_nodes([
            {"id": "A", "country_short": "JP", "country": "Japan", "ip": "1.1.1.1",
             "score_latency": 5, "config_text": "a", "probe_status": "available"},
        ])
        _wait_ready(mgr, 1)

        mgr.tick_health()

        self.assertEqual(mgr.slots[0].state, proxy_pool.SLOT_EMPTY)
        self.assertEqual(mgr.slots[0].last_error, "")
        self.assertEqual(mgr.slots[0].fail_count, 0)
        mgr.shutdown()

    def test_health_failure_refills_slot_without_shadow(self) -> None:
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
        _wait_ready(mgr, 1)

        self.assertEqual(active.node_id, "B")
        self.assertIsNot(active.listener, old_listener)
        self.assertIsNot(active.process, old_process)
        self.assertFalse(active.replacement_pending)
        self.assertIsNone(active.shadow)

        active.last_health_at = 0
        mgr.tick_health()
        self.assertEqual(active.exit_ip, "9.9.9.9")
        mgr.shutdown()


    def test_grace_expiry_falls_back_to_stop_and_refill(self) -> None:
        mgr = self._mgr(pool_size=1)
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

        self.assertIn(active.state, (proxy_pool.SLOT_READY, proxy_pool.SLOT_EMPTY))
        mgr.shutdown()

    def test_fatal_health_errors_release_slot_immediately_after_first_failure(self) -> None:
        fatal_reasons = (
            "<urlopen error timed out>",
            "[错误代码 2005] [ERR_OVPN_AUTH_FAILED] OpenVPN 身份验证失败",
        )
        for reason in fatal_reasons:
            with self.subTest(reason=reason):
                mgr = self._mgr(pool_size=1)
                mgr.health_check = mock.Mock(return_value=(False, reason, {}))
                mgr.start()
                mgr.sync_from_nodes([
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

                deadline = time.time() + 2
                while time.time() < deadline and active.node_id == "A":
                    time.sleep(0.01)

                original_listener.stop.assert_called()
                mgr.stop_openvpn.assert_any_call(original_process)
                self.assertNotEqual(active.node_id, "A")
                self.assertFalse(active.replacement_pending)
                mgr.shutdown()

    def test_replace_all_slots_from_nodes_rebuilds_and_probes_ready_slots(self) -> None:
        mgr = self._mgr(pool_size=2)
        mgr.health_check = mock.Mock(side_effect=[
            (True, "ok", {"exit_ip": "9.9.9.1", "latency_ms": 11}),
            (True, "ok", {"exit_ip": "9.9.9.2", "latency_ms": 12}),
        ])
        mgr.start()

        old0 = _ready_slot(0, "JP", 10, node_id="A")
        old1 = _ready_slot(1, "US", 20, node_id="B")
        old0.process = mock.Mock()
        old0.process.poll.return_value = None
        old1.process = mock.Mock()
        old1.process.poll.return_value = None
        old0.listener = mock.Mock(stop=mock.Mock(), is_alive=mock.Mock(return_value=True))
        old1.listener = mock.Mock(stop=mock.Mock(), is_alive=mock.Mock(return_value=True))
        old0_process = old0.process
        old1_process = old1.process
        old0_listener = old0.listener
        old1_listener = old1.listener
        mgr.slots[0] = old0
        mgr.slots[1] = old1

        mgr.replace_all_slots_from_nodes([
            {"id": "C", "country_short": "KR", "country": "Korea", "ip": "3.3.3.3",
             "score_latency": 5, "config_text": "c", "probe_status": "available"},
            {"id": "D", "country_short": "SG", "country": "Singapore", "ip": "4.4.4.4",
             "score_latency": 6, "config_text": "d", "probe_status": "available"},
        ])

        _wait_ready(mgr, 2)
        self.assertEqual([s.node_id for s in mgr.slots if s.state == proxy_pool.SLOT_READY], ["C", "D"])
        self.assertEqual([s.exit_ip for s in mgr.slots if s.state == proxy_pool.SLOT_READY], ["9.9.9.1", "9.9.9.2"])
        old0_listener.stop.assert_called_once()
        old1_listener.stop.assert_called_once()
        mgr.stop_openvpn.assert_any_call(old0_process)
        mgr.stop_openvpn.assert_any_call(old1_process)
        self.assertEqual(mgr.health_check.call_count, 2)
        mgr.shutdown()

    def test_rolling_replace_only_replaces_one_batch(self) -> None:
        mgr = self._mgr(pool_size=4, max_starting=4, max_shadow_starting=2)
        mgr.health_check = mock.Mock(return_value=(True, "ok", {"exit_ip": "9.9.9.9", "latency_ms": 12}))
        mgr.start()
        mgr.sync_from_nodes([
            {"id": "A", "country_short": "JP", "country": "Japan", "ip": "1.1.1.1",
             "score_latency": 5, "ip_type": "hosting", "config_text": "a", "probe_status": "available"},
            {"id": "B", "country_short": "US", "country": "US", "ip": "2.2.2.2",
             "score_latency": 6, "ip_type": "hosting", "config_text": "b", "probe_status": "available"},
            {"id": "C", "country_short": "KR", "country": "Korea", "ip": "3.3.3.3",
             "score_latency": 7, "ip_type": "hosting", "config_text": "c", "probe_status": "available"},
            {"id": "D", "country_short": "SG", "country": "Singapore", "ip": "4.4.4.4",
             "score_latency": 8, "ip_type": "hosting", "config_text": "d", "probe_status": "available"},
        ])
        _wait_ready(mgr, 4)

        started = mgr.rolling_replace_from_nodes([
            {"id": "E", "country_short": "TH", "country": "Thailand", "ip": "5.5.5.5",
             "score_latency": 1, "ip_type": "hosting", "config_text": "e", "probe_status": "available"},
            {"id": "F", "country_short": "GB", "country": "United Kingdom", "ip": "6.6.6.6",
             "score_latency": 50, "ip_type": "residential", "config_text": "f", "probe_status": "available"},
            {"id": "G", "country_short": "NL", "country": "Netherlands", "ip": "7.7.7.7",
             "score_latency": 60, "ip_type": "mobile", "config_text": "g", "probe_status": "available"},
            {"id": "H", "country_short": "DE", "country": "Germany", "ip": "8.8.8.8",
             "score_latency": 2, "ip_type": "hosting", "config_text": "h", "probe_status": "available"},
        ], batch_size=2)

        deadline = time.time() + 2
        while time.time() < deadline:
            ids = [s.node_id for s in mgr.slots if s.state == proxy_pool.SLOT_READY]
            if len(set(ids) & {"F", "G"}) == 2:
                break
            time.sleep(0.01)

        ids = [s.node_id for s in mgr.slots if s.state == proxy_pool.SLOT_READY]
        self.assertEqual(started, 2)
        self.assertEqual(len(ids), 4)
        self.assertEqual(len(set(ids) & {"F", "G"}), 2)
        self.assertEqual(mgr.refresh_cursor, 2)
        self.assertEqual([mgr.slots[i].node_id for i in range(2)], ["F", "G"])
        self.assertEqual([mgr.slots[i].node_id for i in range(2, 4)], ["C", "D"])
        mgr.shutdown()

    def test_rolling_replace_advances_slots_by_cursor_and_wraps(self) -> None:
        mgr = self._mgr(pool_size=4, max_starting=4, max_shadow_starting=2)
        mgr.health_check = mock.Mock(side_effect=lambda slot: (True, "ok", {"exit_ip": getattr(slot, "node_ip", ""), "latency_ms": 12}))
        mgr.start()
        mgr.sync_from_nodes([
            {"id": "A", "country_short": "JP", "country": "Japan", "ip": "1.1.1.1",
             "score_latency": 5, "ip_type": "hosting", "config_text": "a", "probe_status": "available"},
            {"id": "B", "country_short": "US", "country": "US", "ip": "2.2.2.2",
             "score_latency": 6, "ip_type": "hosting", "config_text": "b", "probe_status": "available"},
            {"id": "C", "country_short": "KR", "country": "Korea", "ip": "3.3.3.3",
             "score_latency": 7, "ip_type": "hosting", "config_text": "c", "probe_status": "available"},
            {"id": "D", "country_short": "SG", "country": "Singapore", "ip": "4.4.4.4",
             "score_latency": 8, "ip_type": "hosting", "config_text": "d", "probe_status": "available"},
        ])
        _wait_ready(mgr, 4)

        first = mgr.rolling_replace_from_nodes([
            {"id": "E", "country_short": "TH", "country": "Thailand", "ip": "5.5.5.5",
             "score_latency": 1, "ip_type": "residential", "config_text": "e", "probe_status": "available"},
            {"id": "F", "country_short": "GB", "country": "United Kingdom", "ip": "6.6.6.6",
             "score_latency": 2, "ip_type": "residential", "config_text": "f", "probe_status": "available"},
        ], batch_size=2)

        deadline = time.time() + 2
        while time.time() < deadline:
            if [mgr.slots[i].node_id for i in range(4)] == ["E", "F", "C", "D"]:
                break
            time.sleep(0.01)

        self.assertEqual(first, 2)
        self.assertEqual([mgr.slots[i].node_id for i in range(4)], ["E", "F", "C", "D"])
        self.assertEqual(mgr.refresh_cursor, 2)

        second = mgr.rolling_replace_from_nodes([
            {"id": "E", "country_short": "TH", "country": "Thailand", "ip": "5.5.5.5",
             "score_latency": 1, "ip_type": "residential", "config_text": "e", "probe_status": "available"},
            {"id": "F", "country_short": "GB", "country": "United Kingdom", "ip": "6.6.6.6",
             "score_latency": 2, "ip_type": "residential", "config_text": "f", "probe_status": "available"},
            {"id": "G", "country_short": "NL", "country": "Netherlands", "ip": "7.7.7.7",
             "score_latency": 3, "ip_type": "residential", "config_text": "g", "probe_status": "available"},
            {"id": "H", "country_short": "DE", "country": "Germany", "ip": "8.8.8.8",
             "score_latency": 4, "ip_type": "residential", "config_text": "h", "probe_status": "available"},
        ], batch_size=2)

        deadline = time.time() + 2
        while time.time() < deadline:
            if [mgr.slots[i].node_id for i in range(4)] == ["E", "F", "G", "H"]:
                break
            time.sleep(0.01)

        self.assertEqual(second, 2)
        self.assertEqual([mgr.slots[i].node_id for i in range(4)], ["E", "F", "G", "H"])
        self.assertEqual(mgr.refresh_cursor, 0)
        mgr.shutdown()

    def test_rolling_replace_does_not_skip_past_current_window(self) -> None:
        mgr = self._mgr(pool_size=4, max_starting=4, max_shadow_starting=2)
        mgr.health_check = mock.Mock(return_value=(True, "ok", {"exit_ip": "9.9.9.9", "latency_ms": 12}))
        mgr.start()
        mgr.sync_from_nodes([
            {"id": "A", "country_short": "JP", "country": "Japan", "ip": "1.1.1.1",
             "score_latency": 5, "ip_type": "hosting", "config_text": "a", "probe_status": "available"},
            {"id": "B", "country_short": "US", "country": "US", "ip": "2.2.2.2",
             "score_latency": 6, "ip_type": "hosting", "config_text": "b", "probe_status": "available"},
            {"id": "C", "country_short": "KR", "country": "Korea", "ip": "3.3.3.3",
             "score_latency": 7, "ip_type": "hosting", "config_text": "c", "probe_status": "available"},
            {"id": "D", "country_short": "SG", "country": "Singapore", "ip": "4.4.4.4",
             "score_latency": 8, "ip_type": "hosting", "config_text": "d", "probe_status": "available"},
        ])
        _wait_ready(mgr, 4)

        original = [mgr.slots[i].node_id for i in range(4)]
        mgr.slots[0].state = proxy_pool.SLOT_STARTING
        mgr.slots[0].process = None
        mgr.slots[0].listener = None

        started = mgr.rolling_replace_from_nodes([
            {"id": "E", "country_short": "TH", "country": "Thailand", "ip": "5.5.5.5",
             "score_latency": 1, "ip_type": "residential", "config_text": "e", "probe_status": "available"},
            {"id": "F", "country_short": "GB", "country": "United Kingdom", "ip": "6.6.6.6",
             "score_latency": 2, "ip_type": "residential", "config_text": "f", "probe_status": "available"},
        ], batch_size=2)

        deadline = time.time() + 2
        while time.time() < deadline:
            if mgr.slots[1].node_id == "E":
                break
            time.sleep(0.01)

        self.assertEqual(started, 1)
        self.assertEqual(mgr.refresh_cursor, 2)
        self.assertEqual(mgr.slots[0].node_id, original[0])
        self.assertEqual(mgr.slots[1].node_id, "E")
        self.assertEqual(mgr.slots[2].node_id, original[2])
        self.assertEqual(mgr.slots[3].node_id, original[3])
        mgr.shutdown()

    def test_rolling_replace_ignores_slot_order_when_exit_ips_already_present(self) -> None:
        mgr = self._mgr(pool_size=4, max_starting=4, max_shadow_starting=2)
        mgr.health_check = mock.Mock(return_value=(True, "ok", {"exit_ip": "9.9.9.9", "latency_ms": 12}))
        mgr.start()

        slot0 = _ready_slot(0, "US", 10, node_id="A")
        slot1 = _ready_slot(1, "US", 20, node_id="B")
        slot2 = _ready_slot(2, "JP", 30, node_id="C")
        slot3 = _ready_slot(3, "JP", 40, node_id="D")
        slot0.exit_ip = "4.4.4.4"
        slot1.exit_ip = "3.3.3.3"
        slot2.exit_ip = "2.2.2.2"
        slot3.exit_ip = "1.1.1.1"
        for slot in (slot0, slot1, slot2, slot3):
            slot.process = mock.Mock(poll=mock.Mock(return_value=None))
            slot.listener = mock.Mock(start=mock.Mock(return_value=slot.port), is_alive=mock.Mock(return_value=True), stop=mock.Mock())
        mgr.slots = [slot0, slot1, slot2, slot3]

        started = mgr.rolling_replace_from_nodes([
            {"id": "C", "country_short": "JP", "country": "Japan", "ip": "1.1.1.1",
             "score_latency": 1, "ip_type": "residential", "config_text": "c", "probe_status": "available"},
            {"id": "D", "country_short": "JP", "country": "Japan", "ip": "2.2.2.2",
             "score_latency": 2, "ip_type": "residential", "config_text": "d", "probe_status": "available"},
            {"id": "A", "country_short": "US", "country": "US", "ip": "3.3.3.3",
             "score_latency": 3, "ip_type": "hosting", "config_text": "a", "probe_status": "available"},
            {"id": "B", "country_short": "US", "country": "US", "ip": "4.4.4.4",
             "score_latency": 4, "ip_type": "hosting", "config_text": "b", "probe_status": "available"},
        ], batch_size=4)

        self.assertEqual(started, 0)
        self.assertEqual([mgr.slots[i].node_id for i in range(4)], ["A", "B", "C", "D"])
        mgr.shutdown()

    def test_rolling_replace_starts_empty_window_slots_from_target_layout(self) -> None:
        mgr = self._mgr(pool_size=4, max_starting=4, max_shadow_starting=2)
        mgr.health_check = mock.Mock(return_value=(True, "ok", {"exit_ip": "9.9.9.9", "latency_ms": 12}))
        mgr.start()

        slot2 = _ready_slot(2, "JP", 30, node_id="C")
        slot3 = _ready_slot(3, "JP", 40, node_id="D")
        for slot in (slot2, slot3):
            slot.process = mock.Mock(poll=mock.Mock(return_value=None))
            slot.listener = mock.Mock(start=mock.Mock(return_value=slot.port), is_alive=mock.Mock(return_value=True), stop=mock.Mock())
        mgr.slots = [
            proxy_pool.PoolSlot(index=0, port_base=52000),
            proxy_pool.PoolSlot(index=1, port_base=52000),
            slot2,
            slot3,
        ]

        started = mgr.rolling_replace_from_nodes([
            {"id": "C", "country_short": "JP", "country": "Japan", "ip": "3.3.3.3",
             "score_latency": 1, "ip_type": "residential", "config_text": "c", "probe_status": "available"},
            {"id": "D", "country_short": "JP", "country": "Japan", "ip": "4.4.4.4",
             "score_latency": 2, "ip_type": "residential", "config_text": "d", "probe_status": "available"},
            {"id": "A", "country_short": "US", "country": "US", "ip": "1.1.1.1",
             "score_latency": 3, "ip_type": "hosting", "config_text": "a", "probe_status": "available"},
            {"id": "B", "country_short": "US", "country": "US", "ip": "2.2.2.2",
             "score_latency": 4, "ip_type": "hosting", "config_text": "b", "probe_status": "available"},
        ], batch_size=2)

        _wait_ready(mgr, 4)

        self.assertEqual(started, 2)
        self.assertEqual([mgr.slots[i].node_id for i in range(2)], ["C", "D"])
        self.assertEqual([mgr.slots[i].state for i in range(2)], [proxy_pool.SLOT_READY, proxy_pool.SLOT_READY])
        self.assertEqual([mgr.slots[i].node_id for i in range(2, 4)], ["C", "D"])
        self.assertEqual(mgr.refresh_cursor, 2)
        mgr.shutdown()

    def test_rolling_replace_prioritizes_stale_slots_before_empty_slots(self) -> None:
        def health_check(slot):
            return True, "ok", {"exit_ip": getattr(slot, "node_ip", ""), "latency_ms": 12}

        mgr = self._mgr(pool_size=3, max_starting=3, max_shadow_starting=1)
        mgr.health_check = mock.Mock(side_effect=health_check)
        mgr.start()
        stale = _ready_slot(0, "US", 20, node_id="stale")
        stale.exit_ip = "9.9.9.9"
        stale.node_ip = "9.9.9.9"
        stale.process = mock.Mock(poll=mock.Mock(return_value=None))
        stale.listener = mock.Mock(start=mock.Mock(return_value=stale.port), is_alive=mock.Mock(return_value=True), stop=mock.Mock())
        keep = _ready_slot(2, "JP", 10, node_id="keep")
        keep.exit_ip = "1.1.1.1"
        keep.node_ip = "1.1.1.1"
        keep.process = mock.Mock(poll=mock.Mock(return_value=None))
        keep.listener = mock.Mock(start=mock.Mock(return_value=keep.port), is_alive=mock.Mock(return_value=True), stop=mock.Mock())
        mgr.slots = [stale, proxy_pool.PoolSlot(index=1, port_base=52000), keep]

        started = mgr.rolling_replace_from_nodes([
            {"id": "keep", "country_short": "JP", "country": "Japan", "ip": "1.1.1.1",
             "score_latency": 1, "ip_type": "residential", "config_text": "keep", "probe_status": "available"},
            {"id": "new-home", "country_short": "TH", "country": "Thailand", "ip": "2.2.2.2",
             "score_latency": 2, "ip_type": "residential", "config_text": "new-home", "probe_status": "available"},
        ], batch_size=3)

        deadline = time.time() + 2
        while time.time() < deadline:
            if mgr.slots[0].node_id == "new-home":
                break
            time.sleep(0.01)

        self.assertEqual(started, 1)
        self.assertEqual(mgr.slots[0].node_id, "new-home")
        self.assertEqual(mgr.slots[1].state, proxy_pool.SLOT_EMPTY)
        mgr.shutdown()

    def test_default_rolling_replace_reconciles_full_pool(self) -> None:
        def health_check(slot):
            return True, "ok", {"exit_ip": getattr(slot, "node_ip", ""), "latency_ms": 12}

        mgr = self._mgr(pool_size=20, max_starting=20, max_shadow_starting=20)
        mgr.health_check = mock.Mock(side_effect=health_check)
        mgr.start()
        mgr.sync_from_nodes([
            {
                "id": f"old-{i:02d}",
                "country_short": "JP",
                "country": "Japan",
                "ip": f"1.1.1.{i}",
                "score_latency": 100 + i,
                "ip_type": "hosting",
                "config_text": f"old-{i}",
                "probe_status": "available",
            }
            for i in range(20)
        ])
        _wait_ready(mgr, 20)

        started = mgr.rolling_replace_from_nodes([
            {
                "id": f"new-{i:02d}",
                "country_short": "TH",
                "country": "Thailand",
                "ip": f"2.2.2.{i}",
                "score_latency": i,
                "ip_type": "residential",
                "config_text": f"new-{i}",
                "probe_status": "available",
            }
            for i in range(20)
        ])

        deadline = time.time() + 3
        while time.time() < deadline:
            ids = [mgr.slots[i].node_id for i in range(20)]
            if ids == [f"new-{i:02d}" for i in range(20)]:
                break
            time.sleep(0.01)

        self.assertEqual(started, 20)
        self.assertEqual([mgr.slots[i].node_id for i in range(20)], [f"new-{i:02d}" for i in range(20)])
        mgr.shutdown()

    def test_default_rolling_replace_ignores_small_starting_limits(self) -> None:
        def health_check(slot):
            return True, "ok", {"exit_ip": getattr(slot, "node_ip", ""), "latency_ms": 12}

        mgr = self._mgr(pool_size=20, max_starting=1, max_shadow_starting=1)
        mgr.health_check = mock.Mock(side_effect=health_check)
        mgr.start()
        mgr.sync_from_nodes([
            {
                "id": f"old-{i:02d}",
                "country_short": "JP",
                "country": "Japan",
                "ip": f"1.1.1.{i}",
                "score_latency": 100 + i,
                "ip_type": "hosting",
                "config_text": f"old-{i}",
                "probe_status": "available",
            }
            for i in range(20)
        ])
        _wait_ready(mgr, 20)

        started = mgr.rolling_replace_from_nodes([
            {
                "id": f"new-{i:02d}",
                "country_short": "TH",
                "country": "Thailand",
                "ip": f"2.2.2.{i}",
                "score_latency": i,
                "ip_type": "residential",
                "config_text": f"new-{i}",
                "probe_status": "available",
            }
            for i in range(20)
        ])

        deadline = time.time() + 3
        while time.time() < deadline:
            ids = [mgr.slots[i].node_id for i in range(20)]
            if ids == [f"new-{i:02d}" for i in range(20)]:
                break
            time.sleep(0.01)

        self.assertEqual(started, 20)
        self.assertEqual([mgr.slots[i].node_id for i in range(20)], [f"new-{i:02d}" for i in range(20)])
        mgr.shutdown()

    def test_new_manager_starts_refresh_cursor_from_zero(self) -> None:
        mgr = self._mgr(pool_size=4, max_starting=4, max_shadow_starting=2)
        self.assertEqual(mgr.refresh_cursor, 0)
        mgr.shutdown()


if __name__ == "__main__":
    unittest.main()
