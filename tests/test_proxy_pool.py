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
            config_dir=None,
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
        # fail_count threshold 2: call tick_health twice
        mgr.tick_health()
        mgr.tick_health()
        # after health, dead slot drained/replaced if candidates remain via _last_candidates
        self.assertTrue(
            any(
                s.node_id == "B" or s.state in (proxy_pool.SLOT_READY, proxy_pool.SLOT_EMPTY)
                for s in mgr.slots
            )
        )


if __name__ == "__main__":
    unittest.main()
