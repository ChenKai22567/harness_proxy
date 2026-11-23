import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

CODEX_PROBE_URL = "https://chatgpt.com/cdn-cgi/trace"
CONTROL_PROBE_URL = "https://www.google.com/generate_204"
CODEX_PROBE_TIMEOUT_MS = 12_000
CONTROL_PROBE_TIMEOUT_MS = 5_000

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


@dataclass(frozen=True)
class ProxyProbeStage:
    name: str
    ok: bool
    elapsed_ms: float
    message: str
    status_code: Optional[int] = None


@dataclass(frozen=True)
class HttpsProxyResult:
    url: str
    stages: Tuple[ProxyProbeStage, ...]

    @property
    def received_http_response(self) -> bool:
        last = self.stages[-1]
        return last.name == "http" and last.ok


def _remaining_seconds(deadline: float) -> float:
    remaining = deadline - time.perf_counter()
    if remaining <= 0:
        raise TimeoutError
    return remaining


def _read_http_status(channel: socket.socket, deadline: float) -> int:
    """Read through the final response headers under one time and size limit."""
    pending = bytearray()
    received = 0
    max_header_bytes = 64 * 1024
    while True:
        header_end = pending.find(b"\r\n\r\n")
        if header_end < 0:
            if received >= max_header_bytes:
                raise OSError("HTTP 响应头过大")
            channel.settimeout(_remaining_seconds(deadline))
            chunk = channel.recv(min(4096, max_header_bytes - received))
            if not chunk:
                raise OSError("收到完整 HTTP 响应头前连接已关闭")
            pending.extend(chunk)
            received += len(chunk)
            continue

        status_line = pending[:header_end].split(b"\r\n", 1)[0]
        parts = status_line.split(None, 2)
        if (
            len(parts) < 2
            or parts[0] not in (b"HTTP/1.0", b"HTTP/1.1")
            or len(parts[1]) != 3
            or not parts[1].isdigit()
            or not 100 <= int(parts[1]) <= 599
        ):
            raise OSError("收到无效的 HTTP 状态行")
        status = int(parts[1])
        if status >= 200 or status == 101:
            return status

        del pending[:header_end + 4]  # Skip informational responses.


def probe_https_via_proxy(
    host: str,
    port: int,
    timeout_ms: int = CODEX_PROBE_TIMEOUT_MS,
    url: str = CODEX_PROBE_URL,
) -> HttpsProxyResult:
    """Check CONNECT, verified TLS and the first HTTP response via this proxy."""
    target = urlsplit(url)
    if target.scheme != "https" or not target.hostname or target.username is not None:
        raise ValueError("预检目标必须是无凭据的 HTTPS 地址")

    target_host = target.hostname.encode("idna").decode("ascii")
    authority = f"[{target_host}]" if ":" in target_host else target_host
    tunnel_target = f"{authority}:{target.port or 443}"
    host_header = tunnel_target if target.port else authority
    path = target.path or "/"
    if target.query:
        path += f"?{target.query}"

    started = time.perf_counter()
    deadline = started + timeout_ms / 1000.0
    stage = "tcp"
    stages = []

    def record(ok: bool, message: str, status_code: Optional[int] = None):
        elapsed = round((time.perf_counter() - started) * 1000.0, 1)
        stages.append(ProxyProbeStage(stage, ok, elapsed, message, status_code))

    try:
        with socket.create_connection((host, port), _remaining_seconds(deadline)) as proxy:
            record(True, "已连接代理端口")
            stage, started = "connect", time.perf_counter()
            proxy.settimeout(_remaining_seconds(deadline))
            proxy.sendall(
                f"CONNECT {tunnel_target} HTTP/1.1\r\nHost: {tunnel_target}\r\n\r\n".encode("ascii")
            )
            status = _read_http_status(proxy, deadline)
            if not 200 <= status < 300:
                reason = "代理需要身份验证" if status == 407 else "代理未建立隧道"
                record(False, f"{reason}（HTTP {status}）", status)
                return HttpsProxyResult(url, tuple(stages))
            record(True, f"隧道已建立（HTTP {status}）", status)

            stage, started = "tls", time.perf_counter()
            context = ssl.create_default_context()
            proxy.settimeout(_remaining_seconds(deadline))
            with context.wrap_socket(proxy, server_hostname=target_host) as tls:
                record(True, f"证书验证通过（{tls.version()}）")
                stage, started = "http", time.perf_counter()
                tls.settimeout(_remaining_seconds(deadline))
                tls.sendall(
                    (
                        f"GET {path} HTTP/1.1\r\nHost: {host_header}\r\n"
                        "User-Agent: HarnessProxyLauncher/2.0\r\nConnection: close\r\n\r\n"
                    ).encode("ascii")
                )
                status = _read_http_status(tls, deadline)
                record(True, f"站点已响应（HTTP {status}）", status)
    except TimeoutError:
        record(False, "超时")
    except ssl.SSLCertVerificationError as error:
        record(False, f"证书验证失败（{error.verify_message}）")
    except OSError as error:
        record(False, str(error))

    return HttpsProxyResult(url, tuple(stages))

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
