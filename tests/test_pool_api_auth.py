#!/usr/bin/env python3
from __future__ import annotations

import tempfile
import unittest
from unittest import mock
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

    def test_extract_query_token(self) -> None:
        self.assertEqual(
            proxy_pool.extract_query_api_token({"token": ["tok-from-query"]}),
            "tok-from-query",
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

    def test_load_or_create_pool_secrets_ignores_deprecated_shadow_setting(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "pool_secrets.json"
            path.write_text(
                '{"api_token":"a","proxy_user":"u","proxy_pass":"p","max_shadow_starting":99}\n',
                encoding="utf-8",
            )
            cfg = proxy_pool.load_or_create_pool_config(path)
            self.assertNotIn("max_shadow_starting", cfg)

    def test_load_or_create_pool_secrets_reads_refresh_and_skip_settings(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "pool_secrets.json"
            with mock.patch.dict(
                "os.environ",
                {
                    "POOL_REFRESH_BATCH_SIZE": "17",
                    "POOL_FAILED_NODE_SKIP_SECONDS": "321",
                },
                clear=False,
            ):
                cfg = proxy_pool.load_or_create_pool_config(path)
        self.assertEqual(cfg["refresh_batch_size"], 17)
        self.assertEqual(cfg["failed_node_skip_seconds"], 321)


class PoolQueryParseTests(unittest.TestCase):
    def test_parse_pool_query_defaults_return_type_to_http(self) -> None:
        parsed = proxy_pool.parse_pool_query({})
        self.assertEqual(parsed["return_type"], "http")

    def test_parse_pool_query_rejects_invalid_return_type(self) -> None:
        with self.assertRaises(ValueError):
            proxy_pool.parse_pool_query({"return_type": ["ftp"]})


if __name__ == "__main__":
    unittest.main()
