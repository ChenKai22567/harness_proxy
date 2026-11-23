import os
import socket
import ssl
import unittest
from unittest import mock

from core.launcher_engine import LauncherEngine
from core.proxy_prober import (
    CODEX_PROBE_URL,
    CONTROL_PROBE_URL,
    HttpsProxyResult,
    ProxyProbeStage,
    probe_https_via_proxy,
    probe_tcp_port,
    scan_available_proxies,
)


def _listen_on_random_port():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    return server


class TcpProbeTests(unittest.TestCase):
    def test_listening_port_reports_success_with_latency(self):
        server = _listen_on_random_port()
        try:
            reachable, latency, message = probe_tcp_port(
                "127.0.0.1", server.getsockname()[1], timeout_ms=500
            )
        finally:
            server.close()
        self.assertTrue(reachable)
        self.assertGreaterEqual(latency, 0.0)
        self.assertEqual(message, "端口连接成功")

    def test_closed_port_reports_failure(self):
        server = _listen_on_random_port()
        port = server.getsockname()[1]
        server.close()
        reachable, latency, message = probe_tcp_port("127.0.0.1", port, timeout_ms=500)
        self.assertFalse(reachable)
        self.assertEqual(latency, 0.0)
        self.assertTrue(message)


class ScanTests(unittest.TestCase):
    def test_scan_returns_only_listening_ports(self):
        first = _listen_on_random_port()
        second = _listen_on_random_port()
        closed = _listen_on_random_port()
        closed_port = closed.getsockname()[1]
        closed.close()
        try:
            ports = [
                {"name": "first", "port": first.getsockname()[1]},
                {"name": "second", "port": second.getsockname()[1]},
                {"name": "closed", "port": closed_port},
            ]
            results = scan_available_proxies("127.0.0.1", ports)
        finally:
            first.close()
            second.close()
        self.assertEqual({item["name"] for item in results}, {"first", "second"})
        for item in results:
            self.assertGreaterEqual(item["latency_ms"], 0.0)


class HttpsProbeTests(unittest.TestCase):
    def setUp(self):
        self.clock = 100.0
        self.open_socket = self.enterContext(mock.patch("core.proxy_prober.socket.create_connection"))
        self.create_context = self.enterContext(mock.patch("core.proxy_prober.ssl.create_default_context"))
        self.enterContext(mock.patch("core.proxy_prober.time.perf_counter", side_effect=lambda: self.clock))
        self.proxy = self.open_socket.return_value.__enter__.return_value
        self.proxy.recv.return_value = b"HTTP/1.1 200 Connection Established\r\n\r\n"
        self.wrap_socket = self.create_context.return_value.wrap_socket
        self.tls = self.wrap_socket.return_value.__enter__.return_value
        self.tls.version.return_value = "TLSv1.3"

    def test_slow_tls_uses_the_budget_for_the_whole_explicit_route(self):
        tls_context = self.wrap_socket.return_value

        def slow_handshake(*_args, **_kwargs):
            self.clock += 5.7
            return tls_context

        self.wrap_socket.side_effect = slow_handshake
        self.tls.recv.return_value = b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n"
        with mock.patch.dict(os.environ, {"NO_PROXY": "*"}):
            result = probe_https_via_proxy("127.0.0.1", 7890)

        self.open_socket.assert_called_once_with(("127.0.0.1", 7890), 12.0)
        self.assertIn(b"CONNECT chatgpt.com:443 HTTP/1.1", self.proxy.sendall.call_args.args[0])
        self.wrap_socket.assert_called_once_with(self.proxy, server_hostname="chatgpt.com")
        self.assertAlmostEqual(self.tls.settimeout.call_args.args[0], 6.3)
        self.assertEqual([s.name for s in result.stages], ["tcp", "connect", "tls", "http"])
        self.assertEqual(result.stages[2].elapsed_ms, 5700)
        self.assertTrue(result.received_http_response)
        self.assertEqual(result.stages[-1].status_code, 403)

    def test_proxy_authentication_response_stops_at_connect(self):
        self.proxy.recv.return_value = b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n"
        result = probe_https_via_proxy("127.0.0.1", 7890)
        self.assertFalse(result.received_http_response)
        self.assertEqual(result.stages[-1].name, "connect")
        self.assertEqual(result.stages[-1].status_code, 407)
        self.create_context.assert_not_called()

    def test_certificate_error_is_reported_at_tls(self):
        error = ssl.SSLCertVerificationError(1, "certificate has expired")
        error.verify_message = "certificate has expired"
        self.wrap_socket.side_effect = error
        result = probe_https_via_proxy("127.0.0.1", 7890)
        self.assertFalse(result.received_http_response)
        self.assertEqual(result.stages[-1].name, "tls")
        self.assertIn("证书验证失败", result.stages[-1].message)

    def test_incomplete_headers_cannot_extend_the_budget(self):
        def slow_chunk(_size):
            self.clock += 4
            return b"x"

        self.proxy.recv.side_effect = slow_chunk
        result = probe_https_via_proxy("127.0.0.1", 7890)
        self.assertEqual(self.proxy.recv.call_count, 3)
        self.assertFalse(result.received_http_response)
        self.assertEqual(result.stages[-1], ProxyProbeStage("connect", False, 12000, "超时"))
        self.create_context.assert_not_called()


class CodexProbeTests(unittest.TestCase):
    def test_failed_target_checks_control_with_same_proxy(self):
        engine = LauncherEngine.__new__(LauncherEngine)
        engine.preflight_proxy = mock.Mock(
            return_value={"reachable": True, "host": "127.0.0.1", "port": 7890},
        )
        engine.preflight_codex = mock.Mock(return_value={"running": False})
        failing = HttpsProxyResult(CODEX_PROBE_URL, (ProxyProbeStage("tls", False, 12000, "超时"),))
        control = HttpsProxyResult(CONTROL_PROBE_URL, (ProxyProbeStage("http", True, 100, "HTTP 204", 204),))
        with mock.patch("core.launcher_engine.probe_https_via_proxy", side_effect=[failing, control]) as probe:
            result = engine.diagnose_codex_proxy()

        self.assertEqual(probe.call_args_list, [
            mock.call("127.0.0.1", 7890, timeout_ms=12000),
            mock.call("127.0.0.1", 7890, timeout_ms=5000, url=CONTROL_PROBE_URL),
        ])
        self.assertIs(result["primary"], failing)
        self.assertIs(result["control"], control)
        self.assertEqual(result["process"], {"running": False})


if __name__ == "__main__":
    unittest.main()
