#!/usr/bin/env python3
from __future__ import annotations

import tempfile
import unittest
from unittest import mock
from pathlib import Path

import vpngate_manager


def _row(ip: str, port: str = "443", proto: str = "tcp") -> dict[str, str]:
    return {
        "IP": ip,
        "Port": port,
        "Proto": proto,
        "CountryShort": "JP",
        "CountryLong": "Japan",
        "OpenVPN_ConfigData_Base64": "ZmFrZQ==",
    }


class VpnGateSourceDiscoveryTests(unittest.TestCase):
    def test_extract_mirror_api_urls_filters_external_and_deduplicates(self) -> None:
        html = """
        <a href="https://www.vpngate.net/en/">main</a>
        <a href="http://118.32.241.238:38392/en/">mirror-a</a>
        <a href="http://118.32.241.238:38392/en/">mirror-a-dup</a>
        <a href="http://150.40.105.5:32536/en/">mirror-b</a>
        <a href="http://www.softether.org/">external</a>
        """

        urls = vpngate_manager.extract_mirror_api_urls(html)

        self.assertEqual(
            urls,
            [
                "http://118.32.241.238:38392/api/iphone/",
                "http://150.40.105.5:32536/api/iphone/",
            ],
        )

    def test_get_candidate_api_urls_persists_discovered_mirrors(self) -> None:
        mirrors = ["http://118.32.241.238:38392/api/iphone/"]

        with (
            mock.patch.object(vpngate_manager, "MERGE_MIRROR_SOURCES", True),
            mock.patch.object(vpngate_manager, "MAX_MIRROR_SOURCES", 6),
            mock.patch.object(vpngate_manager, "discover_mirror_api_urls", return_value=mirrors),
            mock.patch.object(vpngate_manager, "save_cached_mirror_api_urls") as save_mock,
        ):
            urls = vpngate_manager.get_candidate_api_urls()

        self.assertEqual(urls, [vpngate_manager.API_URL, *mirrors])
        save_mock.assert_called_once_with(mirrors)

    def test_get_candidate_api_urls_falls_back_to_cached_then_default(self) -> None:
        cached = ["http://cached.example/api/iphone/"]
        with (
            mock.patch.object(vpngate_manager, "MERGE_MIRROR_SOURCES", True),
            mock.patch.object(vpngate_manager, "MAX_MIRROR_SOURCES", 6),
            mock.patch.object(vpngate_manager, "discover_mirror_api_urls", side_effect=TimeoutError("timeout")),
            mock.patch.object(vpngate_manager, "load_cached_mirror_api_urls", return_value=cached),
            mock.patch.object(vpngate_manager, "log_to_json"),
        ):
            urls = vpngate_manager.get_candidate_api_urls()
        self.assertEqual(urls, [vpngate_manager.API_URL, *cached])

        with (
            mock.patch.object(vpngate_manager, "MERGE_MIRROR_SOURCES", True),
            mock.patch.object(vpngate_manager, "MAX_MIRROR_SOURCES", 6),
            mock.patch.object(vpngate_manager, "discover_mirror_api_urls", side_effect=TimeoutError("timeout")),
            mock.patch.object(vpngate_manager, "load_cached_mirror_api_urls", return_value=[]),
            mock.patch.object(vpngate_manager, "log_to_json"),
        ):
            urls = vpngate_manager.get_candidate_api_urls()
        self.assertEqual(urls, [vpngate_manager.API_URL, *vpngate_manager.DEFAULT_MIRROR_API_URLS[:6]])


class VpnGateFetchCandidatesTests(unittest.TestCase):
    def test_fetch_candidates_merges_multiple_sources(self) -> None:
        source_urls = [
            "https://primary.example/api/iphone/",
            "http://mirror.example/api/iphone/",
        ]
        row_sets = {
            source_urls[0]: [_row("1.1.1.1", "443", "tcp")],
            source_urls[1]: [_row("2.2.2.2", "443", "tcp")],
        }

        def fake_fetch(url: str | None = None, use_ssl_verify: bool = True) -> str:
            return str(url)

        def fake_parse(api_text: str) -> list[dict[str, str]]:
            return list(row_sets.get(api_text, []))

        def fake_row_to_node(row: dict[str, str], config_text: str) -> dict[str, object]:
            ip = row["IP"]
            return {
                "id": f"JP_{ip}_443_tcp",
                "ip": ip,
                "country": "日本",
                "config_text": config_text,
            }

        with (
            mock.patch.object(vpngate_manager, "get_candidate_api_urls", return_value=source_urls, create=True),
            mock.patch.object(vpngate_manager, "cached_nodes", return_value=[]),
            mock.patch.object(vpngate_manager, "load_blacklist", return_value={}),
            mock.patch.object(vpngate_manager, "fetch_api_text", side_effect=fake_fetch),
            mock.patch.object(vpngate_manager, "parse_vpngate_rows", side_effect=fake_parse),
            mock.patch.object(vpngate_manager, "decode_config", return_value="config"),
            mock.patch.object(vpngate_manager, "row_to_node", side_effect=fake_row_to_node),
            mock.patch.object(vpngate_manager, "log_to_json"),
            mock.patch.object(vpngate_manager, "set_state"),
            mock.patch.object(vpngate_manager.time, "sleep"),
        ):
            nodes = vpngate_manager.fetch_candidates()

        self.assertEqual([node["ip"] for node in nodes], ["1.1.1.1", "2.2.2.2"])

    def test_fetch_candidates_keeps_same_ip_with_different_port_or_proto(self) -> None:
        source_urls = ["https://primary.example/api/iphone/"]
        row_sets = {
            source_urls[0]: [
                _row("1.1.1.1", "443", "tcp"),
                _row("1.1.1.1", "1195", "udp"),
                _row("1.1.1.1", "443", "tcp"),
            ],
        }

        def fake_fetch(url: str | None = None, use_ssl_verify: bool = True) -> str:
            return str(url)

        def fake_parse(api_text: str) -> list[dict[str, str]]:
            return list(row_sets.get(api_text, []))

        def fake_row_to_node(row: dict[str, str], config_text: str) -> dict[str, object]:
            ip = row["IP"]
            port = row["Port"]
            proto = row["Proto"]
            return {
                "id": f"JP_{ip}_{port}_{proto}",
                "ip": ip,
                "remote_port": int(port),
                "proto": proto,
                "country": "日本",
                "config_text": config_text,
            }

        with (
            mock.patch.object(vpngate_manager, "get_candidate_api_urls", return_value=source_urls, create=True),
            mock.patch.object(vpngate_manager, "cached_nodes", return_value=[]),
            mock.patch.object(vpngate_manager, "load_blacklist", return_value={}),
            mock.patch.object(vpngate_manager, "fetch_api_text", side_effect=fake_fetch),
            mock.patch.object(vpngate_manager, "parse_vpngate_rows", side_effect=fake_parse),
            mock.patch.object(vpngate_manager, "decode_config", return_value="config"),
            mock.patch.object(vpngate_manager, "row_to_node", side_effect=fake_row_to_node),
            mock.patch.object(vpngate_manager, "log_to_json"),
            mock.patch.object(vpngate_manager, "set_state"),
            mock.patch.object(vpngate_manager.time, "sleep"),
        ):
            nodes = vpngate_manager.fetch_candidates()

        self.assertEqual(
            [(node["ip"], node["remote_port"], node["proto"]) for node in nodes],
            [("1.1.1.1", 443, "tcp"), ("1.1.1.1", 1195, "udp")],
        )

    def test_fetch_candidates_does_not_retry_failed_http_source(self) -> None:
        source_urls = [
            "http://bad.example/api/iphone/",
            "http://good.example/api/iphone/",
        ]
        fetch_calls: list[str] = []

        def fake_fetch(url: str | None = None, use_ssl_verify: bool = True) -> str:
            fetch_calls.append(f"{url}|{use_ssl_verify}")
            if "bad.example" in str(url):
                raise RuntimeError("boom")
            return str(url)

        def fake_parse(api_text: str) -> list[dict[str, str]]:
            if "good.example" in api_text:
                return [_row("2.2.2.2", "443", "tcp")]
            return []

        def fake_row_to_node(row: dict[str, str], config_text: str) -> dict[str, object]:
            ip = row["IP"]
            return {
                "id": f"JP_{ip}_443_tcp",
                "ip": ip,
                "remote_port": 443,
                "proto": "tcp",
                "country": "日本",
                "config_text": config_text,
            }

        with (
            mock.patch.object(vpngate_manager, "get_candidate_api_urls", return_value=source_urls, create=True),
            mock.patch.object(vpngate_manager, "load_blacklist", return_value={}),
            mock.patch.object(vpngate_manager, "fetch_api_text", side_effect=fake_fetch),
            mock.patch.object(vpngate_manager, "parse_vpngate_rows", side_effect=fake_parse),
            mock.patch.object(vpngate_manager, "decode_config", return_value="config"),
            mock.patch.object(vpngate_manager, "row_to_node", side_effect=fake_row_to_node),
            mock.patch.object(vpngate_manager, "log_to_json"),
            mock.patch.object(vpngate_manager, "set_state"),
        ):
            nodes = vpngate_manager.fetch_candidates()

        self.assertEqual([node["ip"] for node in nodes], ["2.2.2.2"])
        self.assertEqual(
            fetch_calls,
            [
                "http://bad.example/api/iphone/|True",
                "http://good.example/api/iphone/|True",
            ],
        )


class VpnGateBatchProbeTests(unittest.TestCase):
    def _node(self, node_id: str, ip: str) -> dict[str, object]:
        return {
            "id": node_id,
            "ip": ip,
            "remote_host": ip,
            "remote_port": 443,
            "ping": 10,
            "config_text": "client",
            "probe_status": "not_checked",
            "probe_message": "",
            "active": False,
        }

    def test_test_multiple_nodes_syncs_available_results_to_pool(self) -> None:
        nodes = [
            self._node("node-1", "1.1.1.1"),
            self._node("node-2", "2.2.2.2"),
        ]

        class ImmediateFuture:
            def __init__(self, result: dict[str, object]) -> None:
                self._result = result

            def result(self) -> dict[str, object]:
                return self._result

        class ImmediateExecutor:
            def __init__(self, max_workers: int) -> None:
                self.max_workers = max_workers

            def __enter__(self) -> "ImmediateExecutor":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def submit(self, fn, arg):
                return ImmediateFuture(fn(arg))

        with tempfile.TemporaryDirectory() as td:
            nodes_file = Path(td) / "nodes.json"
            config_dir = Path(td) / "configs"
            vpngate_manager.write_json(nodes_file, nodes)
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
                mock.patch.object(
                    vpngate_manager,
                    "run_openvpn_until_ready",
                    side_effect=[(True, "ok", None), (False, "fail", None)],
                ),
                mock.patch.object(vpngate_manager.concurrent.futures, "ThreadPoolExecutor", ImmediateExecutor),
                mock.patch.object(vpngate_manager.concurrent.futures, "as_completed", side_effect=lambda futures: list(futures)),
            ):
                results = vpngate_manager.test_multiple_nodes(["node-1", "node-2"])

        self.assertEqual(len(results), 2)
        pool_manager.sync_from_nodes.assert_called()
        synced_nodes = pool_manager.sync_from_nodes.call_args_list[-1].args[0]
        self.assertEqual([node["id"] for node in synced_nodes], ["node-1"])

    def test_test_multiple_nodes_uses_configured_parallel_workers(self) -> None:
        nodes = [self._node(f"node-{i}", f"10.0.0.{i}") for i in range(1, 21)]
        created_workers: list[int] = []

        class ImmediateFuture:
            def __init__(self, result: dict[str, object]) -> None:
                self._result = result

            def result(self) -> dict[str, object]:
                return self._result

        class CapturingExecutor:
            def __init__(self, max_workers: int) -> None:
                created_workers.append(max_workers)

            def __enter__(self) -> "CapturingExecutor":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def submit(self, fn, arg):
                return ImmediateFuture(fn(arg))

        with tempfile.TemporaryDirectory() as td:
            nodes_file = Path(td) / "nodes.json"
            config_dir = Path(td) / "configs"
            vpngate_manager.write_json(nodes_file, nodes)

            with (
                mock.patch.object(vpngate_manager, "NODES_FILE", nodes_file),
                mock.patch.object(vpngate_manager, "CONFIG_DIR", config_dir),
                mock.patch.object(vpngate_manager, "NODE_TEST_MAX_WORKERS", 12, create=True),
                mock.patch.object(vpngate_manager.vpn_utils, "ping_latency_ms", return_value=9),
                mock.patch.object(vpngate_manager.vpn_utils, "enrich_ip_info"),
                mock.patch.object(vpngate_manager, "get_free_test_index", return_value=3),
                mock.patch.object(vpngate_manager, "release_test_index"),
                mock.patch.object(vpngate_manager, "run_openvpn_until_ready", return_value=(False, "fail", None)),
                mock.patch.object(vpngate_manager.concurrent.futures, "ThreadPoolExecutor", CapturingExecutor),
                mock.patch.object(vpngate_manager.concurrent.futures, "as_completed", side_effect=lambda futures: list(futures)),
            ):
                vpngate_manager.test_multiple_nodes([node["id"] for node in nodes])

        self.assertEqual(created_workers, [12])


if __name__ == "__main__":
    unittest.main()
