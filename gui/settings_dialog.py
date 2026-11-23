import threading
from concurrent.futures import ThreadPoolExecutor
from tkinter import filedialog
import customtkinter as ctk
from typing import Callable, Optional

from core.config_manager import ConfigManager
from core.proxy_prober import probe_tcp_port, scan_available_proxies
from gui.dialog_base import DialogSurface, ask_confirmation, show_message
from gui.theme import *  # noqa: F401,F403 - shared design tokens
from gui.widgets import ActionButton, CrispCheckBox, PolishedComboBox

class SettingsDialog(DialogSurface):
    def __init__(
        self,
        parent,
        config_mgr: ConfigManager,
        on_save_callback: Optional[Callable[[], None]] = None,
        on_close_callback: Optional[Callable[[], None]] = None,
        worker_executor: Optional[ThreadPoolExecutor] = None,
    ):
        super().__init__(parent)
        self._ui_thread_id = threading.get_ident()
        self._worker_executor = worker_executor

        # Always reload the freshest configuration from disk every time opened
        self.config_mgr = config_mgr
        self.config_mgr.load()
        self.on_save_callback = on_save_callback
        self.on_close_callback = on_close_callback
        self._close_notified = False

        self.title("偏好设置 - Harness 代理启动")
        self.geometry("600x632")
        self.minsize(524, 420)
        self._build_ui()
        self._load_values()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda _event: self._close())
        self.center_over_parent(600, 632)
        self.show()

    def show(self):
        self.present()

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
        ctk.CTkLabel(
            self, text="偏好设置", anchor="w", text_color=COLOR_TEXT_HEADING,
            font=ctk.CTkFont(family=FONT_FAMILY, size=21, weight="bold"),
        ).pack(fill="x", padx=WINDOW_PADDING, pady=(16, SECTION_GAP))
        content = ctk.CTkScrollableFrame(
            self, fg_color=COLOR_BG, scrollbar_button_color=COLOR_SCROLLBAR,
            scrollbar_button_hover_color=COLOR_SCROLLBAR_HOVER,
        )
        content.pack(fill="both", expand=True, padx=(WINDOW_PADDING, 12))

        proxy = self._form_section(content, "代理网络")
        preset = self._form_row(proxy, "预设")
        self.preset_combo = PolishedComboBox(
            preset, values=[item["name"] for item in self.config_mgr.config.get("presets", [])],
            command=self._on_preset_selected, width=218,
        )
        self.preset_combo.pack(side="left", fill="x", expand=True)
        self.scan_btn = ActionButton(preset, text="自动探测端口", width=108, height=30, command=self._scan_ports)
        self.scan_btn.pack(side="right", padx=(8, 0))

        address = self._form_row(proxy, "主机")
        self.host_entry = self._form_entry(address, width=130)
        self.host_entry.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            address, text="端口", text_color=COLOR_TEXT_MUTED,
            font=ctk.CTkFont(family=FONT_FAMILY, size=12),
        ).pack(side="left", padx=(10, 6))
        self.port_entry = self._form_entry(address, width=66)
        self.port_entry.pack(side="left")
        self.test_btn = ActionButton(
            address, text="测试端口", width=90, height=30, command=self._test_proxy_conn,
        )
        self.test_btn.pack(side="right", padx=(8, 0))

        bypass = self._form_row(proxy, "绕过地址")
        self.bypass_entry = self._form_entry(bypass)
        self.bypass_entry.pack(side="left", fill="x", expand=True)
        self.test_status_label = ctk.CTkLabel(
            proxy, text="", anchor="w", justify="left", wraplength=415, height=18,
            text_color=COLOR_TEXT_MUTED, font=ctk.CTkFont(family=FONT_FAMILY, size=12),
        )
        self.test_status_label.pack(fill="x", padx=12, pady=(0, 8))

        antigravity = self._form_section(content, "Antigravity")
        self.anti_auto_var = ctk.BooleanVar(value=True)
        self.anti_path_entry, self.anti_browse_btn = self._path_fields(
            antigravity, self.anti_auto_var, self._toggle_anti_path, self._browse_anti_path,
        )
        self.anti_shim_var = ctk.BooleanVar(value=True)
        self._form_check(
            antigravity, "启用浏览器代理扩展（Node.js / npx）", self.anti_shim_var,
        ).pack(anchor="w", padx=12, pady=(0, 8))

        codex = self._form_section(content, "Codex (ChatGPT)")
        self.codex_auto_var = ctk.BooleanVar(value=True)
        self.codex_path_entry, self.codex_browse_btn = self._path_fields(
            codex, self.codex_auto_var, self._toggle_codex_path, self._browse_codex_path,
        )

        general = self._form_section(content, "启动与托盘")
        self.tray_var = ctk.BooleanVar(value=True)
        self.check_clash_var = ctk.BooleanVar(value=True)
        self.start_minimized_var = ctk.BooleanVar(value=False)
        for text, variable in (
            ("关闭窗口时最小化到托盘", self.tray_var),
            ("启动应用前检查代理端口", self.check_clash_var),
            ("启动本工具时隐藏到托盘", self.start_minimized_var),
        ):
            self._form_check(general, text, variable).pack(anchor="w", padx=12, pady=(0, 6))

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(fill="x", padx=WINDOW_PADDING, pady=(8, 16))
        ActionButton(footer, text="恢复默认", width=90, command=self._reset_defaults).pack(side="left")
        cancel = ActionButton(footer, text="取消", width=72, command=self._close)
        save = ActionButton(footer, text="保存", width=80, kind="primary", command=self._save_config)
        save.pack(side="right")
        cancel.pack(side="right", padx=(0, 8))

    def _form_section(self, parent, title):
        section = ctk.CTkFrame(
            parent, fg_color=COLOR_CARD_BG, corner_radius=CARD_RADIUS,
            border_width=1, border_color=COLOR_CARD_BORDER,
        )
        section.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(
            section, text=title, text_color=COLOR_TEXT_HEADING,
            font=ctk.CTkFont(family=FONT_FAMILY, size=14, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(8, 4))
        return section

    def _form_row(self, section, title):
        row = ctk.CTkFrame(section, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(0, 8))
        ctk.CTkLabel(
            row, text=title, width=66, anchor="w", text_color=COLOR_TEXT_MUTED,
            font=ctk.CTkFont(family=FONT_FAMILY, size=12),
        ).pack(side="left")
        return row

    def _form_entry(self, parent, **kwargs):
        entry = ctk.CTkEntry(
            parent, height=30, corner_radius=BUTTON_RADIUS, border_width=1,
            fg_color=COLOR_CARD_BG, border_color=COLOR_BORDER_STRONG, text_color=COLOR_TEXT_PRIMARY,
            font=ctk.CTkFont(family=FONT_FAMILY, size=12), **kwargs,
        )
        entry.bind("<FocusIn>", lambda _event: entry.configure(border_color=COLOR_FOCUS), add="+")
        entry.bind("<FocusOut>", lambda _event: entry.configure(border_color=COLOR_BORDER_STRONG), add="+")
        return entry

    def _form_check(self, parent, text, variable, command=None):
        return CrispCheckBox(
            parent, text=text, variable=variable, command=command,
            checkbox_width=16, checkbox_height=16, border_width=1,
            border_color=COLOR_BORDER_STRONG, fg_color=COLOR_ACCENT,
            hover_color=COLOR_ACCENT_HOVER, checkmark_color=COLOR_CARD_BG,
            text_color=COLOR_TEXT_PRIMARY, font=ctk.CTkFont(family=FONT_FAMILY, size=12),
        )

    def _path_fields(self, section, auto, toggle, browse):
        self._form_check(section, "自动查找安装", auto, toggle).pack(anchor="w", padx=12, pady=(0, 6))
        row = self._form_row(section, "安装路径")
        entry = self._form_entry(row)
        entry.pack(side="left", fill="x", expand=True)
        button = ActionButton(row, text="浏览…", width=70, height=30, command=browse)
        button.pack(side="right", padx=(8, 0))
        return entry, button

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
        self.preset_combo.set(preset_name if preset_name in self.preset_combo.cget("values") else "自定义")

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
            self.anti_path_entry.configure(state="disabled", fg_color=COLOR_INACTIVE_BG)
            self.anti_browse_btn.configure(state="disabled")
        else:
            self.anti_path_entry.configure(state="normal", fg_color=COLOR_CARD_BG)
            self.anti_browse_btn.configure(state="normal")

    def _toggle_codex_path(self):
        if self.codex_auto_var.get():
            self.codex_path_entry.configure(state="disabled", fg_color=COLOR_INACTIVE_BG)
            self.codex_browse_btn.configure(state="disabled")
        else:
            self.codex_path_entry.configure(state="normal", fg_color=COLOR_CARD_BG)
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
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            self.test_status_label.configure(text="端口应为 1 至 65535 之间的整数", text_color=COLOR_ERROR_TEXT)
            return

        self.test_btn.configure(state="disabled", text="测试中...")
        self.test_status_label.configure(text="连接中...", text_color=COLOR_TEXT_MUTED)

        def _run():
            reachable, latency, msg = probe_tcp_port(host, port, timeout_ms=1500)
            def _update():
                self.test_btn.configure(state="normal", text="测试端口")
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
                    preset = best["name"]
                    self.preset_combo.set(preset if preset in self.preset_combo.cget("values") else "自定义")
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
        if ask_confirmation(self, "恢复默认配置", "是否重置所有配置为默认值？"):
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
            self.test_status_label.configure(text="端口应为 1 至 65535 之间的整数", text_color=COLOR_ERROR_TEXT)
            self.port_entry.focus_set()
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
            show_message(self, "保存失败", "无法保存配置文件到磁盘，请检查目录权限。")
