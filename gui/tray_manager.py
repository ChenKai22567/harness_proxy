import os
import threading
from typing import Callable, Optional
from PIL import Image, ImageDraw
import pystray

class TrayManager:
    def __init__(
        self,
        app_icon_path: str,
        on_show_window: Callable[[], None],
        on_exit_app: Callable[[], None],
        on_launch_antigravity: Optional[Callable[[], None]] = None,
        on_launch_codex: Optional[Callable[[], None]] = None,
        on_launch_all: Optional[Callable[[], None]] = None,
        on_stop_all: Optional[Callable[[], None]] = None,
    ):
        self.app_icon_path = app_icon_path
        self.on_show_window = on_show_window
        self.on_exit_app = on_exit_app
        self.on_launch_antigravity = on_launch_antigravity
        self.on_launch_codex = on_launch_codex
        self.on_launch_all = on_launch_all
        self.on_stop_all = on_stop_all
        self.icon: Optional[pystray.Icon] = None
        self._thread: Optional[threading.Thread] = None

    def _create_image(self):
        # Prefer the dedicated small-size artwork, preserving its alpha channel.
        tray_path = os.path.join(os.path.dirname(self.app_icon_path), "tray_icon.png")
        for candidate in (tray_path, self.app_icon_path):
            if not os.path.exists(candidate):
                continue
            try:
                with Image.open(candidate) as source:
                    image = source.convert("RGBA")
                    if image.width != image.height:
                        side = max(image.size)
                        square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
                        square.alpha_composite(
                            image,
                            ((side - image.width) // 2, (side - image.height) // 2),
                        )
                        image = square
                    return image.copy()
            except Exception:
                continue

        # Keep the fallback identifiable as the application instead of showing
        # an opaque blue block when packaging/resource discovery regresses.
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((3, 3, 60, 60), radius=14, fill=(23, 111, 229, 255))
        draw.rounded_rectangle((16, 14, 25, 50), radius=4, fill=(255, 255, 255, 255))
        draw.rounded_rectangle((39, 14, 48, 50), radius=4, fill=(255, 255, 255, 255))
        draw.rectangle((23, 27, 41, 36), fill=(255, 255, 255, 255))
        return image

    def _build_menu(self):
        menu_items = []
        if self.on_launch_antigravity:
            menu_items.append(pystray.MenuItem("启动 Antigravity", lambda: self.on_launch_antigravity()))
        if self.on_launch_codex:
            menu_items.append(pystray.MenuItem("启动 Codex", lambda: self.on_launch_codex()))
        if self.on_launch_all:
            menu_items.append(pystray.MenuItem("一键全部启动", lambda: self.on_launch_all()))
        if self.on_stop_all:
            menu_items.append(pystray.MenuItem("结束所有进程", lambda: self.on_stop_all()))

        menu_items.append(pystray.Menu.SEPARATOR)
        menu_items.append(pystray.MenuItem("显示主窗口", lambda: self.on_show_window(), default=True))
        menu_items.append(pystray.MenuItem("退出程序", lambda: self.on_exit_app()))

        return pystray.Menu(*menu_items)

    def start(self):
        if self.icon is not None:
            return

        def _run():
            image = self._create_image()
            self.icon = pystray.Icon(
                "HarnessProxyLauncher",
                image,
                "Harness代理启动",
                menu=self._build_menu()
            )
            self.icon.run()

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

    def stop(self):
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass
            self.icon = None
