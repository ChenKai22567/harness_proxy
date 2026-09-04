import socket
import time
import urllib.error
import urllib.request
from typing import Tuple, List, Dict, Any
from concurrent.futures import ThreadPoolExecutor

COMMON_PORTS = [
    {"name": "Clash / Mihomo (7890)", "port": 7890},
    {"name": "Clash Verge (7897)", "port": 7897},
    {"name": "v2rayN (10808)", "port": 10808},
    {"name": "v2rayN HTTP (10809)", "port": 10809},
    {"name": "Sing-box (2080)", "port": 2080},
    {"name": "通用 HTTP (8080)", "port": 8080}
]

def probe_tcp_port(host: str, port: int, timeout_ms: int = 1500) -> Tuple[bool, float, str]:
    """
    Tests TCP connection to host:port.
    Returns: (is_reachable: bool, latency_ms: float, message: str)
    """
    timeout_sec = max(0.2, timeout_ms / 1000.0)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout_sec)

    start_time = time.perf_counter()
    try:
        sock.connect((host, port))
        elapsed_ms = (time.perf_counter() - start_time) * 1000.0
        return True, round(elapsed_ms, 1), "端口连接成功"
    except socket.timeout:
        return False, 0.0, "连接超时"
    except ConnectionRefusedError:
        return False, 0.0, "目标端口拒绝连接 (服务未开启)"
    except Exception as e:
        return False, 0.0, f"连接失败: {str(e)}"
    finally:
        sock.close()


def probe_https_via_proxy(
    host: str,
    port: int,
    timeout_ms: int = 5000,
    url: str = "https://chatgpt.com/cdn-cgi/trace",
) -> Tuple[bool, float, str]:
    """Verify that a listening local port is actually an outbound HTTPS proxy."""
    proxy_url = f"http://{host}:{int(port)}"
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    )
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "HarnessProxyLauncher/1.0"},
        method="GET",
    )
    start_time = time.perf_counter()
    try:
        with opener.open(request, timeout=max(0.5, timeout_ms / 1000.0)) as response:
            response.read(256)
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            status = int(getattr(response, "status", 0) or 0)
            return 200 <= status < 400, round(elapsed_ms, 1), f"HTTPS {status}"
    except urllib.error.HTTPError as error:
        elapsed_ms = (time.perf_counter() - start_time) * 1000.0
        # Any HTTP response proves the CONNECT/TLS route is working. Only a
        # transport failure means the local listener is not a usable proxy.
        return True, round(elapsed_ms, 1), f"HTTPS {error.code}"
    except Exception as error:
        return False, 0.0, f"HTTPS 出站失败: {error}"

def scan_available_proxies(host: str = "127.0.0.1", ports_to_scan: List[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """
    Concurrently scans common proxy ports.
    Returns list of reachable proxy descriptors with latencies.
    """
    targets = ports_to_scan or COMMON_PORTS
    results = []

    def _check(item):
        port = item["port"]
        reachable, latency, msg = probe_tcp_port(host, port, timeout_ms=1000)
        if reachable:
            return {
                "name": item.get("name", f"Port {port}"),
                "host": host,
                "port": port,
                "latency_ms": latency
            }
        return None

    with ThreadPoolExecutor(max_workers=6) as executor:
        for res in executor.map(_check, targets):
            if res is not None:
                results.append(res)

    results.sort(key=lambda x: x["latency_ms"])
    return results
