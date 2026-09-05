"""Single-instance guard and the loopback wake-up channel.

The Windows named mutex is the authoritative single-instance boundary; the
``127.0.0.1`` TCP listener is only a convenience channel so a second launch
can bring the first window back instead of silently exiting.  Both sides
share this module so the port and mutex name can never drift apart.
"""

import ctypes
import socket
import threading
import time
from ctypes import wintypes
from typing import Callable, Optional

SINGLE_INSTANCE_PORT = 47891
SINGLE_INSTANCE_MUTEX = "Local\\HarnessProxyLauncher.SingleInstance"


class SingleInstanceGuard:
    """Process-lifetime Windows mutex that cannot disappear with a UI thread."""

    ERROR_ALREADY_EXISTS = 183

    def __init__(self, mutex_name: str = SINGLE_INSTANCE_MUTEX):
        self._mutex_name = mutex_name
        self._handle = None

    def acquire(self) -> bool:
        # Windows-only by design: the launcher depends on winreg,
        # ctypes.windll and the win32 tray backend throughout.
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.SetLastError(0)
        handle = kernel32.CreateMutexW(None, False, self._mutex_name)
        if not handle:
            # Do not make an OS API failure prevent the application from opening;
            # the loopback wake-up channel remains a secondary guard.
            return True

        if kernel32.GetLastError() == self.ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False

        self._handle = handle
        return True

    def close(self):
        if self._handle:
            try:
                ctypes.windll.kernel32.CloseHandle(self._handle)
            finally:
                self._handle = None


def notify_existing_instance(attempts: int = 8) -> bool:
    """Wake the existing window without making a second launch feel hung.

    A very early second click can arrive while the first process owns the mutex
    but has not bound its loopback listener yet.  Short bounded retries cover
    that startup race without ever blocking Explorer for several seconds.
    """
    for attempt in range(max(1, attempts)):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.15)
                s.connect(('127.0.0.1', SINGLE_INSTANCE_PORT))
                s.sendall(b'WAKEUP\n')
                return True
        except OSError:
            if attempt + 1 < attempts:
                time.sleep(0.08)
    return False


def start_wakeup_server(
    is_running: Callable[[], bool],
    on_wakeup: Callable[[], None],
    on_bind_failed: Optional[Callable[[str], None]] = None,
):
    """Listen on the loopback wake-up port in a daemon thread.

    ``on_bind_failed`` keeps a port collision observable instead of silently
    disabling the second-instance wake-up feature.
    """

    def _server_loop():
        try:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(('127.0.0.1', SINGLE_INSTANCE_PORT))
            server.listen(5)
            server.settimeout(1.0)
            while is_running():
                try:
                    conn, _ = server.accept()
                    data = conn.recv(1024)
                    conn.close()
                    if b'WAKEUP' in data:
                        on_wakeup()
                except socket.timeout:
                    continue
                except OSError:
                    break
        except OSError as exc:
            if on_bind_failed is not None:
                try:
                    on_bind_failed(
                        f"单实例唤醒端口 {SINGLE_INSTANCE_PORT} 绑定失败，"
                        f"第二实例将无法唤起主窗口: {exc}"
                    )
                except Exception:
                    pass
        except Exception:
            pass

    threading.Thread(target=_server_loop, daemon=True, name="single-instance-ipc").start()
