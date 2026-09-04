import os
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from tkinter import messagebox
import customtkinter as ctk

from core.config_manager import ConfigManager, get_app_root_dir, get_resource_path
from core.launcher_engine import LauncherEngine
from core.proxy_prober import probe_tcp_port
from core.process_detector import get_process_snapshot_entries, process_map_from_entries
from gui.tray_manager import TrayManager
from gui.widgets import PolishedComboBox

# Standardize theme to system light mode
ctk.set_appearance_mode("Light")
ctk.set_default_color_theme("blue")
# Font-based rounded shapes are attractive but unusually expensive on some
# Windows graphics/font stacks. Polygon rendering avoids hundreds of glyph
# operations per repaint and stays visually crisp at the sizes used here.
ctk.DrawEngine.preferred_drawing_method = "polygon_shapes"

# Windows-aligned neutral light theme. Colour is reserved for compact status
# feedback; surfaces and controls stay within one cool-grey family.
FONT_FAMILY = "Microsoft YaHei UI"

COLOR_BG = "#F3F3F3"
COLOR_CARD_BG = "#FFFFFF"
COLOR_CARD_SOFT = "#F7F7F7"
COLOR_CARD_HOVER = "#F0F0F0"
COLOR_CARD_BORDER = "#E1E1E1"
COLOR_BORDER_STRONG = "#C7C7C7"

COLOR_TEXT_HEADING = "#1F1F1F"
COLOR_TEXT_PRIMARY = "#242424"
COLOR_TEXT_SECTION = "#3A3A3A"
COLOR_TEXT_MUTED = "#666666"
COLOR_TEXT_SUBTLE = "#8A8A8A"

COLOR_ACCENT = "#3B3B3B"
COLOR_ACCENT_HOVER = "#2F2F2F"
COLOR_ACCENT_SOFT = "#EEEEEE"

COLOR_SUCCESS = "#0AA36D"          # --type-manufacture: #0aa36d (refined emerald green)
COLOR_SUCCESS_HOVER = "#088A5C"
COLOR_SUCCESS_BG = "#E7F7F0"
COLOR_SUCCESS_TEXT = "#0A8055"

COLOR_WARN = "#F28A00"             # --city: #f28a00 (warm tech amber)
COLOR_WARN_BG = "#FFF7EB"
COLOR_WARN_TEXT = "#B45309"

COLOR_ERROR = "#D95765"            # --policy-data-red: #d95765 (soft rose-red)
COLOR_ERROR_BG = "#FDF0F1"
COLOR_ERROR_BORDER = "#F8CCD1"
COLOR_ERROR_TEXT = "#C53041"
COLOR_ERROR_HOVER = "#FCE1E4"

COLOR_INACTIVE_BG = "#EEEEEE"
COLOR_INACTIVE_TEXT = "#808080"

COLOR_LOG_BG = "#F7F8FA"
COLOR_LOG_BORDER = "#E1E5EA"
COLOR_LOG_TEXT = "#263548"
COLOR_LOG_CONTROL = "#EEF1F4"
COLOR_LOG_CONTROL_HOVER = "#E2E6EA"
COLOR_SCROLLBAR = "#C8D0DA"
COLOR_SCROLLBAR_HOVER = "#AEB8C5"

MAX_VISIBLE_LOG_LINES = 600

class MainWindow(ctk.CTk):
    # CustomTkinter's Windows title-bar helper hides the window and calls a
    # nested ``update()``. On a canvas-heavy window this can consume seconds,
    # swallow clicks, and expose black intermediate surfaces while restoring.
    _deactivate_windows_window_header_manipulation = True

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
        self._action_lock = threading.Lock()
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
        self.title("Harness代理启动")
        self.geometry("700x630")
        self.minsize(660, 500)
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
        self.log_drawer_visible = True
        self._visible_log_lines = 0
        self._last_anti_status = None
        self._last_codex_status = None
        self._last_proxy_status = None
        self._latest_codex_info = None
        self._settings_dialog = None
        self._component_dialog = None

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
                on_launch_antigravity=lambda: self._async_action(self.engine.launch_antigravity),
                on_launch_codex=lambda: self._async_action(self.engine.launch_codex),
                on_launch_all=lambda: self._async_action(self.engine.launch_all),
                on_stop_all=lambda: self._async_action(self.engine.stop_all)
            )
            self.tray.start()

        # Intercept window close
        self.protocol("WM_DELETE_WINDOW", self._on_window_close)

        def _on_window_map(event):
            if event.widget == self:
                self._apply_native_surface_guards()
                self.update_idletasks()
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
        self._apply_native_surface_guards()
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
        # ---------------- Header Frame ----------------
        header_frame = ctk.CTkFrame(self, corner_radius=8, fg_color=COLOR_CARD_BG, border_width=1, border_color=COLOR_CARD_BORDER)
        header_frame.pack(fill="x", padx=16, pady=(12, 6))

        # Title row
        title_row = ctk.CTkFrame(header_frame, fg_color="transparent")
        title_row.pack(fill="x", padx=14, pady=(10, 4))

        title_lbl = ctk.CTkLabel(
            title_row,
            text="Harness代理启动",
            font=ctk.CTkFont(family=FONT_FAMILY, size=18, weight="bold"),
            text_color=COLOR_TEXT_HEADING
        )
        title_lbl.pack(side="left")

        shield_badge = ctk.CTkLabel(
            title_row,
            text="进程沙箱 · 零系统污染",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11, weight="bold"),
            text_color=COLOR_ACCENT,
            fg_color=COLOR_ACCENT_SOFT,
            corner_radius=5,
            padx=8,
            pady=2
        )
        shield_badge.pack(side="left", padx=10)

        self.settings_btn = ctk.CTkButton(
            title_row,
            text="偏好设置",
            width=80,
            height=28,
            corner_radius=6,
            font=ctk.CTkFont(family=FONT_FAMILY, size=12),
            fg_color="#FFFFFF",
            text_color=COLOR_TEXT_SECTION,
            hover_color=COLOR_CARD_HOVER,
            border_width=1,
            border_color=COLOR_BORDER_STRONG,
            command=self._open_settings
        )
        self.settings_btn.pack(side="right")

        self.refresh_btn = ctk.CTkButton(
            title_row,
            text="刷新状态",
            width=80,
            height=28,
            corner_radius=6,
            font=ctk.CTkFont(family=FONT_FAMILY, size=12),
            fg_color="#FFFFFF",
            text_color=COLOR_TEXT_SECTION,
            hover_color=COLOR_CARD_HOVER,
            border_width=1,
            border_color=COLOR_CARD_BORDER,
            command=self._manual_refresh
        )
        self.refresh_btn.pack(side="right", padx=6)

        # Proxy Status row (with fixed widths to prevent any jitter)
        proxy_row = ctk.CTkFrame(header_frame, fg_color=COLOR_CARD_SOFT, corner_radius=6, border_width=1, border_color=COLOR_CARD_BORDER)
        proxy_row.pack(fill="x", padx=14, pady=(4, 6))

        ctk.CTkLabel(
            proxy_row,
            text="代理网络:",
            font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold"),
            text_color=COLOR_TEXT_SECTION
        ).pack(side="left", padx=(10, 8), pady=6)

        # Fixed width badge prevents layout shifting
        self.proxy_status_badge = ctk.CTkLabel(
            proxy_row,
            text="检测中...",
            width=135,
            anchor="center",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11, weight="bold"),
            text_color=COLOR_WARN_TEXT,
            fg_color=COLOR_WARN_BG,
            corner_radius=5,
            pady=2
        )
        self.proxy_status_badge.pack(side="left", padx=(4, 6))

        # Fixed width address display
        self.proxy_addr_badge = ctk.CTkLabel(
            proxy_row,
            text="127.0.0.1:7890",
            width=105,
            anchor="w",
            font=ctk.CTkFont(family=FONT_FAMILY, size=12),
            text_color=COLOR_TEXT_MUTED
        )
        self.proxy_addr_badge.pack(side="left", padx=(5, 8))

        # Compact preset dropdown with a bordered toggle and custom styled popup.
        ctk.CTkLabel(
            proxy_row,
            text="预设:",
            font=ctk.CTkFont(family=FONT_FAMILY, size=12),
            text_color=COLOR_TEXT_SECTION
        ).pack(side="left", padx=(12, 6))

        self.preset_combo = PolishedComboBox(
            proxy_row,
            values=[p["name"] for p in self.config_mgr.config.get("presets", [])],
            command=self._on_preset_quick_switch,
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            width=208,
            height=30,
            corner_radius=6,
            border_width=1,
            border_color=COLOR_BORDER_STRONG,
            fg_color="#FFFFFF",
            text_color=COLOR_TEXT_PRIMARY,
            text_color_disabled=COLOR_TEXT_SUBTLE,
            button_color="#FFFFFF",
            button_hover_color=COLOR_CARD_HOVER,
            dropdown_fg_color="#FFFFFF",
            dropdown_hover_color=COLOR_CARD_HOVER,
            dropdown_text_color=COLOR_TEXT_PRIMARY,
            dropdown_font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            focus_color="#A8A8A8",
            arrow_color=COLOR_TEXT_MUTED,
            menu_active_text_color=COLOR_TEXT_PRIMARY,
            toggle_border_color=COLOR_BORDER_STRONG,
            popup_border_color=COLOR_BORDER_STRONG,
            popup_selected_color=COLOR_ACCENT_SOFT,
        )
        self.preset_combo.pack(side="left", padx=(4, 8))
        preset_name = self.config_mgr.config["proxy"].get("preset", "Clash / Mihomo (7890)")
        if preset_name in self.preset_combo.cget("values"):
            self.preset_combo.set(preset_name)

        # ---------------- Core Cards Container ----------------
        cards_container = ctk.CTkFrame(self, fg_color="transparent")
        cards_container.pack(fill="x", padx=16, pady=4)

        # ====== Card 1: Antigravity ======
        self.anti_card = ctk.CTkFrame(cards_container, corner_radius=8, fg_color=COLOR_CARD_BG, border_width=1, border_color=COLOR_CARD_BORDER)
        self.anti_card.pack(fill="x", pady=5)

        anti_top = ctk.CTkFrame(self.anti_card, fg_color="transparent")
        anti_top.pack(fill="x", padx=14, pady=(10, 4))

        ctk.CTkLabel(
            anti_top,
            text="Antigravity",
            font=ctk.CTkFont(family=FONT_FAMILY, size=15, weight="bold"),
            text_color=COLOR_TEXT_HEADING
        ).pack(side="left")

        self.anti_status_badge = ctk.CTkLabel(
            anti_top,
            text="未运行",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11, weight="bold"),
            text_color=COLOR_INACTIVE_TEXT,
            fg_color=COLOR_INACTIVE_BG,
            corner_radius=5,
            padx=8,
            pady=2
        )
        self.anti_status_badge.pack(side="left", padx=10)

        self.anti_path_lbl = ctk.CTkLabel(
            self.anti_card,
            text="路径: 探测中...",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            text_color=COLOR_TEXT_MUTED,
            anchor="w"
        )
        self.anti_path_lbl.pack(fill="x", padx=14, pady=(0, 2))

        self.anti_patch_lbl = ctk.CTkLabel(
            self.anti_card,
            text="浏览器扩展代理: 检测中...",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            text_color=COLOR_SUCCESS_TEXT,
            anchor="w"
        )
        self.anti_patch_lbl.pack(fill="x", padx=14, pady=(0, 6))

        anti_btn_row = ctk.CTkFrame(self.anti_card, fg_color="transparent")
        anti_btn_row.pack(fill="x", padx=14, pady=(2, 10))

        self.anti_launch_btn = ctk.CTkButton(
            anti_btn_row,
            text="专用启动",
            width=85,
            height=30,
            corner_radius=6,
            font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold"),
            fg_color=COLOR_ACCENT,
            hover_color=COLOR_ACCENT_HOVER,
            text_color="#FFFFFF",
            command=lambda: self._async_action(self.engine.launch_antigravity)
        )
        self.anti_launch_btn.pack(side="left", padx=(0, 6))

        self.anti_restart_btn = ctk.CTkButton(
            anti_btn_row,
            text="重启",
            width=70,
            height=30,
            corner_radius=6,
            font=ctk.CTkFont(family=FONT_FAMILY, size=12),
            fg_color="#FFFFFF",
            text_color=COLOR_TEXT_SECTION,
            hover_color=COLOR_CARD_HOVER,
            border_width=1,
            border_color=COLOR_CARD_BORDER,
            command=lambda: self._async_action(self.engine.restart_antigravity)
        )
        self.anti_restart_btn.pack(side="left", padx=6)

        self.anti_stop_btn = ctk.CTkButton(
            anti_btn_row,
            text="终止进程",
            width=80,
            height=30,
            corner_radius=6,
            font=ctk.CTkFont(family=FONT_FAMILY, size=12),
            fg_color=COLOR_ERROR_BG,
            text_color=COLOR_ERROR_TEXT,
            hover_color=COLOR_ERROR_HOVER,
            border_width=1,
            border_color=COLOR_ERROR_BORDER,
            command=lambda: self._async_action(self.engine.stop_antigravity)
        )
        self.anti_stop_btn.pack(side="left", padx=6)

        self.anti_check_btn = ctk.CTkButton(
            anti_btn_row,
            text="预检诊断",
            width=80,
            height=30,
            corner_radius=6,
            font=ctk.CTkFont(family=FONT_FAMILY, size=12),
            fg_color="#FFFFFF",
            text_color=COLOR_TEXT_SECTION,
            border_width=1,
            border_color=COLOR_CARD_BORDER,
            hover_color=COLOR_CARD_HOVER,
            command=self._check_antigravity_diag
        )
        self.anti_check_btn.pack(side="right")

        self.anti_components_btn = ctk.CTkButton(
            anti_btn_row,
            text="组件管理",
            width=80,
            height=30,
            corner_radius=6,
            font=ctk.CTkFont(family=FONT_FAMILY, size=12),
            fg_color="#FFFFFF",
            text_color=COLOR_TEXT_SECTION,
            border_width=1,
            border_color=COLOR_CARD_BORDER,
            hover_color=COLOR_CARD_HOVER,
            command=lambda: self._open_components("antigravity")
        )
        self.anti_components_btn.pack(side="right", padx=6)

        # ====== Card 2: Codex (ChatGPT) ======
        self.codex_card = ctk.CTkFrame(cards_container, corner_radius=8, fg_color=COLOR_CARD_BG, border_width=1, border_color=COLOR_CARD_BORDER)
        self.codex_card.pack(fill="x", pady=5)

        codex_top = ctk.CTkFrame(self.codex_card, fg_color="transparent")
        codex_top.pack(fill="x", padx=14, pady=(10, 4))

        ctk.CTkLabel(
            codex_top,
            text="Codex (ChatGPT)",
            font=ctk.CTkFont(family=FONT_FAMILY, size=15, weight="bold"),
            text_color=COLOR_TEXT_HEADING
        ).pack(side="left")

        self.codex_status_badge = ctk.CTkLabel(
            codex_top,
            text="未运行",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11, weight="bold"),
            text_color=COLOR_INACTIVE_TEXT,
            fg_color=COLOR_INACTIVE_BG,
            corner_radius=5,
            padx=8,
            pady=2
        )
        self.codex_status_badge.pack(side="left", padx=10)

        self.codex_path_lbl = ctk.CTkLabel(
            self.codex_card,
            text="路径: 探测中...",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            text_color=COLOR_TEXT_MUTED,
            anchor="w"
        )
        self.codex_path_lbl.pack(fill="x", padx=14, pady=(0, 2))

        self.codex_conn_lbl = ctk.CTkLabel(
            self.codex_card,
            text="网络模式: 独立混合端口代理",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            text_color=COLOR_TEXT_MUTED,
            anchor="w"
        )
        self.codex_conn_lbl.pack(fill="x", padx=14, pady=(0, 6))

        codex_btn_row = ctk.CTkFrame(self.codex_card, fg_color="transparent")
        codex_btn_row.pack(fill="x", padx=14, pady=(2, 10))

        self.codex_launch_btn = ctk.CTkButton(
            codex_btn_row,
            text="专用启动",
            width=85,
            height=30,
            corner_radius=6,
            font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold"),
            fg_color=COLOR_SUCCESS,
            hover_color=COLOR_SUCCESS_HOVER,
            text_color="#FFFFFF",
            command=lambda: self._async_action(self.engine.launch_codex)
        )
        self.codex_launch_btn.pack(side="left", padx=(0, 6))

        self.codex_restart_btn = ctk.CTkButton(
            codex_btn_row,
            text="重启",
            width=70,
            height=30,
            corner_radius=6,
            font=ctk.CTkFont(family=FONT_FAMILY, size=12),
            fg_color="#FFFFFF",
            text_color=COLOR_TEXT_SECTION,
            hover_color=COLOR_CARD_HOVER,
            border_width=1,
            border_color=COLOR_CARD_BORDER,
            command=lambda: self._request_codex_control("restart")
        )
        self.codex_restart_btn.pack(side="left", padx=6)

        self.codex_stop_btn = ctk.CTkButton(
            codex_btn_row,
            text="终止进程",
            width=80,
            height=30,
            corner_radius=6,
            font=ctk.CTkFont(family=FONT_FAMILY, size=12),
            fg_color=COLOR_ERROR_BG,
            text_color=COLOR_ERROR_TEXT,
            hover_color=COLOR_ERROR_HOVER,
            border_width=1,
            border_color=COLOR_ERROR_BORDER,
            command=lambda: self._request_codex_control("stop")
        )
        self.codex_stop_btn.pack(side="left", padx=6)

        self.codex_check_btn = ctk.CTkButton(
            codex_btn_row,
            text="预检诊断",
            width=80,
            height=30,
            corner_radius=6,
            font=ctk.CTkFont(family=FONT_FAMILY, size=12),
            fg_color="#FFFFFF",
            text_color=COLOR_TEXT_SECTION,
            border_width=1,
            border_color=COLOR_CARD_BORDER,
            hover_color=COLOR_CARD_HOVER,
            command=self._check_codex_diag
        )
        self.codex_check_btn.pack(side="right")

        self.codex_components_btn = ctk.CTkButton(
            codex_btn_row,
            text="组件管理",
            width=80,
            height=30,
            corner_radius=6,
            font=ctk.CTkFont(family=FONT_FAMILY, size=12),
            fg_color="#FFFFFF",
            text_color=COLOR_TEXT_SECTION,
            border_width=1,
            border_color=COLOR_CARD_BORDER,
            hover_color=COLOR_CARD_HOVER,
            command=lambda: self._open_components("codex")
        )
        self.codex_components_btn.pack(side="right", padx=6)

        # ---------------- Global Batch Action Row ----------------
        # Centered action buttons with right-aligned log toggle
        batch_row = ctk.CTkFrame(self, fg_color="transparent")
        batch_row.pack(fill="x", padx=16, pady=(6, 8))

        batch_row.grid_columnconfigure(0, weight=1, uniform="half")
        batch_row.grid_columnconfigure(1, weight=0) # Mathematically dead-center
        batch_row.grid_columnconfigure(2, weight=1, uniform="half")

        center_cluster = ctk.CTkFrame(batch_row, fg_color="transparent")
        center_cluster.grid(row=0, column=1)

        self.launch_all_btn = ctk.CTkButton(
            center_cluster,
            text="一键全部启动",
            font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold"),
            fg_color=COLOR_ACCENT,
            hover_color=COLOR_ACCENT_HOVER,
            text_color="#FFFFFF",
            corner_radius=6,
            height=32,
            width=120,
            command=lambda: self._async_action(self.engine.launch_all)
        )
        self.launch_all_btn.pack(side="left", padx=6)

        self.stop_all_btn = ctk.CTkButton(
            center_cluster,
            text="全部停止",
            font=ctk.CTkFont(family=FONT_FAMILY, size=12),
            fg_color=COLOR_ERROR_BG,
            hover_color=COLOR_ERROR_HOVER,
            text_color=COLOR_ERROR_TEXT,
            border_width=1,
            border_color=COLOR_ERROR_BORDER,
            corner_radius=6,
            height=32,
            width=85,
            command=lambda: self._async_action(self.engine.stop_all)
        )
        self.stop_all_btn.pack(side="left", padx=6)

        self.toggle_log_btn = ctk.CTkButton(
            batch_row,
            text="运行日志 ▼",
            font=ctk.CTkFont(family=FONT_FAMILY, size=12),
            width=90,
            height=32,
            corner_radius=6,
            fg_color="#FFFFFF",
            text_color=COLOR_TEXT_SECTION,
            hover_color=COLOR_CARD_HOVER,
            border_width=1,
            border_color=COLOR_CARD_BORDER,
            command=self._toggle_log_drawer
        )
        self.toggle_log_btn.grid(row=0, column=2, sticky="e")

        # ---------------- Collapsible Log Console ----------------
        # wrap="word" for natural auto-wrapping without horizontal scrollbar
        self.log_container = ctk.CTkFrame(self, corner_radius=8, fg_color=COLOR_CARD_BG, border_width=1, border_color=COLOR_CARD_BORDER)
        self.log_container.pack(fill="both", expand=True, padx=16, pady=(2, 12))

        log_head = ctk.CTkFrame(self.log_container, fg_color="transparent")
        log_head.pack(fill="x", padx=12, pady=(6, 4))
        ctk.CTkLabel(
            log_head,
            text="运行日志",
            font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold"),
            text_color=COLOR_TEXT_SECTION
        ).pack(side="left")

        ctk.CTkButton(
            log_head,
            text="清空",
            width=45,
            height=22,
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            fg_color=COLOR_LOG_CONTROL,
            text_color=COLOR_TEXT_MUTED,
            hover_color=COLOR_LOG_CONTROL_HOVER,
            border_width=1,
            border_color=COLOR_LOG_BORDER,
            corner_radius=5,
            command=self._clear_logs
        ).pack(side="right")

        # wrap="word" eliminates horizontal scrollbar!
        self.log_textbox = ctk.CTkTextbox(
            self.log_container,
            font=ctk.CTkFont(family="Consolas", size=10),
            fg_color=COLOR_LOG_BG,
            text_color=COLOR_LOG_TEXT,
            border_width=1,
            border_color=COLOR_LOG_BORDER,
            corner_radius=6,
            wrap="word",
            activate_scrollbars=True,
            scrollbar_button_color=COLOR_SCROLLBAR,
            scrollbar_button_hover_color=COLOR_SCROLLBAR_HOVER
        )
        self.log_textbox.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.log_textbox.configure(state="disabled")


    # ==========================================
    # Background Polling Loop (Traffic & Power Friendly)
    # ==========================================
    def _collect_status(self, refresh_paths: bool = False):
        """Collect one coherent status snapshot outside the Tk main thread."""
        with self._refresh_lock:
            if refresh_paths:
                self.engine.refresh_cached_paths()

            p_cfg = self.config_mgr.config.get("proxy", {})
            host = p_cfg.get("host", "127.0.0.1")
            port = p_cfg.get("port", 7890)
            timeout = p_cfg.get("timeout_ms", 1000)
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
        self._latest_codex_info = dict(codex_info)
        # 1. Update Proxy Status Badge (fixed width, zero layout shift)
        proxy_signature = (host, port, proxy_ok, latency if proxy_ok else msg)
        if proxy_signature != self._last_proxy_status:
            self._last_proxy_status = proxy_signature
            self.proxy_addr_badge.configure(text=f"{host}:{port}")
            if proxy_ok:
                self.proxy_status_badge.configure(
                    text=f"监听中 ({latency} ms)",
                    text_color=COLOR_SUCCESS_TEXT,
                    fg_color=COLOR_SUCCESS_BG
                )
            else:
                self.proxy_status_badge.configure(
                    text=f"未连接 ({msg})",
                    text_color=COLOR_ERROR_TEXT,
                    fg_color=COLOR_ERROR_BG
                )

        # 2. Only redraw application cards when their semantic state changes.
        # Repainting every CustomTkinter canvas on a four-second timer caused
        # unnecessary CPU/GDI work and made restore animations less stable.
        anti_signature = (
            anti_info["exe_found"], anti_info["exe_path"], anti_info["running"],
            tuple(anti_info["pids"]), anti_info["has_proxy"],
            anti_info["node_found"], anti_info["npx_found"], anti_info["node_path"],
            anti_info.get("managed", False), tuple(anti_info.get("owned_pids", [])),
            tuple(anti_info.get("residual_pids", [])),
        )
        if anti_signature != self._last_anti_status:
            self._last_anti_status = anti_signature
            if anti_info["exe_found"]:
                self.anti_path_lbl.configure(
                    text=f"可执行文件: {anti_info['exe_path']}",
                    text_color=COLOR_TEXT_MUTED,
                )
            else:
                self.anti_path_lbl.configure(
                    text="未找到 Antigravity，请在设置中手动指定路径",
                    text_color=COLOR_ERROR_TEXT,
                )

            if anti_info["running"]:
                num_procs = len(anti_info["pids"])
                primary_pids = anti_info.get("root_pids") or anti_info["pids"]
                main_pid = primary_pids[0] if primary_pids else "-"
                managed = bool(anti_info.get("managed"))
                owner_text = "受管" if managed else "外部"
                proxy_text = "代理已生效" if anti_info["has_proxy"] else "直连无代理"
                badge_text = f"运行中 ({num_procs}进程) · {proxy_text}"
                self.anti_status_badge.configure(
                    text=badge_text,
                    text_color=COLOR_SUCCESS_TEXT if anti_info["has_proxy"] else COLOR_WARN_TEXT,
                    fg_color=COLOR_SUCCESS_BG if anti_info["has_proxy"] else COLOR_WARN_BG,
                )
                self.anti_patch_lbl.configure(
                    text=f"{owner_text}会话: 主 PID {main_pid} (共 {num_procs} 个进程)"
                         + (" · 可安全停止" if managed else " · 接管后可停止/重启"),
                    text_color=COLOR_SUCCESS_TEXT if managed else COLOR_WARN_TEXT,
                )
                self.anti_launch_btn.configure(text="正在运行", state="disabled", fg_color=COLOR_INACTIVE_BG, text_color=COLOR_INACTIVE_TEXT)
                self.anti_restart_btn.configure(state="normal" if managed else "disabled")
                self.anti_stop_btn.configure(state="normal" if managed else "disabled")
            else:
                residual_pids = anti_info.get("residual_pids", [])
                if residual_pids:
                    self.anti_status_badge.configure(
                        text=f"已退出 · {len(residual_pids)}个残留",
                        text_color=COLOR_WARN_TEXT,
                        fg_color=COLOR_WARN_BG,
                    )
                    self.anti_patch_lbl.configure(
                        text="主进程已退出；可启动新会话，或在组件管理中清理残留",
                        text_color=COLOR_WARN_TEXT,
                    )
                elif anti_info["node_found"] and anti_info["npx_found"]:
                    self.anti_status_badge.configure(text="未运行", text_color=COLOR_INACTIVE_TEXT, fg_color=COLOR_INACTIVE_BG)
                    self.anti_patch_lbl.configure(text=f"浏览器扩展代理: 已就绪 ({anti_info['node_path']})", text_color=COLOR_SUCCESS_TEXT)
                else:
                    self.anti_status_badge.configure(text="未运行", text_color=COLOR_INACTIVE_TEXT, fg_color=COLOR_INACTIVE_BG)
                    self.anti_patch_lbl.configure(text="浏览器扩展代理: Node/npx 未检测到，已优雅降级", text_color=COLOR_WARN_TEXT)
                self.anti_launch_btn.configure(text="专用启动", state="normal", fg_color=COLOR_ACCENT, text_color="#FFFFFF")
                self.anti_restart_btn.configure(state="disabled")
                self.anti_stop_btn.configure(state="disabled")

        codex_signature = (
            codex_info["exe_found"], codex_info["exe_path"], codex_info["running"],
            tuple(codex_info["pids"]), codex_info["has_proxy"],
            codex_info.get("browser_proxy", False), codex_info.get("api_proxy", False),
            codex_info.get("websocket_proxy", False), codex_info.get("proxy_mode", ""),
            codex_info.get("system_proxy", {}).get("matches", False),
            codex_info.get("managed", False), tuple(codex_info.get("owned_pids", [])),
            tuple(codex_info.get("residual_pids", [])),
        )
        if codex_signature != self._last_codex_status:
            self._last_codex_status = codex_signature
            if codex_info["exe_found"]:
                self.codex_path_lbl.configure(
                    text=f"可执行文件: {codex_info['exe_path']}",
                    text_color=COLOR_TEXT_MUTED,
                )
            else:
                self.codex_path_lbl.configure(
                    text="未找到 Codex (ChatGPT.exe)，请在设置中手动选择路径",
                    text_color=COLOR_ERROR_TEXT,
                )

            if codex_info["running"]:
                num_procs = len(codex_info["pids"])
                primary_pids = codex_info.get("root_pids") or codex_info["pids"]
                main_pid = primary_pids[0] if primary_pids else "-"
                managed = bool(codex_info.get("managed"))
                owner_text = "受管" if managed else "外部"
                badge_text = f"运行中 ({num_procs}进程) · 代理已生效" if codex_info["has_proxy"] else f"运行中 ({num_procs}进程) · 代理不完整"
                self.codex_status_badge.configure(
                    text=badge_text,
                    text_color=COLOR_SUCCESS_TEXT if codex_info["has_proxy"] else COLOR_WARN_TEXT,
                    fg_color=COLOR_SUCCESS_BG if codex_info["has_proxy"] else COLOR_WARN_BG,
                )
                layer_text = " / ".join((
                    f"浏览器{'✓' if codex_info.get('browser_proxy') else '×'}",
                    f"API{'✓' if codex_info.get('api_proxy') else '×'}",
                    f"WebSocket{'✓' if (codex_info.get('websocket_proxy') or codex_info.get('system_proxy', {}).get('matches')) else '×'}",
                ))
                self.codex_conn_lbl.configure(
                    text=f"{owner_text}会话: 主 PID {main_pid} · {layer_text}"
                         + ("" if managed else " · 重启/终止时确认接管"),
                    text_color=COLOR_SUCCESS_TEXT if codex_info["has_proxy"] else COLOR_WARN_TEXT,
                )
                self.codex_launch_btn.configure(text="正在运行", state="disabled", fg_color=COLOR_INACTIVE_BG, text_color=COLOR_INACTIVE_TEXT)
                self.codex_restart_btn.configure(state="normal")
                self.codex_stop_btn.configure(state="normal")
            else:
                residual_pids = codex_info.get("residual_pids", [])
                if residual_pids:
                    self.codex_status_badge.configure(
                        text=f"已退出 · {len(residual_pids)}个残留",
                        text_color=COLOR_WARN_TEXT,
                        fg_color=COLOR_WARN_BG,
                    )
                    self.codex_conn_lbl.configure(
                        text="主进程已退出；残留不会阻止启动，可在组件管理中安全清理",
                        text_color=COLOR_WARN_TEXT,
                    )
                else:
                    self.codex_status_badge.configure(text="未运行", text_color=COLOR_INACTIVE_TEXT, fg_color=COLOR_INACTIVE_BG)
                    self.codex_conn_lbl.configure(text="网络模式: 独立混合端口代理", text_color=COLOR_TEXT_MUTED)
                self.codex_launch_btn.configure(text="专用启动", state="normal", fg_color=COLOR_SUCCESS, text_color="#FFFFFF")
                self.codex_restart_btn.configure(state="disabled")
                self.codex_stop_btn.configure(state="disabled")

    # ==========================================
    # Actions & Handlers
    # ==========================================
    def _request_codex_control(self, action: str):
        """Keep controls usable while preserving verified process ownership."""
        info = self._latest_codex_info
        if not info or not info.get("running"):
            self.engine.log("Codex 当前未运行，无法执行该操作。", "WARN")
            self._request_lightweight_refresh()
            return

        if info.get("managed"):
            target = self.engine.restart_codex if action == "restart" else self.engine.stop_codex
            self._async_action(target)
            return

        root_pids = info.get("root_pids") or []
        if not root_pids:
            messagebox.showwarning(
                "无法确认 Codex 根进程",
                "检测到 Codex 正在运行，但无法验证应用根进程。请刷新状态后重试，或在组件管理中查看进程详情。",
                parent=self,
            )
            return

        root_pid = int(root_pids[0])
        verb = "重启" if action == "restart" else "终止"
        if not messagebox.askyesno(
            f"接管并{verb} Codex",
            f"当前 Codex 会话由其他启动方式创建，尚未纳入管理。\n\n"
            f"继续将先验证并接管根进程 PID {root_pid}，随后{verb}该进程及其实际子进程。"
            f"{'当前对话会关闭并重新打开。' if action == 'restart' else '当前对话会关闭。'}\n\n是否继续？",
            parent=self,
        ):
            return

        def _adopt_then_control():
            if action == "restart":
                return self.engine.adopt_and_restart_codex(root_pid)
            return self.engine.adopt_and_stop("codex", root_pid)

        _adopt_then_control.__name__ = f"adopt_then_{action}_codex"
        self._async_action(_adopt_then_control)

    def _async_action(self, func):
        action_key = getattr(func, "__name__", repr(func))
        with self._action_lock:
            if action_key in self._actions_in_flight:
                self.engine.log(f"操作 {action_key} 已在执行，请稍候。", "WARN")
                return
            self._actions_in_flight.add(action_key)

        def _run():
            try:
                func()
            except Exception as e:
                self.engine.log(f"操作异常: {e}", "ERROR")
            finally:
                with self._action_lock:
                    self._actions_in_flight.discard(action_key)
                self._dispatch_ui(self._request_lightweight_refresh)

        try:
            self._executor.submit(_run)
        except RuntimeError:
            with self._action_lock:
                self._actions_in_flight.discard(action_key)

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
            )
        except Exception as exc:
            self._settings_dialog = None
            self.engine.log(f"打开偏好设置失败: {exc}", "ERROR")
            messagebox.showerror("偏好设置", f"无法打开偏好设置：\n{exc}", parent=self)
        finally:
            self.settings_btn.configure(state="normal", text="偏好设置")

    def _on_settings_closed(self):
        self._settings_dialog = None

    def _on_settings_saved(self):
        self.engine.log("配置已更新并保存！", "INFO")
        self._manual_refresh()
        preset_name = self.config_mgr.config["proxy"].get("preset", "Clash / Mihomo (7890)")
        if preset_name in self.preset_combo.cget("values"):
            self.preset_combo.set(preset_name)

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
            )
        except Exception as exc:
            self._component_dialog = None
            self.engine.log(f"打开组件管理失败: {exc}", "ERROR")
            messagebox.showerror("组件管理", f"无法打开组件管理：\n{exc}", parent=self)

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

    def _check_antigravity_diag(self):
        info = self.engine.preflight_antigravity()
        msg = f"【Antigravity 诊断结果】\n\n"
        msg += f"• 可执行文件: {'已找到' if info['exe_found'] else '未找到'}\n  路径: {info['exe_path']}\n"
        msg += f"• 运行状态: {'运行中' if info['running'] else '未运行'}\n  PIDs: {info['pids']}\n"
        if info.get("residual_pids"):
            msg += f"• 存活残留: {info['residual_pids']}（可在组件管理中清理）\n"
        if info.get("ignored_pids"):
            msg += f"• 已终止记录: {info['ignored_pids']}（不计为运行中）\n"
        msg += f"• 代理参数注入: {'已生效' if info['has_proxy'] else '未挂载/未运行'}\n"
        msg += f"• Node.js 环境: {'已找到' if info['node_found'] else '未找到'}\n  路径: {info['node_path']}\n"
        msg += f"• NPX 命令: {'已找到' if info['npx_found'] else '未找到'}\n  路径: {info['npx_path']}\n"
        messagebox.showinfo("Antigravity 预检诊断", msg, parent=self)

    def _check_codex_diag(self):
        self.codex_check_btn.configure(state="disabled", text="检测中...")

        def _show_result(result=None, error=None):
            self.codex_check_btn.configure(state="normal", text="预检诊断")
            if error:
                messagebox.showerror("Codex 预检诊断", f"诊断失败：\n{error}", parent=self)
                return
            info = result["process"]
            listener = result["listener"]
            system_proxy = info.get("system_proxy", {})
            ws_ready = info.get("websocket_proxy") or system_proxy.get("matches")
            ownership = "受管会话" if info.get("managed") else ("外部/旧会话" if info.get("running") else "—")
            msg = "【Codex (ChatGPT) 分层诊断】\n\n"
            msg += f"• 可执行文件: {'已找到' if info['exe_found'] else '未找到'}\n  路径: {info['exe_path']}\n"
            msg += f"• 运行状态: {'运行中' if info['running'] else '未运行'} · {ownership}\n  PIDs: {info['pids']}\n"
            if info.get("residual_pids"):
                msg += f"• 存活残留: {info['residual_pids']}（不会阻止重新启动）\n"
            if info.get("ignored_pids"):
                msg += f"• 已终止记录: {info['ignored_pids']}（已从运行状态排除）\n"
            msg += f"• 本地代理监听: {'正常' if listener['reachable'] else '失败'} ({listener['host']}:{listener['port']})\n"
            msg += f"• 代理 HTTPS 出站: {'正常' if result['outbound_ok'] else '失败'} ({result['outbound_message']})\n"
            msg += f"• Chromium 浏览器层: {'已代理' if info.get('browser_proxy') else '未检测到'}\n"
            msg += f"• Codex API 环境层: {'已代理' if info.get('api_proxy') else '未检测到'}\n"
            msg += f"• Responses WebSocket 层: {'已代理' if ws_ready else '未检测到'}\n"
            msg += f"• 当前模式: {info.get('proxy_mode', '未知')}\n"
            if system_proxy.get("enabled"):
                msg += f"• Windows 系统代理: 已开启 ({system_proxy.get('server', '')})\n"
            if info.get("running") and not ws_ready:
                msg += "\n建议：使用启动器重新启动受管会话，以注入标准 HTTP/HTTPS/ALL_PROXY 环境。"
            messagebox.showinfo("Codex 预检诊断", msg, parent=self)

        def _run():
            try:
                result = self.engine.diagnose_codex_proxy()
                self._dispatch_ui(lambda: _show_result(result=result))
            except Exception as exc:
                self._dispatch_ui(lambda: _show_result(error=str(exc)))

        try:
            self._executor.submit(_run)
        except RuntimeError as exc:
            _show_result(error=str(exc))

    def _toggle_log_drawer(self):
        if self.log_drawer_visible:
            self.log_container.pack_forget()
            self.toggle_log_btn.configure(text="运行日志 ▲")
            self.log_drawer_visible = False
        else:
            self.log_container.pack(fill="both", expand=True, padx=16, pady=(2, 12))
            self.toggle_log_btn.configure(text="运行日志 ▼")
            self.log_drawer_visible = True

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

        1. Strips WS_EX_LAYERED to ensure the window remains a pure standard native window.
        2. Sets GCLP_HBRBACKGROUND on the window class to a solid brush matching COLOR_BG.
           so that any GDI background erase is clean and light, never black.
        3. Sets DWMWA_TRANSITIONS_FORCEDISABLED to True so Windows DWM skips the zoom animation
           that stretches an uninitialized black DirectX backbuffer from the taskbar button.
        """
        if os.name == "nt":
            try:
                import ctypes
                from ctypes import wintypes
                child_hwnd = self.winfo_id()
                parent_hwnd = ctypes.windll.user32.GetParent(child_hwnd)
                if not parent_hwnd:
                    return

                # 1. Strip WS_EX_LAYERED
                GWL_EXSTYLE = -20
                WS_EX_LAYERED = 0x00080000
                exstyle = ctypes.windll.user32.GetWindowLongW(parent_hwnd, GWL_EXSTYLE)
                if exstyle & WS_EX_LAYERED:
                    ctypes.windll.user32.SetWindowLongW(parent_hwnd, GWL_EXSTYLE, exstyle & ~WS_EX_LAYERED)
                    ctypes.windll.user32.SetWindowPos(parent_hwnd, 0, 0, 0, 0, 0, 0x0027)

                # 2. Set solid GDI background brush matching COLOR_BG
                if not hasattr(self, "_native_bg_brush") or not self._native_bg_brush:
                    r = int(COLOR_BG[1:3], 16)
                    g = int(COLOR_BG[3:5], 16)
                    b = int(COLOR_BG[5:7], 16)
                    colorref = r | (g << 8) | (b << 16)
                    self._native_bg_brush = ctypes.windll.gdi32.CreateSolidBrush(colorref)

                GCLP_HBRBACKGROUND = -10
                ctypes.windll.user32.SetClassLongPtrW(parent_hwnd, GCLP_HBRBACKGROUND, self._native_bg_brush)
                ctypes.windll.user32.SetClassLongPtrW(child_hwnd, GCLP_HBRBACKGROUND, self._native_bg_brush)

                # 3. Disable DWM minimize/restore transition animation
                DWMWA_TRANSITIONS_FORCEDISABLED = 3
                val = wintypes.BOOL(True)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    parent_hwnd,
                    DWMWA_TRANSITIONS_FORCEDISABLED,
                    ctypes.byref(val),
                    ctypes.sizeof(val)
                )
            except Exception:
                pass

    def _force_native_repaint(self):
        """Paint the completed Tk surface before DWM presents a restored frame."""
        if os.name != "nt":
            return
        try:
            import ctypes
            child_hwnd = self.winfo_id()
            parent_hwnd = ctypes.windll.user32.GetParent(child_hwnd) or child_hwnd
            RDW_INVALIDATE = 0x0001
            RDW_ERASE = 0x0004
            RDW_ALLCHILDREN = 0x0080
            RDW_UPDATENOW = 0x0100
            ctypes.windll.user32.RedrawWindow(
                parent_hwnd,
                None,
                None,
                RDW_INVALIDATE | RDW_ERASE | RDW_ALLCHILDREN | RDW_UPDATENOW,
            )
            ctypes.windll.dwmapi.DwmFlush()
        except Exception:
            pass

    def _start_single_instance_server(self):
        def _server_loop():
            import socket
            try:
                server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                server.bind(('127.0.0.1', 47891))
                server.listen(5)
                server.settimeout(1.0)
                while self.is_running:
                    try:
                        conn, _ = server.accept()
                        data = conn.recv(1024)
                        conn.close()
                        if b'WAKEUP' in data:
                            self._dispatch_ui(self._restore_from_tray)
                    except socket.timeout:
                        continue
                    except OSError:
                        break
            except Exception:
                pass
        threading.Thread(target=_server_loop, daemon=True).start()

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
            self._apply_native_surface_guards()
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
            try:
                import ctypes
                ctypes.windll.gdi32.DeleteObject(self._native_bg_brush)
                self._native_bg_brush = None
            except Exception:
                pass
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        self.destroy()
