import os
import sys
import ctypes
import time
from ctypes import wintypes

# Enable High-DPI awareness on Windows for crisp fonts and borders
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2) # Per-monitor DPI aware
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# Ensure app root is on sys.path
APP_ROOT = os.path.dirname(os.path.abspath(__file__))
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)

import socket

SINGLE_INSTANCE_PORT = 47891
SINGLE_INSTANCE_MUTEX = "Local\\HarnessProxyLauncher.SingleInstance"


class SingleInstanceGuard:
    """Process-lifetime Windows mutex that cannot disappear with a UI thread."""

    ERROR_ALREADY_EXISTS = 183

    def __init__(self):
        self._handle = None

    def acquire(self) -> bool:
        if os.name != "nt":
            return True

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.SetLastError(0)
        handle = kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX)
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
        if self._handle and os.name == "nt":
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

def main():
    guard = SingleInstanceGuard()

    # The mutex prevents duplicate background/tray instances even if the TCP
    # wake-up listener is temporarily unavailable.
    if not guard.acquire():
        notify_existing_instance()
        sys.exit(0)

    try:
        # Importing Tk, CustomTkinter, Pillow, psutil and pystray is the most
        # expensive part of a frozen start.  It must happen only after the
        # single-instance decision, otherwise every taskbar/double click loads
        # the whole UI just to exit again.
        from gui.main_window import MainWindow

        app = MainWindow()
        app.mainloop()
    except Exception as e:
        import traceback
        err = traceback.format_exc()
        try:
            import tkinter.messagebox as mb
            mb.showerror("Harness代理启动 运行错误", f"发生未捕获的异常:\n\n{err}")
        except Exception:
            print(err)
    finally:
        guard.close()

if __name__ == "__main__":
    main()
