#!/usr/bin/env python3
from __future__ import annotations

import unittest
from unittest import mock

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


if __name__ == "__main__":
    unittest.main()
