import socket
import unittest

from core.proxy_prober import probe_tcp_port, scan_available_proxies


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


if __name__ == "__main__":
    unittest.main()
