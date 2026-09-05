import os
import sys
import ctypes

# Harness代理启动 is Windows-only by design (winreg, ctypes.windll, the win32
# tray backend); the DPI call below is the first of those dependencies.
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

# Re-exported so existing imports and tests keep working against ``main``.
from core.single_instance import SingleInstanceGuard, notify_existing_instance


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
