import os
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
import customtkinter as ctk
from PIL import Image

from core.config_manager import (
    DEFAULT_PROXY_TIMEOUT_MS,
    ConfigManager,
    get_app_root_dir,
    get_resource_path,
)
from core.launcher_engine import LauncherEngine
from core.proxy_prober import probe_tcp_port
from core.process_detector import get_process_snapshot_entries, process_map_from_entries
from core.single_instance import start_wakeup_server
from gui.dialog_base import ask_confirmation, show_message
from gui.native_window import (
    OpaqueWindow,
    apply_native_surface_guards,
    create_solid_background_brush,
    delete_background_brush,
    force_native_repaint,
)
from gui.theme import *  # noqa: F401,F403 - shared design tokens
from gui.tray_manager import TrayManager
from gui.widgets import ActionButton, ActionMenu, PolishedComboBox

# Standardize theme to system light mode
ctk.set_appearance_mode("Light")
ctk.set_default_color_theme("blue")
# Font-based rounded shapes are attractive but unusually expensive on some
# Windows graphics/font stacks. Polygon rendering avoids hundreds of glyph
# operations per repaint and stays visually crisp at the sizes used here.
ctk.DrawEngine.preferred_drawing_method = "polygon_shapes"

MAX_VISIBLE_LOG_LINES = 600
MIN_WINDOW_WIDTH = 604
MIN_WINDOW_HEIGHT = 388
APP_NAMES = {"antigravity": "Antigravity", "codex": "Codex (ChatGPT)"}


class ApplicationCard(ctk.CTkFrame):
    def __init__(self, master, name, *, launch, diagnose, restart, stop, components):
        super().__init__(
            master, corner_radius=CARD_RADIUS, fg_color=COLOR_CARD_BG,
            border_width=1, border_color=COLOR_CARD_BORDER,
        )
        self.grid_columnconfigure(0, weight=1)
        self._rendered_status = None
        heading = ctk.CTkFrame(self, fg_color="transparent")
        heading.grid(row=0, column=0, sticky="w", padx=16, pady=(15, 1))
        ctk.CTkLabel(
            heading, text=name, text_color=COLOR_TEXT_HEADING,
            font=ctk.CTkFont(family=FONT_FAMILY, size=19, weight="bold"),
        ).pack(side="left")
        self.badge = ctk.CTkLabel(
            heading, text="读取中", width=64, height=22, corner_radius=6,
            fg_color=COLOR_INACTIVE_BG, text_color=COLOR_INACTIVE_TEXT,
            font=ctk.CTkFont(family=FONT_FAMILY, size=11, weight="bold"),
        )
        self.badge.pack(side="left", padx=(10, 0))
        self.summary = ctk.CTkLabel(
            self, text="正在读取运行状态…", anchor="w", height=22,
            text_color=COLOR_TEXT_MUTED, font=ctk.CTkFont(family=FONT_FAMILY, size=12),
        )
        self.summary.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 15))

        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=0, column=1, rowspan=2, sticky="e", padx=(0, 16), pady=12)
        self.launch_btn = ActionButton(actions, text="启动", kind="primary", width=76, command=launch)
        self.launch_btn.pack(side="left")
        self.check_btn = ActionButton(actions, text="预检", width=64, command=diagnose)
        self.check_btn.pack(side="left", padx=8)
        self.more_btn = ActionButton(actions, text="更多", width=68, command=lambda: self.menu.toggle())
        self.more_btn.pack(side="left")
        self.menu = ActionMenu(self.more_btn, (
            ("restart", "重启", restart), ("stop", "停止", stop), ("components", "组件管理", components),
        ))
        self.menu.buttons["restart"].configure(state="disabled")
        self.menu.buttons["stop"].configure(state="disabled")

        self.feedback = ctk.CTkLabel(
            self, text="", anchor="w", justify="left", wraplength=480,
            padx=10, pady=4, corner_radius=6, fg_color=COLOR_ERROR_BG, text_color=COLOR_ERROR_TEXT,
            font=ctk.CTkFont(family=FONT_FAMILY, size=12),
        )

    def show_status(self, info, busy):
        running = bool(info.get("running"))
        found = bool(info.get("exe_found"))
        managed = bool(info.get("managed"))
        proxied = bool(info.get("has_proxy"))
        residual = len(info.get("residual_pids", []))
        status = (running, found, managed, proxied, residual, busy)
        if status == self._rendered_status:
            return
        self._rendered_status = status

        if running:
            badge = ("运行中", COLOR_SUCCESS_BG, COLOR_SUCCESS_TEXT)
            session = "受管会话" if managed else "外部会话"
            route = "代理已配置" if proxied else "代理配置待检查"
            summary = f"{session} · {route}"
        elif residual:
            badge = ("已退出", COLOR_WARN_BG, COLOR_WARN_TEXT)
            summary = f"{residual} 个残留进程 · 可在组件管理中清理"
        elif info and not found:
            badge = ("未找到", COLOR_ERROR_BG, COLOR_ERROR_TEXT)
            summary = "在偏好设置中指定安装路径"
        else:
            badge = ("未运行", COLOR_INACTIVE_BG, COLOR_INACTIVE_TEXT)
            summary = "已检测到安装" if found else "正在读取运行状态…"

        label, surface, color = badge
        self.badge.configure(text=label, fg_color=surface, text_color=color)
        self.summary.configure(text=summary)
        enabled = not (running or busy)
        self.launch_btn.configure(
            text="处理中…" if busy else "启动",
            state="normal" if enabled else "disabled",
        )
        menu_state = "normal" if running and not busy else "disabled"
        self.menu.buttons["restart"].configure(state=menu_state)
        self.menu.buttons["stop"].configure(state=menu_state)

    def show_error(self, message: str):
        if not message:
            self.feedback.grid_forget()
            return
        if "V8 版本不匹配" in message:
            message = "V8 组件版本不一致，请打开预检查看修复步骤。"
        elif message.startswith("Antigravity 安装异常"):
            message = "Antigravity 安装文件异常，请打开预检查看详情。"
        else:
            message = message.splitlines()[0]
            if len(message) > 72:
                message = message[:71] + "…"
        self.feedback.configure(text=message)
        self.feedback.grid(row=2, column=0, columnspan=2, sticky="ew", padx=16, pady=(0, 12))


class MainWindow(OpaqueWindow, ctk.CTk):
    def __init__(self, start_services: bool = True, allow_process_control: bool = True):
        super().__init__()

        # Keep the native window hidden until every CustomTkinter canvas has
        # completed its first layout pass.  Mapping a partially built window is
        # what produced the transient black rectangles on Windows.
        self.withdraw()
        self._ui_thread_id = threading.get_ident()
        self._ui_queue = queue.Queue()
        self._stop_event = threading.Event()
        self._refresh_lock = threading.Lock()
        self._actions_in_flight = set()
        self._manual_refresh_in_flight = False
        self._executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="harness-worker")
        self._monitor_started = False
        self._surface_ready = False
        self._restore_pending = False
        self._restore_token = 0
        self._start_services = start_services

        self.app_root = get_app_root_dir()
        self.icon_ico = get_resource_path("assets", "icon.ico")
        self.icon_png = get_resource_path("assets", "icon.png")

        # Window configuration
        self.title("Harness 代理启动")
        self.geometry("640x394")
        self.minsize(MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)
        self.configure(fg_color=COLOR_BG)

        if os.path.exists(self.icon_ico):
            try:
                self.iconbitmap(self.icon_ico)
            except Exception:
                pass

        # State & Engine
        self.config_mgr = ConfigManager()
        self.engine = LauncherEngine(
            self.config_mgr,
            log_callback=self._on_engine_log,
            allow_process_control=allow_process_control,
        )
        self.is_running = True
        self.is_window_visible = True
        self.log_drawer_visible = False
        self._visible_log_lines = 0
        self._last_proxy_status = None
        self._latest_app_info = {"antigravity": None, "codex": None}
        self._settings_dialog = None
        self._component_dialog = None
        self._diagnostic_dialogs = {}

        # Build UI
        self._build_ui()
        self.after(50, self._drain_ui_queue)

        # Initialize Tray Manager
        self.tray = None
        if self._start_services:
            self.tray = TrayManager(
                app_icon_path=self.icon_png,
                on_show_window=lambda: self._dispatch_ui(self._restore_from_tray),
                on_exit_app=lambda: self._dispatch_ui(self._force_exit_app),
                on_launch_antigravity=lambda: self._async_action(self.engine.launch_antigravity, "antigravity"),
                on_launch_codex=lambda: self._async_action(self.engine.launch_codex, "codex"),
                on_launch_all=lambda: self._async_action(self.engine.launch_all),
                on_stop_all=lambda: self._async_action(self.engine.stop_all)
            )
            self.tray.start()

        # Intercept window close
        self.protocol("WM_DELETE_WINDOW", self._on_window_close)

        def _on_window_map(event):
            if event.widget == self:
                self._apply_native_surface_guards()
        self.bind("<Map>", _on_window_map, add="+")

        # Initial ready log
        self.engine.log("Harness代理启动 已就绪", "INFO")

        # Start single-instance IPC listener to wake up on double click
        if self._start_services:
            self._start_single_instance_server()

        # Finish all hidden geometry work, then map the completed window once.
        self.update_idletasks()
        self.after_idle(self._show_initial_window)

    def _safe_after(self, delay_ms: int, callback):
        self._dispatch_ui(callback, delay_ms=delay_ms)

    def _dispatch_ui(self, callback, delay_ms: int = 0):
        """Run Tk work on the UI thread without calling ``after`` off-thread."""
        if not self.is_running:
            return
        if threading.get_ident() == self._ui_thread_id:
            try:
                if delay_ms > 0:
                    self.after(delay_ms, callback)
                else:
                    callback()
            except Exception:
                pass
            return
        self._ui_queue.put((max(0, int(delay_ms)), callback))

    def _drain_ui_queue(self):
        if not self.is_running:
            return
        for _ in range(100):
            try:
                delay_ms, callback = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                if delay_ms > 0:
                    self.after(delay_ms, callback)
                else:
                    callback()
            except Exception:
                pass
        if self.is_running:
            try:
                # 20 FPS is more than enough for status/UI dispatch and avoids
                # waking the Tk interpreter 62 times per second while idle.
                self.after(50 if self.is_window_visible else 200, self._drain_ui_queue)
            except Exception:
                pass

    def _show_initial_window(self):
        if not self.is_running:
            return
        if (
            self._start_services
            and self.config_mgr.config.get("launcher", {}).get("start_minimized", False)
        ):
            self.is_window_visible = False
            self._surface_ready = True
            self.engine.log("已按偏好设置在启动后隐藏到系统托盘。", "INFO")
            return
        self._apply_native_surface_guards()
        self.deiconify()
        self.update_idletasks()
        self._force_native_repaint()
        self.lift()
        self.focus_force()
        self._surface_ready = True
        if self._start_services and not self._monitor_started:
            self._monitor_started = True
            # Do not compete with the first paint. The first status snapshot is
            # useful, but it is not required before the window becomes usable.
            self.after(250, self._start_background_monitor)

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=WINDOW_PADDING, pady=(16, 14))
        try:
            with Image.open(self.icon_png) as icon:
                self._brand_icon = ctk.CTkImage(light_image=icon.convert("RGBA"), size=(42, 42))
            ctk.CTkLabel(header, text="", image=self._brand_icon).pack(side="left", padx=(0, 10))
        except OSError:
            pass
        ctk.CTkLabel(
            header, text="Harness 代理启动", text_color=COLOR_TEXT_HEADING,
            font=ctk.CTkFont(family=FONT_FAMILY, size=24, weight="bold"),
        ).pack(side="left")
        tools = ctk.CTkFrame(header, fg_color="transparent")
        tools.pack(side="right")
        self.refresh_btn = ActionButton(tools, text="刷新状态", width=80, command=self._manual_refresh)
        self.refresh_btn.pack(side="left")
        self.settings_btn = ActionButton(tools, text="偏好设置", width=80, command=self._open_settings)
        self.settings_btn.pack(side="left", padx=(8, 0))

        proxy = ctk.CTkFrame(
            self, fg_color=COLOR_CARD_BG, corner_radius=CARD_RADIUS,
            border_width=1, border_color=COLOR_CARD_BORDER,
        )
        proxy.grid(row=1, column=0, sticky="ew", padx=WINDOW_PADDING, pady=(0, SECTION_GAP))
        proxy.grid_columnconfigure(3, weight=1)
        ctk.CTkLabel(
            proxy, text="代理端口", text_color=COLOR_TEXT_PRIMARY,
            font=ctk.CTkFont(family=FONT_FAMILY, size=13, weight="bold"),
        ).grid(row=0, column=0, padx=(14, 8), pady=12)
        self.proxy_addr_badge = ctk.CTkLabel(
            proxy, text="—", anchor="w", text_color=COLOR_TEXT_SECTION,
            font=ctk.CTkFont(family="Consolas", size=11),
        )
        self.proxy_addr_badge.grid(row=0, column=1, sticky="w")
        self.proxy_status_badge = ctk.CTkLabel(
            proxy, text="检测中", width=108, height=24, corner_radius=5,
            fg_color=COLOR_INACTIVE_BG, text_color=COLOR_INACTIVE_TEXT,
            font=ctk.CTkFont(family=FONT_FAMILY, size=11, weight="bold"),
        )
        self.proxy_status_badge.grid(row=0, column=2, padx=(8, 0))
        ctk.CTkLabel(
            proxy, text="预设", text_color=COLOR_TEXT_MUTED,
            font=ctk.CTkFont(family=FONT_FAMILY, size=12),
        ).grid(row=0, column=4, padx=(10, 6))
        self.preset_combo = PolishedComboBox(
            proxy, values=[item["name"] for item in self.config_mgr.config.get("presets", [])],
            command=self._on_preset_quick_switch, width=180,
        )
        self.preset_combo.grid(row=0, column=5, padx=(0, 14), pady=12)
        self._update_preset_display()

        app_list = ctk.CTkFrame(self, fg_color="transparent")
        app_list.grid(row=2, column=0, sticky="ew", padx=WINDOW_PADDING)
        self.app_cards = {}
        for app_id, name in APP_NAMES.items():
            launch = getattr(self.engine, f"launch_{app_id}")
            card = ApplicationCard(
                app_list, name,
                launch=lambda target=app_id, action=launch: self._async_action(action, target),
                diagnose=lambda target=app_id: self._open_diagnostics(target),
                restart=lambda target=app_id: self._request_app_control(target, "restart"),
                stop=lambda target=app_id: self._request_app_control(target, "stop"),
                components=lambda target=app_id: self._open_components(target),
            )
            card.pack(fill="x", pady=(0, SECTION_GAP))
            self.app_cards[app_id] = card

        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=3, column=0, sticky="sew", padx=WINDOW_PADDING, pady=(2, 16))
        self.launch_all_btn = ActionButton(
            actions, text="全部启动", width=92, kind="primary",
            command=lambda: self._async_action(self.engine.launch_all),
        )
        self.launch_all_btn.pack(side="left")
        self.stop_all_btn = ActionButton(
            actions, text="全部停止", width=88, kind="danger",
            command=lambda: self._async_action(self.engine.stop_all),
        )
        self.stop_all_btn.pack(side="left", padx=(8, 0))
        self.toggle_log_btn = ActionButton(
            actions, text="运行日志", width=100, command=self._toggle_log_drawer,
        )
        self.toggle_log_btn.pack(side="right")
        self._build_log_drawer()

    def _build_log_drawer(self):
        self.log_container = ctk.CTkFrame(
            self, fg_color=COLOR_CARD_BG, corner_radius=CARD_RADIUS,
            border_width=1, border_color=COLOR_CARD_BORDER,
        )
        head = ctk.CTkFrame(self.log_container, fg_color="transparent")
        head.pack(fill="x", padx=12, pady=(8, 6))
        ctk.CTkLabel(
            head, text="运行日志", text_color=COLOR_TEXT_SECTION,
            font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold"),
        ).pack(side="left")
        ActionButton(head, text="清空", width=52, height=26, command=self._clear_logs).pack(side="right")
        self.log_textbox = ctk.CTkTextbox(
            self.log_container, height=100, wrap="word", fg_color=COLOR_LOG_BG,
            text_color=COLOR_LOG_TEXT, border_width=1, border_color=COLOR_LOG_BORDER,
            corner_radius=BUTTON_RADIUS, font=ctk.CTkFont(family="Consolas", size=11),
            scrollbar_button_color=COLOR_SCROLLBAR,
            scrollbar_button_hover_color=COLOR_SCROLLBAR_HOVER,
        )
        self.log_textbox.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.log_textbox.configure(state="disabled")

    def _update_preset_display(self):
        preset = self.config_mgr.config["proxy"].get("preset", "")
        self.preset_combo.set(preset if preset in self.preset_combo.cget("values") else "自定义")

    def _collect_status(self, refresh_paths: bool = False):
        """Collect one coherent status snapshot outside the Tk main thread."""
        with self._refresh_lock:
            if refresh_paths:
                self.engine.refresh_cached_paths()

            # snapshot() isolates the worker from in-place edits the UI thread
            # performs while saving (preset switch, settings dialog).
            p_cfg = self.config_mgr.snapshot().get("proxy", {})
            host = p_cfg.get("host", "127.0.0.1")
            port = p_cfg.get("port", 7890)
            timeout = p_cfg.get("timeout_ms", DEFAULT_PROXY_TIMEOUT_MS)
            proxy_ok, latency, msg = probe_tcp_port(host, port, timeout)

            process_entries = get_process_snapshot_entries()
            proc_map = process_map_from_entries(process_entries)
            anti_info = self.engine.preflight_antigravity(proc_map, process_entries)
            codex_info = self.engine.preflight_codex(proc_map, process_entries)
            return host, port, proxy_ok, latency, msg, anti_info, codex_info

    def _start_background_monitor(self):
        def _monitor_loop():
            while self.is_running and not self._stop_event.is_set():
                # If window is minimized or hidden, sleep without polling to save CPU & sockets
                if not self.is_window_visible:
                    if self._stop_event.wait(15.0):
                        break
                    continue

                try:
                    snapshot = self._collect_status()
                    if self.is_running:
                        self._dispatch_ui(lambda values=snapshot: self._apply_status_updates(*values))
                except Exception:
                    pass

                # A full Windows process snapshot is the dominant steady-state
                # cost. Manual refresh and every launch/stop still update
                # immediately; 15 seconds is sufficient for passive display.
                if self._stop_event.wait(15.0):
                    break

        threading.Thread(target=_monitor_loop, daemon=True).start()

    def _apply_status_updates(self, host, port, proxy_ok, latency, msg, anti_info, codex_info):
        self._latest_app_info["antigravity"] = dict(anti_info)
        self._latest_app_info["codex"] = dict(codex_info)
        proxy_signature = (host, port, proxy_ok, latency if proxy_ok else msg)
        if proxy_signature != self._last_proxy_status:
            self._last_proxy_status = proxy_signature
            address = f"{host}:{port}"
            self.proxy_addr_badge.configure(text=address if len(address) <= 21 else address[:20] + "…")
            if proxy_ok:
                self.proxy_status_badge.configure(
                    text=f"监听中 · {latency:.1f} ms", text_color=COLOR_SUCCESS_TEXT,
                    fg_color=COLOR_SUCCESS_BG,
                )
            else:
                self.proxy_status_badge.configure(
                    text="未连接", text_color=COLOR_ERROR_TEXT, fg_color=COLOR_ERROR_BG,
                )
        self._refresh_app_cards()

    def _refresh_app_cards(self):
        for app_id, card in self.app_cards.items():
            card.show_status(self._latest_app_info[app_id] or {}, app_id in self._actions_in_flight)
        state = "disabled" if self._actions_in_flight else "normal"
        if self.launch_all_btn.cget("state") != state:
            self.launch_all_btn.configure(state=state)
            self.stop_all_btn.configure(state=state)

    def _request_app_control(self, app_id: str, action: str):
        """Keep controls usable while preserving verified process ownership.

        Both applications share this flow: a managed session is controlled
        directly, an external one is confirmed, then verified and adopted
        before the operation touches only that root's process tree.
        """
        app_label = "Codex (ChatGPT)" if app_id == "codex" else "Antigravity"
        restart = self.engine.restart_codex if app_id == "codex" else self.engine.restart_antigravity
        stop = self.engine.stop_codex if app_id == "codex" else self.engine.stop_antigravity

        info = self._latest_app_info.get(app_id)
        if not info or not info.get("running"):
            self.engine.log(f"{app_label} 当前未运行，无法执行该操作。", "WARN")
            self._request_lightweight_refresh()
            return

        if info.get("managed"):
            self._async_action(restart if action == "restart" else stop, app_id)
            return

        root_pids = info.get("root_pids") or []
        if not root_pids:
            show_message(
                self, f"无法确认 {app_label} 根进程",
                f"检测到 {app_label} 正在运行，但无法验证应用根进程。请刷新状态后重试，或在组件管理中查看进程详情。",
            )
            return

        root_pid = int(root_pids[0])
        verb = "重启" if action == "restart" else "终止"
        if app_id == "codex":
            consequence = "当前对话会关闭并重新打开。" if action == "restart" else "当前对话会关闭。"
        else:
            consequence = "当前 Antigravity 会关闭并重新打开。" if action == "restart" else "当前 Antigravity 会关闭。"
        if not ask_confirmation(
            self, f"接管并{verb} {app_label}",
            f"当前 {app_label} 会话由其他启动方式创建，尚未纳入管理。\n\n"
            f"继续将先验证并接管根进程 PID {root_pid}，随后{verb}该进程及其实际子进程。"
            f"{consequence}\n\n是否继续？",
        ):
            return

        def _adopt_then_control():
            if action == "restart":
                adopt_restart = (self.engine.adopt_and_restart_codex
                                 if app_id == "codex"
                                 else self.engine.adopt_and_restart_antigravity)
                return adopt_restart(root_pid)
            return self.engine.adopt_and_stop(app_id, root_pid)

        _adopt_then_control.__name__ = f"adopt_then_{action}_{app_id}"
        self._async_action(_adopt_then_control, app_id)

    def _async_action(self, func, app_id=None):
        targets = {app_id} if app_id else set(self.app_cards)
        if self._actions_in_flight.intersection(targets):
            return
        self._actions_in_flight.update(targets)
        for target in targets:
            self.app_cards[target].show_error("")
        self._fit_card_errors()
        self._refresh_app_cards()

        def _finish(results):
            self._actions_in_flight.difference_update(targets)
            for target, (ok, message) in results.items():
                self.app_cards[target].show_error("" if ok else message)
            self._fit_card_errors()
            self._refresh_app_cards()
            self._request_lightweight_refresh()

        def _run():
            try:
                result = func()
                results = {app_id: result} if app_id else result
            except Exception as error:
                message = f"操作失败，{error}"
                self.engine.log(message, "ERROR")
                results = {target: (False, message) for target in targets}
            self._dispatch_ui(lambda: _finish(results))

        try:
            self._executor.submit(_run)
        except RuntimeError:
            _finish({target: (False, "操作无法启动，请重试。") for target in targets})

    def _fit_card_errors(self):
        error_rows = sum(bool(card.feedback.winfo_manager()) for card in self.app_cards.values())
        self.minsize(MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT + error_rows * 36)

    def _manual_refresh(self):
        if self._manual_refresh_in_flight:
            return
        self._manual_refresh_in_flight = True
        self.engine.log("正在重新扫描并刷新运行状态...", "INFO")
        self.refresh_btn.configure(state="disabled", text="扫描中...")

        def _run():
            try:
                snapshot = self._collect_status(refresh_paths=True)
                host, port, proxy_ok, latency, msg, anti_info, codex_info = snapshot

                # Dispatch UI update
                self._dispatch_ui(lambda values=snapshot: self._apply_status_updates(*values))

                # Log results explicitly to console
                if proxy_ok:
                    self.engine.log(f"代理状态: 正常监听 ({host}:{port}, 延时: {latency} ms)", "INFO")
                else:
                    self.engine.log(f"代理状态: 未连接 ({host}:{port}, 原因: {msg})", "WARN")

                anti_stat = f"运行中 (PIDs: {', '.join(map(str, anti_info['pids']))})" if anti_info["running"] else "未运行"
                anti_flag = " [代理已生效]" if anti_info["has_proxy"] else ""
                self.engine.log(f"Antigravity: {anti_stat}{anti_flag}", "INFO")

                codex_stat = f"运行中 (PIDs: {', '.join(map(str, codex_info['pids']))})" if codex_info["running"] else "未运行"
                codex_flag = " [代理已生效]" if codex_info["has_proxy"] else ""
                self.engine.log(f"Codex (ChatGPT): {codex_stat}{codex_flag}", "INFO")

                self.engine.log("刷新完成，运行状态已与系统同步。", "INFO")
            except Exception as e:
                self.engine.log(f"刷新失败: {e}", "ERROR")
            finally:
                def _finish():
                    self._manual_refresh_in_flight = False
                    self.refresh_btn.configure(state="normal", text="刷新状态")
                self._dispatch_ui(_finish)

        try:
            self._executor.submit(_run)
        except RuntimeError:
            self._manual_refresh_in_flight = False
            self.refresh_btn.configure(state="normal", text="刷新状态")

    def _request_lightweight_refresh(self):
        """Refresh runtime state without an expensive registry/path rescan."""
        if not self.is_running or self._manual_refresh_in_flight:
            return

        def _run():
            try:
                snapshot = self._collect_status(refresh_paths=False)
                self._dispatch_ui(lambda values=snapshot: self._apply_status_updates(*values))
            except Exception as exc:
                self.engine.log(f"状态更新失败: {exc}", "WARN")

        try:
            self._executor.submit(_run)
        except RuntimeError:
            pass

    def _open_settings(self):
        self.settings_btn.configure(state="disabled", text="打开中...")
        try:
            if self._settings_dialog is not None and self._settings_dialog.winfo_exists():
                self._settings_dialog.show()
                return
        except Exception:
            self._settings_dialog = None
        finally:
            if self._settings_dialog is not None:
                self.settings_btn.configure(state="normal", text="偏好设置")

        try:
            # Keep the sizeable settings module out of the cold-start import
            # path. More importantly, this construction no longer takes a Tk
            # input grab, so a hidden dialog can never disable the main window.
            from gui.settings_dialog import SettingsDialog
            self._settings_dialog = SettingsDialog(
                self,
                self.config_mgr,
                on_save_callback=self._on_settings_saved,
                on_close_callback=self._on_settings_closed,
                worker_executor=self._executor,
            )
        except Exception as exc:
            self._settings_dialog = None
            self.engine.log(f"打开偏好设置失败: {exc}", "ERROR")
            show_message(self, "偏好设置", f"无法打开偏好设置\n{exc}")
        finally:
            self.settings_btn.configure(state="normal", text="偏好设置")

    def _on_settings_closed(self):
        self._settings_dialog = None

    def _on_settings_saved(self):
        self.engine.log("配置已更新并保存！", "INFO")
        self._manual_refresh()
        self._update_preset_display()

    def _open_components(self, app_id: str):
        try:
            if self._component_dialog is not None and self._component_dialog.winfo_exists():
                self._component_dialog.show(app_id)
                return
        except Exception:
            self._component_dialog = None

        try:
            # Role classification reads command lines and memory only while
            # this window is open, keeping the normal status loop lightweight.
            from gui.component_dialog import ComponentDialog
            self._component_dialog = ComponentDialog(
                self,
                self.engine,
                self.config_mgr,
                app_id=app_id,
                on_saved=self._on_component_saved,
                on_close=self._on_component_closed,
                worker_executor=self._executor,
            )
        except Exception as exc:
            self._component_dialog = None
            self.engine.log(f"打开组件管理失败: {exc}", "ERROR")
            show_message(self, "组件管理", f"无法打开组件管理\n{exc}")

    def _on_component_closed(self):
        self._component_dialog = None

    def _on_component_saved(self):
        self.engine.log("组件启动策略已保存。", "INFO")
        self._request_lightweight_refresh()

    def _on_preset_quick_switch(self, choice: str):
        for p in self.config_mgr.config.get("presets", []):
            if p["name"] == choice:
                self.config_mgr.config["proxy"]["host"] = p.get("host", "127.0.0.1")
                self.config_mgr.config["proxy"]["port"] = p.get("port", 7890)
                self.config_mgr.config["proxy"]["preset"] = choice
                self.config_mgr.save()
                self.engine.log(f"已快速切换代理预设: {choice} ({p.get('host', '127.0.0.1')}:{p.get('port', 7890)})", "INFO")
                self._manual_refresh()
                break

    def _open_diagnostics(self, app_id):
        dialog = self._diagnostic_dialogs.get(app_id)
        if dialog is not None and not dialog._closed:
            dialog.show()
            return
        from gui.diagnostic_dialog import DiagnosticDialog

        check = self.engine.diagnose_codex_proxy if app_id == "codex" else self.engine.diagnose_antigravity
        self._diagnostic_dialogs[app_id] = DiagnosticDialog(self, app_id, APP_NAMES[app_id], check)

    def _toggle_log_drawer(self):
        width = round(self._reverse_window_scaling(self.winfo_width()))
        height = round(self._reverse_window_scaling(self.winfo_height()))
        if self.log_drawer_visible:
            self.log_container.grid_forget()
            self.grid_rowconfigure(3, weight=1)
            self.grid_rowconfigure(4, weight=0)
            self.toggle_log_btn.configure(text="运行日志")
            if self._compact_log_height and abs(height - self._expanded_log_height) <= 1:
                self.geometry(f"{width}x{self._compact_log_height}")
        else:
            self.grid_rowconfigure(3, weight=0)
            self.grid_rowconfigure(4, weight=1)
            self.log_container.grid(row=4, column=0, sticky="nsew", padx=WINDOW_PADDING, pady=(0, 16))
            self.toggle_log_btn.configure(text="收起日志")
            screen_height = self._reverse_window_scaling(self.winfo_screenheight() - 90)
            self._expanded_log_height = min(576, int(screen_height))
            self._compact_log_height = height if height < self._expanded_log_height else None
            if self._compact_log_height:
                self.geometry(f"{width}x{self._expanded_log_height}")
        self.log_drawer_visible = not self.log_drawer_visible

    def _clear_logs(self):
        try:
            self.log_textbox.configure(state="normal")
            self.log_textbox.delete("1.0", "end")
            self.log_textbox.configure(state="disabled")
            self._visible_log_lines = 0
        except Exception:
            pass

    def _on_engine_log(self, formatted_msg: str, level: str):
        def _append():
            try:
                self.log_textbox.configure(state="normal")
                self.log_textbox.insert("end", formatted_msg + "\n")
                self._visible_log_lines += 1
                if self._visible_log_lines > MAX_VISIBLE_LOG_LINES:
                    trim_count = min(100, self._visible_log_lines - MAX_VISIBLE_LOG_LINES)
                    self.log_textbox.delete("1.0", f"{trim_count + 1}.0")
                    self._visible_log_lines -= trim_count
                self.log_textbox.see("end")
                self.log_textbox.configure(state="disabled")
            except Exception:
                pass
        self._dispatch_ui(_append)

    def _apply_native_surface_guards(self):
        """Prevent DWM and Windows GDI from ever displaying a black box during restore.

        The Win32 work lives in :mod:`gui.native_window`; this wrapper owns the
        cached background brush and tolerates being called before the native
        parent exists.
        """
        if os.name != "nt":
            return
        try:
            child_hwnd = self.winfo_id()
            if not getattr(self, "_native_bg_brush", None):
                self._native_bg_brush = create_solid_background_brush(COLOR_BG)
            apply_native_surface_guards(child_hwnd, self._native_bg_brush)
        except Exception:
            pass

    def _force_native_repaint(self):
        """Paint the completed Tk surface before DWM presents a restored frame."""
        if os.name != "nt":
            return
        try:
            force_native_repaint(self.winfo_id())
        except Exception:
            pass

    def _start_single_instance_server(self):
        start_wakeup_server(
            is_running=lambda: self.is_running,
            on_wakeup=lambda: self._dispatch_ui(self._restore_from_tray),
            on_bind_failed=lambda message: self.engine.log(message, "WARN"),
        )

    def _on_window_close(self):
        minimize_tray = self.config_mgr.config["launcher"].get("minimize_to_tray_on_close", True)
        if minimize_tray:
            self.is_window_visible = False
            self.withdraw()
        else:
            self._force_exit_app()

    def _restore_from_tray(self):
        if threading.get_ident() != self._ui_thread_id:
            self._dispatch_ui(self._restore_from_tray)
            return
        if self._restore_pending:
            return
        try:
            if self.is_window_visible and self.state() == "normal":
                self.lift()
                self.focus_force()
                return
        except Exception:
            pass

        # Let the native tray menu finish closing, then present exactly one
        # fully painted frame. This avoids exposing CustomTkinter canvases
        # during the menu callback's DWM transition.
        self._restore_pending = True
        self._restore_token += 1
        token = self._restore_token
        self.update_idletasks()
        self.after(12, lambda: self._commit_restore(token))

    def _commit_restore(self, token: int):
        if not self.is_running or token != self._restore_token:
            self._restore_pending = False
            return
        try:
            self._apply_native_surface_guards()
            self.deiconify()
            self.update_idletasks()
            self._force_native_repaint()
            self.lift()
            self.focus_force()
            self.is_window_visible = True
            # Resuming never performs path/registry discovery on the UI thread.
            self._safe_after(100, self._request_lightweight_refresh)
            if self._start_services and not self._monitor_started:
                self._monitor_started = True
                self.after(250, self._start_background_monitor)
        finally:
            self._restore_pending = False

    def _force_exit_app(self):
        if threading.get_ident() != self._ui_thread_id:
            self._dispatch_ui(self._force_exit_app)
            return
        self.is_running = False
        self.is_window_visible = False
        self._stop_event.set()
        if self.tray:
            self.tray.stop()
        if hasattr(self, "_native_bg_brush") and self._native_bg_brush:
            delete_background_brush(self._native_bg_brush)
            self._native_bg_brush = None
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        for dialog in self._diagnostic_dialogs.values():
            if not dialog._closed:
                dialog.close()
        for callback in self.tk.splitlist(self.tk.call("after", "info")):
            self.tk.call("after", "cancel", callback)
        self.destroy()
