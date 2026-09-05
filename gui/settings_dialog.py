import os
import threading
from concurrent.futures import ThreadPoolExecutor
from tkinter import filedialog, messagebox
import customtkinter as ctk
from typing import Callable, Optional

from core.config_manager import ConfigManager, get_resource_path
from core.proxy_prober import probe_tcp_port, scan_available_proxies
from gui.theme import *  # noqa: F401,F403 - shared design tokens
from gui.widgets import CrispCheckBox, PolishedComboBox

COLOR_INACTIVE_BG = "#EEEEEE"
COLOR_INACTIVE_TEXT = "#808080"

class SettingsDialog(ctk.CTkToplevel):
    # Avoid CustomTkinter's nested withdraw/update title-bar workaround. When
    # combined with our own hidden first layout it could leave this toplevel
    # invisible while it still owned the Tk input grab.
    _deactivate_windows_window_header_manipulation = True

    def __init__(
        self,
        parent,
        config_mgr: ConfigManager,
        on_save_callback: Optional[Callable[[], None]] = None,
        on_close_callback: Optional[Callable[[], None]] = None,
        worker_executor: Optional[ThreadPoolExecutor] = None,
    ):
        super().__init__(parent)
        # A CTkToplevel maps immediately unless explicitly withdrawn.  Build it
        # off-screen first so users never see the blank/partially painted page.
        self.withdraw()
        self._parent = parent
        self._ui_thread_id = threading.get_ident()
        self._worker_executor = worker_executor

        # Always reload the freshest configuration from disk every time opened
        self.config_mgr = config_mgr
        self.config_mgr.load()
        self.on_save_callback = on_save_callback
        self.on_close_callback = on_close_callback
        self._close_notified = False

        self.title("偏好设置 - Harness代理启动")
        self.geometry("580x650")
        self.resizable(False, False)
        self.transient(parent)
        self.configure(fg_color=COLOR_BG)

        # Window icon
        icon_path = get_resource_path("assets", "icon.ico")
        if os.path.exists(icon_path):
            try:
                self.iconbitmap(icon_path)
            except Exception:
                pass

        self._build_ui()
        self._load_values()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda _event: self._close())
        self.update_idletasks()
        self._center_over_parent()
        self.show()

    def _center_over_parent(self):
        width, height = 580, 650
        try:
            parent_x = self._parent.winfo_x()
            parent_y = self._parent.winfo_y()
            parent_width = self._parent.winfo_width()
            physical_width = self._apply_window_scaling(width)
            physical_height = self._apply_window_scaling(height)
            title_height = max(0, self._parent.winfo_rooty() - parent_y)
            x = parent_x + (parent_width - physical_width) // 2
            # Align to the parent's top edge instead of offsetting a centered
            # rectangle.  Because this dialog is shorter than the main window,
            # "center then move up" could still leave its top visibly lower.
            y = parent_y - 24
            x = max(0, min(x, self.winfo_screenwidth() - physical_width))
            y = max(0, min(y, self.winfo_screenheight() - physical_height - title_height))
            self.geometry(f"{width}x{height}+{x}+{y}")
        except Exception:
            self.geometry(f"{width}x{height}")

    def show(self):
        try:
            if not self.winfo_exists():
                return
            self.deiconify()
            self.state("normal")
            self.lift()
            self.focus_force()
        except Exception:
            pass

    def _dispatch_ui(self, callback):
        """Use the parent window's thread-safe dispatcher for worker results."""
        def _safe_callback():
            try:
                if self.winfo_exists():
                    callback()
            except Exception:
                pass

        if threading.get_ident() == self._ui_thread_id:
            _safe_callback()
            return

        parent_dispatch = getattr(self._parent, "_dispatch_ui", None)
        if callable(parent_dispatch):
            parent_dispatch(_safe_callback)

    def _run_in_background(self, worker):
        """Probe on the shared launcher executor; fall back to a bare thread."""
        if self._worker_executor is not None:
            try:
                self._worker_executor.submit(worker)
                return
            except RuntimeError:
                pass
        threading.Thread(target=worker, daemon=True).start()

    def _close(self):
        if self._close_notified:
            return
        self._close_notified = True
        try:
            self.destroy()
        finally:
            if self.on_close_callback:
                try:
                    self.on_close_callback()
                except Exception:
                    pass

    def _build_ui(self):
        # ---------------- Dialog Header Frame ----------------
        header_frame = ctk.CTkFrame(self, corner_radius=8, fg_color=COLOR_CARD_BG, border_width=1, border_color=COLOR_CARD_BORDER)
        header_frame.pack(fill="x", padx=14, pady=(10, 5))

        h_row = ctk.CTkFrame(header_frame, fg_color="transparent")
        h_row.pack(fill="x", padx=14, pady=8)

        ctk.CTkLabel(
            h_row,
            text="偏好设置",
            font=ctk.CTkFont(family=FONT_FAMILY, size=15, weight="bold"),
            text_color=COLOR_TEXT_HEADING
        ).pack(side="left")

        ctk.CTkLabel(
            h_row,
            text="代理网络参数 · 路径检索 · 系统行为",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            text_color=COLOR_TEXT_MUTED
        ).pack(side="left", padx=10)

        # ---------------- Non-Scrollable Content Container (All fits without scrollbar) ----------------
        content_frame = ctk.CTkFrame(self, fg_color="transparent")
        content_frame.pack(padx=14, pady=0, fill="both", expand=True)

        # ====== Section 1: Proxy Network Settings ======
        proxy_card = ctk.CTkFrame(content_frame, fg_color=COLOR_CARD_BG, border_width=1, border_color=COLOR_CARD_BORDER, corner_radius=8)
        proxy_card.pack(fill="x", pady=4)

        ctk.CTkLabel(
            proxy_card,
            text="代理网络设置",
            font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold"),
            text_color=COLOR_TEXT_SECTION
        ).pack(anchor="w", padx=14, pady=(8, 4))

        # Preset selector row
        p_row1 = ctk.CTkFrame(proxy_card, fg_color="transparent")
        p_row1.pack(fill="x", padx=14, pady=3)
        ctk.CTkLabel(
            p_row1,
            text="快捷预设:",
            width=65,
            anchor="w",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            text_color=COLOR_TEXT_PRIMARY
        ).pack(side="left")

        self.preset_combo = PolishedComboBox(
            p_row1,
            values=[p["name"] for p in self.config_mgr.config.get("presets", [])],
            command=self._on_preset_selected,
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            width=230,
            height=30,
            corner_radius=6,
            border_width=1,
            fg_color="#FFFFFF",
            border_color=COLOR_BORDER_STRONG,
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
        self.preset_combo.pack(side="left", padx=4)

        self.scan_btn = ctk.CTkButton(
            p_row1,
            text="自动探测端口",
            width=95,
            height=28,
            corner_radius=6,
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            fg_color="#FFFFFF",
            text_color=COLOR_TEXT_SECTION,
            hover_color=COLOR_CARD_HOVER,
            border_width=1,
            border_color=COLOR_CARD_BORDER,
            command=self._scan_ports
        )
        self.scan_btn.pack(side="left", padx=4)

        # Host & Port row
        p_row2 = ctk.CTkFrame(proxy_card, fg_color="transparent")
        p_row2.pack(fill="x", padx=14, pady=3)
        ctk.CTkLabel(
            p_row2,
            text="主机 IP:",
            width=65,
            anchor="w",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            text_color=COLOR_TEXT_PRIMARY
        ).pack(side="left")

        self.host_entry = ctk.CTkEntry(
            p_row2,
            width=135,
            height=28,
            corner_radius=5,
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            fg_color="#FFFFFF",
            border_color=COLOR_CARD_BORDER,
            text_color=COLOR_TEXT_PRIMARY
        )
        self.host_entry.pack(side="left", padx=4)

        ctk.CTkLabel(
            p_row2,
            text="端口:",
            width=35,
            anchor="e",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            text_color=COLOR_TEXT_PRIMARY
        ).pack(side="left")

        self.port_entry = ctk.CTkEntry(
            p_row2,
            width=65,
            height=28,
            corner_radius=5,
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            fg_color="#FFFFFF",
            border_color=COLOR_CARD_BORDER,
            text_color=COLOR_TEXT_PRIMARY
        )
        self.port_entry.pack(side="left", padx=4)

        self.test_btn = ctk.CTkButton(
            p_row2,
            text="测试连通",
            width=80,
            height=28,
            corner_radius=6,
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            fg_color=COLOR_ACCENT,
            hover_color=COLOR_ACCENT_HOVER,
            text_color="#FFFFFF",
            command=self._test_proxy_conn
        )
        self.test_btn.pack(side="left", padx=4)

        self.test_status_label = ctk.CTkLabel(
            p_row2,
            text="",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            text_color=COLOR_TEXT_MUTED
        )
        self.test_status_label.pack(side="left", padx=6)

        # Bypass row
        p_row3 = ctk.CTkFrame(proxy_card, fg_color="transparent")
        p_row3.pack(fill="x", padx=14, pady=(2, 8))
        ctk.CTkLabel(
            p_row3,
            text="绕过地址:",
            width=65,
            anchor="w",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            text_color=COLOR_TEXT_PRIMARY
        ).pack(side="left")

        self.bypass_entry = ctk.CTkEntry(
            p_row3,
            width=360,
            height=28,
            corner_radius=5,
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            fg_color="#FFFFFF",
            border_color=COLOR_CARD_BORDER,
            text_color=COLOR_TEXT_PRIMARY
        )
        self.bypass_entry.pack(side="left", padx=4)

        # ====== Section 2: Antigravity Path ======
        anti_card = ctk.CTkFrame(content_frame, fg_color=COLOR_CARD_BG, border_width=1, border_color=COLOR_CARD_BORDER, corner_radius=8)
        anti_card.pack(fill="x", pady=4)

        ctk.CTkLabel(
            anti_card,
            text="Antigravity 运行配置",
            font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold"),
            text_color=COLOR_TEXT_SECTION
        ).pack(anchor="w", padx=14, pady=(8, 3))

        self.anti_auto_var = ctk.BooleanVar(value=True)
        self.anti_auto_chk = CrispCheckBox(
            anti_card,
            text="自动扫描安装位置 (推荐，自适应不同计算机路径)",
            variable=self.anti_auto_var,
            command=self._toggle_anti_path,
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            checkbox_width=16,
            checkbox_height=16,
            corner_radius=3,
            border_width=1,
            border_color=COLOR_BORDER_STRONG,
            fg_color=COLOR_ACCENT,
            hover_color=COLOR_ACCENT_HOVER,
            checkmark_color="#FFFFFF",
            text_color=COLOR_TEXT_PRIMARY
        )
        self.anti_auto_chk.pack(anchor="w", padx=14, pady=2)

        anti_p_row = ctk.CTkFrame(anti_card, fg_color="transparent")
        anti_p_row.pack(fill="x", padx=14, pady=3)
        ctk.CTkLabel(
            anti_p_row,
            text="自定义路径:",
            width=65,
            anchor="w",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            text_color=COLOR_TEXT_PRIMARY
        ).pack(side="left")

        self.anti_path_entry = ctk.CTkEntry(
            anti_p_row,
            width=305,
            height=28,
            corner_radius=5,
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            fg_color="#FFFFFF",
            border_color=COLOR_CARD_BORDER,
            text_color=COLOR_TEXT_PRIMARY
        )
        self.anti_path_entry.pack(side="left", padx=4)

        self.anti_browse_btn = ctk.CTkButton(
            anti_p_row,
            text="浏览...",
            width=60,
            height=28,
            corner_radius=6,
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            fg_color="#FFFFFF",
            text_color=COLOR_TEXT_SECTION,
            hover_color=COLOR_CARD_HOVER,
            border_width=1,
            border_color=COLOR_CARD_BORDER,
            command=self._browse_anti_path
        )
        self.anti_browse_btn.pack(side="left", padx=2)

        self.anti_shim_var = ctk.BooleanVar(value=True)
        self.anti_shim_chk = CrispCheckBox(
            anti_card,
            text="装载浏览器代理补丁 (Node.js/npx 自动接管 DevTools)",
            variable=self.anti_shim_var,
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            checkbox_width=16,
            checkbox_height=16,
            corner_radius=3,
            border_width=1,
            border_color=COLOR_BORDER_STRONG,
            fg_color=COLOR_ACCENT,
            hover_color=COLOR_ACCENT_HOVER,
            checkmark_color="#FFFFFF",
            text_color=COLOR_TEXT_PRIMARY
        )
        self.anti_shim_chk.pack(anchor="w", padx=14, pady=(2, 8))

        # ====== Section 3: Codex Path ======
        codex_card = ctk.CTkFrame(content_frame, fg_color=COLOR_CARD_BG, border_width=1, border_color=COLOR_CARD_BORDER, corner_radius=8)
        codex_card.pack(fill="x", pady=4)

        ctk.CTkLabel(
            codex_card,
            text="Codex (ChatGPT) 运行配置",
            font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold"),
            text_color=COLOR_TEXT_SECTION
        ).pack(anchor="w", padx=14, pady=(8, 3))

        self.codex_auto_var = ctk.BooleanVar(value=True)
        self.codex_auto_chk = CrispCheckBox(
            codex_card,
            text="自动检索 Microsoft Store 包 (OpenAI.Codex 注册表直查)",
            variable=self.codex_auto_var,
            command=self._toggle_codex_path,
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            checkbox_width=16,
            checkbox_height=16,
            corner_radius=3,
            border_width=1,
            border_color=COLOR_BORDER_STRONG,
            fg_color=COLOR_ACCENT,
            hover_color=COLOR_ACCENT_HOVER,
            checkmark_color="#FFFFFF",
            text_color=COLOR_TEXT_PRIMARY
        )
        self.codex_auto_chk.pack(anchor="w", padx=14, pady=2)

        codex_p_row = ctk.CTkFrame(codex_card, fg_color="transparent")
        codex_p_row.pack(fill="x", padx=14, pady=(2, 8))
        ctk.CTkLabel(
            codex_p_row,
            text="自定义路径:",
            width=65,
            anchor="w",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            text_color=COLOR_TEXT_PRIMARY
        ).pack(side="left")

        self.codex_path_entry = ctk.CTkEntry(
            codex_p_row,
            width=305,
            height=28,
            corner_radius=5,
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            fg_color="#FFFFFF",
            border_color=COLOR_CARD_BORDER,
            text_color=COLOR_TEXT_PRIMARY
        )
        self.codex_path_entry.pack(side="left", padx=4)

        self.codex_browse_btn = ctk.CTkButton(
            codex_p_row,
            text="浏览...",
            width=60,
            height=28,
            corner_radius=6,
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            fg_color="#FFFFFF",
            text_color=COLOR_TEXT_SECTION,
            hover_color=COLOR_CARD_HOVER,
            border_width=1,
            border_color=COLOR_CARD_BORDER,
            command=self._browse_codex_path
        )
        self.codex_browse_btn.pack(side="left", padx=2)

        # ====== Section 4: General Preferences ======
        gen_card = ctk.CTkFrame(content_frame, fg_color=COLOR_CARD_BG, border_width=1, border_color=COLOR_CARD_BORDER, corner_radius=8)
        gen_card.pack(fill="x", pady=4)

        ctk.CTkLabel(
            gen_card,
            text="全局偏好",
            font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold"),
            text_color=COLOR_TEXT_SECTION
        ).pack(anchor="w", padx=14, pady=(8, 3))

        gen_row = ctk.CTkFrame(gen_card, fg_color="transparent")
        gen_row.pack(fill="x", padx=14, pady=(2, 8))

        self.tray_var = ctk.BooleanVar(value=True)
        self.tray_chk = CrispCheckBox(
            gen_row,
            text="关闭时最小化到系统托盘",
            variable=self.tray_var,
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            checkbox_width=16,
            checkbox_height=16,
            corner_radius=3,
            border_width=1,
            border_color=COLOR_BORDER_STRONG,
            fg_color=COLOR_ACCENT,
            hover_color=COLOR_ACCENT_HOVER,
            checkmark_color="#FFFFFF",
            text_color=COLOR_TEXT_PRIMARY
        )
        self.tray_chk.pack(side="left", padx=(0, 20))

        self.check_clash_var = ctk.BooleanVar(value=True)
        self.check_clash_chk = CrispCheckBox(
            gen_row,
            text="启动前强校验端口 (防误开直连)",
            variable=self.check_clash_var,
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            checkbox_width=16,
            checkbox_height=16,
            corner_radius=3,
            border_width=1,
            border_color=COLOR_BORDER_STRONG,
            fg_color=COLOR_ACCENT,
            hover_color=COLOR_ACCENT_HOVER,
            checkmark_color="#FFFFFF",
            text_color=COLOR_TEXT_PRIMARY
        )
        self.check_clash_chk.pack(side="left")

        gen_row2 = ctk.CTkFrame(gen_card, fg_color="transparent")
        gen_row2.pack(fill="x", padx=14, pady=(0, 8))

        self.start_minimized_var = ctk.BooleanVar(value=False)
        self.start_minimized_chk = CrispCheckBox(
            gen_row2,
            text="启动后直接隐藏到系统托盘",
            variable=self.start_minimized_var,
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            checkbox_width=16,
            checkbox_height=16,
            corner_radius=3,
            border_width=1,
            border_color=COLOR_BORDER_STRONG,
            fg_color=COLOR_ACCENT,
            hover_color=COLOR_ACCENT_HOVER,
            checkmark_color="#FFFFFF",
            text_color=COLOR_TEXT_PRIMARY
        )
        self.start_minimized_chk.pack(side="left")

        # ---------------- Bottom Action Button Bar ----------------
        btn_bar = ctk.CTkFrame(self, fg_color="transparent")
        btn_bar.pack(fill="x", padx=14, pady=(6, 12))

        self.reset_btn = ctk.CTkButton(
            btn_bar,
            text="恢复默认",
            width=80,
            height=30,
            corner_radius=6,
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            fg_color="#FFFFFF",
            text_color=COLOR_TEXT_MUTED,
            hover_color=COLOR_CARD_HOVER,
            border_width=1,
            border_color=COLOR_CARD_BORDER,
            command=self._reset_defaults
        )
        self.reset_btn.pack(side="left")

        self.save_btn = ctk.CTkButton(
            btn_bar,
            text="保存生效",
            width=90,
            height=30,
            corner_radius=6,
            font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold"),
            fg_color=COLOR_ACCENT,
            hover_color=COLOR_ACCENT_HOVER,
            text_color="#FFFFFF",
            command=self._save_config
        )
        self.save_btn.pack(side="right", padx=(6, 0))

        self.cancel_btn = ctk.CTkButton(
            btn_bar,
            text="取消",
            width=65,
            height=30,
            corner_radius=6,
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            fg_color="#FFFFFF",
            text_color=COLOR_TEXT_SECTION,
            hover_color=COLOR_CARD_HOVER,
            border_width=1,
            border_color=COLOR_CARD_BORDER,
            command=self._close
        )
        self.cancel_btn.pack(side="right")

    def _load_values(self):
        cfg = self.config_mgr.config
        p_cfg = cfg.get("proxy", {})
        self.host_entry.delete(0, "end")
        self.host_entry.insert(0, p_cfg.get("host", "127.0.0.1"))
        self.port_entry.delete(0, "end")
        self.port_entry.insert(0, str(p_cfg.get("port", 7890)))
        self.bypass_entry.delete(0, "end")
        self.bypass_entry.insert(0, p_cfg.get("bypass", "localhost,127.0.0.1,::1"))

        preset_name = p_cfg.get("preset", "Clash / Mihomo (7890)")
        if preset_name in self.preset_combo.cget("values"):
            self.preset_combo.set(preset_name)

        # Antigravity
        a_cfg = cfg.get("apps", {}).get("antigravity", {})
        self.anti_auto_var.set(a_cfg.get("auto_detect", True))
        self.anti_path_entry.delete(0, "end")
        self.anti_path_entry.insert(0, a_cfg.get("custom_path", ""))
        self.anti_shim_var.set(a_cfg.get("enable_browser_shim", True))
        self._toggle_anti_path()

        # Codex
        c_cfg = cfg.get("apps", {}).get("codex", {})
        self.codex_auto_var.set(c_cfg.get("auto_detect", True))
        self.codex_path_entry.delete(0, "end")
        self.codex_path_entry.insert(0, c_cfg.get("custom_path", ""))
        self._toggle_codex_path()

        # General
        g_cfg = cfg.get("launcher", {})
        self.tray_var.set(g_cfg.get("minimize_to_tray_on_close", True))
        self.check_clash_var.set(g_cfg.get("check_clash_before_launch", True))
        self.start_minimized_var.set(g_cfg.get("start_minimized", False))

    def _toggle_anti_path(self):
        if self.anti_auto_var.get():
            self.anti_path_entry.configure(state="disabled", fg_color="#F2F2F2")
            self.anti_browse_btn.configure(state="disabled")
        else:
            self.anti_path_entry.configure(state="normal", fg_color="#FFFFFF")
            self.anti_browse_btn.configure(state="normal")

    def _toggle_codex_path(self):
        if self.codex_auto_var.get():
            self.codex_path_entry.configure(state="disabled", fg_color="#F2F2F2")
            self.codex_browse_btn.configure(state="disabled")
        else:
            self.codex_path_entry.configure(state="normal", fg_color="#FFFFFF")
            self.codex_browse_btn.configure(state="normal")

    def _browse_anti_path(self):
        file_path = filedialog.askopenfilename(
            parent=self,
            title="选择 Antigravity.exe",
            filetypes=[("Executable Files", "Antigravity.exe;*.exe"), ("All Files", "*.*")]
        )
        if file_path:
            self.anti_path_entry.delete(0, "end")
            self.anti_path_entry.insert(0, file_path)

    def _browse_codex_path(self):
        file_path = filedialog.askopenfilename(
            parent=self,
            title="选择 ChatGPT.exe (Codex)",
            filetypes=[("Executable Files", "ChatGPT.exe;*.exe"), ("All Files", "*.*")]
        )
        if file_path:
            self.codex_path_entry.delete(0, "end")
            self.codex_path_entry.insert(0, file_path)

    def _on_preset_selected(self, choice: str):
        for p in self.config_mgr.config.get("presets", []):
            if p["name"] == choice:
                self.host_entry.delete(0, "end")
                self.host_entry.insert(0, p.get("host", "127.0.0.1"))
                self.port_entry.delete(0, "end")
                self.port_entry.insert(0, str(p.get("port", 7890)))
                self._test_proxy_conn()
                break

    def _test_proxy_conn(self):
        host = self.host_entry.get().strip() or "127.0.0.1"
        try:
            port = int(self.port_entry.get().strip())
        except ValueError:
            self.test_status_label.configure(text="端口无效", text_color=COLOR_ERROR_TEXT)
            return

        self.test_btn.configure(state="disabled", text="测试中...")
        self.test_status_label.configure(text="连接中...", text_color=COLOR_TEXT_MUTED)

        def _run():
            reachable, latency, msg = probe_tcp_port(host, port, timeout_ms=1500)
            def _update():
                self.test_btn.configure(state="normal", text="测试连通")
                if reachable:
                    self.test_status_label.configure(
                        text=f"正常 ({latency} ms)",
                        text_color=COLOR_SUCCESS_TEXT
                    )
                else:
                    self.test_status_label.configure(
                        text=f"失败: {msg}",
                        text_color=COLOR_ERROR_TEXT
                    )
            self._dispatch_ui(_update)

            self._run_in_background(_run)

    def _scan_ports(self):
        host = self.host_entry.get().strip() or "127.0.0.1"
        self.scan_btn.configure(state="disabled", text="扫描中...")

        def _run():
            results = scan_available_proxies(host)
            def _update():
                self.scan_btn.configure(state="normal", text="自动探测端口")
                if results:
                    best = results[0]
                    self.host_entry.delete(0, "end")
                    self.host_entry.insert(0, best["host"])
                    self.port_entry.delete(0, "end")
                    self.port_entry.insert(0, str(best["port"]))
                    self.test_status_label.configure(
                        text=f"匹配: {best['name']} ({best['latency_ms']} ms)",
                        text_color=COLOR_SUCCESS_TEXT
                    )
                else:
                    self.test_status_label.configure(
                        text="未发现运行中的代理端口",
                        text_color=COLOR_WARN_TEXT
                    )
            self._dispatch_ui(_update)

            self._run_in_background(_run)

    def _reset_defaults(self):
        if messagebox.askyesno("确认恢复默认", "是否重置所有配置为默认值？", parent=self):
            self.config_mgr.reset_to_defaults()
            self._close()
            if self.on_save_callback:
                self.on_save_callback()

    def _save_config(self):
        try:
            port = int(self.port_entry.get().strip())
            if not (1 <= port <= 65535):
                raise ValueError()
        except ValueError:
            messagebox.showerror("格式错误", "端口必须在 1 到 65535 之间！", parent=self)
            return

        cfg = self.config_mgr.config
        cfg["proxy"]["host"] = self.host_entry.get().strip() or "127.0.0.1"
        cfg["proxy"]["port"] = port
        cfg["proxy"]["preset"] = self.preset_combo.get()
        cfg["proxy"]["bypass"] = self.bypass_entry.get().strip() or "localhost,127.0.0.1,::1"

        cfg["apps"]["antigravity"]["auto_detect"] = self.anti_auto_var.get()
        cfg["apps"]["antigravity"]["custom_path"] = self.anti_path_entry.get().strip()
        cfg["apps"]["antigravity"]["enable_browser_shim"] = self.anti_shim_var.get()

        cfg["apps"]["codex"]["auto_detect"] = self.codex_auto_var.get()
        cfg["apps"]["codex"]["custom_path"] = self.codex_path_entry.get().strip()

        cfg["launcher"]["minimize_to_tray_on_close"] = self.tray_var.get()
        cfg["launcher"]["check_clash_before_launch"] = self.check_clash_var.get()
        cfg["launcher"]["start_minimized"] = self.start_minimized_var.get()

        if self.config_mgr.save():
            if self.on_save_callback:
                self.on_save_callback()
            self._close()
        else:
            messagebox.showerror("保存失败", "无法保存配置文件到磁盘，请检查目录权限。", parent=self)
