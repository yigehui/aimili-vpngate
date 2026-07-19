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
