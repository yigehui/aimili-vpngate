#!/usr/bin/env python3
from __future__ import annotations

import socket
import threading
import time
import unittest
from unittest import mock

import proxy_server


class ProxyListenerTests(unittest.TestCase):
    def test_create_proxy_listener_requires_credentials_when_forced(self) -> None:
        listener = proxy_server.create_proxy_listener(
            "127.0.0.1",
            0,
            username="u1",
            password="p1",
            bind_device=None,
            max_connections=8,
            require_auth=True,
        )
        self.assertTrue(listener.auth_enabled)
        self.assertEqual(listener.username, "u1")
        self.assertEqual(listener.password, "p1")

    def test_listener_start_stop_binds_port(self) -> None:
        listener = proxy_server.create_proxy_listener(
            "127.0.0.1",
            0,
            username="u1",
            password="p1",
            bind_device=None,
            max_connections=4,
            require_auth=True,
        )
        port = listener.start()
        self.assertGreater(port, 0)
        self.assertTrue(listener.is_alive())
        with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
            sock.settimeout(2)
            # unauthenticated HTTP CONNECT-ish probe: expect 407 when auth required
            sock.sendall(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
            data = sock.recv(4096)
            self.assertIn(b"407", data)
        listener.stop()
        self.assertFalse(listener.is_alive())

    def test_create_connection_uses_bind_device(self) -> None:
        with mock.patch("proxy_server.resolve_dns_over_device", return_value="1.2.3.4"):
            with mock.patch("socket.socket") as sock_cls:
                sock = mock.MagicMock()
                sock_cls.return_value = sock
                sock.connect.return_value = None
                with mock.patch("socket.getaddrinfo", return_value=[
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 443))
                ]):
                    proxy_server.create_connection(
                        ("example.com", 443),
                        timeout=2,
                        bind_device="tun7",
                    )
                sock.setsockopt.assert_any_call(
                    socket.SOL_SOCKET,
                    getattr(socket, "SO_BINDTODEVICE", proxy_server.SO_BINDTODEVICE),
                    b"tun7",
                )


if __name__ == "__main__":
    unittest.main()
