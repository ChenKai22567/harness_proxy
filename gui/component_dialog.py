import threading
from tkinter import messagebox
from typing import Callable, Dict, Optional

import customtkinter as ctk

from core.config_manager import ConfigManager, get_resource_path
from core.launcher_engine import LauncherEngine


FONT_FAMILY = "Microsoft YaHei UI"
COLOR_BG = "#F3F3F3"
COLOR_CARD_BG = "#FFFFFF"
COLOR_CARD_SOFT = "#F7F7F7"
COLOR_CARD_HOVER = "#F0F0F0"
COLOR_BORDER = "#E1E1E1"
COLOR_BORDER_STRONG = "#C7C7C7"
COLOR_TEXT = "#242424"
COLOR_HEADING = "#1F1F1F"
COLOR_SECTION = "#3A3A3A"
COLOR_MUTED = "#666666"
COLOR_SUBTLE = "#8A8A8A"
COLOR_ACCENT = "#3B3B3B"
COLOR_ACCENT_HOVER = "#2F2F2F"
COLOR_ACCENT_SOFT = "#EEEEEE"
COLOR_SUCCESS_BG = "#E7F7F0"
COLOR_SUCCESS_TEXT = "#0A8055"
COLOR_WARN_BG = "#FFF7EB"
COLOR_WARN_TEXT = "#B45309"
COLOR_ERROR_BG = "#FDF0F1"
COLOR_ERROR_TEXT = "#C53041"
COLOR_ERROR_BORDER = "#F8CCD1"


APP_LABELS = {
    "antigravity": "Antigravity",
    "codex": "Codex (ChatGPT)",
}


class ComponentDialog(ctk.CTkToplevel):
    """Modeless process-role viewer and safe component policy editor."""

    _deactivate_windows_window_header_manipulation = True

    def __init__(
        self,
        parent,
        engine: LauncherEngine,
        config_mgr: ConfigManager,
        app_id: str = "codex",
        on_saved: Optional[Callable[[], None]] = None,
        on_close: Optional[Callable[[], None]] = None,
    ):
        super().__init__(parent)
        self.withdraw()
        self._parent = parent
        self.engine = engine
        self.config_mgr = config_mgr
        self.app_id = app_id if app_id in APP_LABELS else "codex"
        self.on_saved = on_saved
        self.on_close = on_close
        self._ui_thread_id = threading.get_ident()
        self._refreshing = False
        self._refresh_requested = False
        self._closed = False
        self._snapshot: Dict = {}

        self.title("组件与子进程 - Harness代理启动")
        self.geometry("760x680")
        self.minsize(720, 620)
        self.configure(fg_color=COLOR_BG)
        self.transient(parent)
        icon_path = get_resource_path("assets", "icon.ico")
        try:
            self.iconbitmap(icon_path)
        except Exception:
            pass

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda _event: self._close())
        self.update_idletasks()
        self._center_over_parent()
        self.show()
        self._refresh_async()

    def _center_over_parent(self):
        width, height = 760, 680
        try:
            physical_width = self._apply_window_scaling(width)
            physical_height = self._apply_window_scaling(height)
            parent_y = self._parent.winfo_y()
            title_height = max(0, self._parent.winfo_rooty() - parent_y)
            x = self._parent.winfo_x() + (self._parent.winfo_width() - physical_width) // 2
            # Keep the secondary workspace's top slightly above the main
            # window.  Center-relative offsets are misleading when the two
            # windows have different heights.
            y = parent_y - 24
            x = max(0, min(x, self.winfo_screenwidth() - physical_width))
            y = max(0, min(y, self.winfo_screenheight() - physical_height - title_height))
            self.geometry(f"{width}x{height}+{x}+{y}")
        except Exception:
            pass

    def show(self, app_id: Optional[str] = None):
        if app_id in APP_LABELS and app_id != self.app_id:
            self._switch_app(app_id)
        try:
            self.deiconify()
            self.state("normal")
            self.lift()
            self.focus_force()
        except Exception:
            pass

    def _dispatch_ui(self, callback):
        if self._closed:
            return
        dispatch = getattr(self._parent, "_dispatch_ui", None)
        if callable(dispatch):
            dispatch(lambda: callback() if self.winfo_exists() else None)

    def _build_ui(self):
        header = ctk.CTkFrame(
            self, corner_radius=8, fg_color=COLOR_CARD_BG,
            border_width=1, border_color=COLOR_BORDER,
        )
        header.pack(fill="x", padx=14, pady=(12, 6))
        title_row = ctk.CTkFrame(header, fg_color="transparent")
        title_row.pack(fill="x", padx=14, pady=(10, 4))
        ctk.CTkLabel(
            title_row, text="组件与子进程", text_color=COLOR_HEADING,
            font=ctk.CTkFont(family=FONT_FAMILY, size=17, weight="bold"),
        ).pack(side="left")
        ctk.CTkLabel(
            title_row, text="按功能聚合 · 仅控制受管会话", text_color=COLOR_MUTED,
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
        ).pack(side="left", padx=10)

        self.app_selector = ctk.CTkSegmentedButton(
            header,
            values=[APP_LABELS["antigravity"], APP_LABELS["codex"]],
            command=self._on_app_selected,
            height=32,
            corner_radius=6,
            fg_color=COLOR_CARD_SOFT,
            # CTkSegmentedButton has one shared text colour. A pale selected
            # surface keeps both selected and unselected labels readable.
            selected_color="#E5E5E5",
            selected_hover_color=COLOR_ACCENT_SOFT,
            unselected_color="#FFFFFF",
            unselected_hover_color=COLOR_CARD_HOVER,
            text_color=COLOR_SECTION,
            text_color_disabled=COLOR_SUBTLE,
            font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold"),
        )
        self.app_selector.pack(fill="x", padx=14, pady=(2, 10))
        self.app_selector.set(APP_LABELS[self.app_id])

        summary = ctk.CTkFrame(
            self, corner_radius=8, fg_color=COLOR_CARD_BG,
            border_width=1, border_color=COLOR_BORDER,
        )
        summary.pack(fill="x", padx=14, pady=5)
        summary_row = ctk.CTkFrame(summary, fg_color="transparent")
        summary_row.pack(fill="x", padx=14, pady=(9, 4))
        self.summary_title = ctk.CTkLabel(
            summary_row, text="正在读取进程…", anchor="w", text_color=COLOR_SECTION,
            font=ctk.CTkFont(family=FONT_FAMILY, size=13, weight="bold"),
        )
        self.summary_title.pack(side="left", fill="x", expand=True)
        self.summary_badge = ctk.CTkLabel(
            summary_row, text="扫描中", width=92, corner_radius=5,
            fg_color=COLOR_WARN_BG, text_color=COLOR_WARN_TEXT,
            font=ctk.CTkFont(family=FONT_FAMILY, size=11, weight="bold"),
        )
        self.summary_badge.pack(side="right")
        self.summary_detail = ctk.CTkLabel(
            summary, text="进程详情只在本窗口打开时采样，不增加后台开销。",
            anchor="w", justify="left", text_color=COLOR_MUTED,
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
        )
        self.summary_detail.pack(fill="x", padx=14, pady=(0, 9))

        policy = ctk.CTkFrame(
            self, corner_radius=8, fg_color=COLOR_CARD_BG,
            border_width=1, border_color=COLOR_BORDER,
        )
        policy.pack(fill="x", padx=14, pady=5)
        ctk.CTkLabel(
            policy, text="启动策略", anchor="w", text_color=COLOR_SECTION,
            font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold"),
        ).pack(fill="x", padx=14, pady=(8, 3))
        policy_row = ctk.CTkFrame(policy, fg_color="transparent")
        policy_row.pack(fill="x", padx=14, pady=(2, 9))
        self.batch_var = ctk.BooleanVar(value=True)
        self.batch_switch = self._make_switch(
            policy_row, "纳入一键启动", self.batch_var,
            "决定批量启动是否包含此应用",
        )
        self.batch_switch.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.shim_var = ctk.BooleanVar(value=True)
        self.secondary_switch = self._make_switch(
            policy_row, "浏览器代理补丁", self.shim_var,
            "Antigravity 下次启动生效",
        )
        self.secondary_switch.pack(side="left", fill="x", expand=True, padx=(8, 0))

        list_header = ctk.CTkFrame(self, fg_color="transparent")
        list_header.pack(fill="x", padx=18, pady=(7, 2))
        ctk.CTkLabel(
            list_header, text="运行组件", text_color=COLOR_SECTION,
            font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold"),
        ).pack(side="left")
        self.refresh_btn = ctk.CTkButton(
            list_header, text="刷新", width=64, height=26, corner_radius=6,
            fg_color="#FFFFFF", text_color=COLOR_SECTION,
            hover_color=COLOR_CARD_HOVER, border_width=1, border_color=COLOR_BORDER,
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            command=self._refresh_async,
        )
        self.refresh_btn.pack(side="right")

        self.component_list = ctk.CTkScrollableFrame(
            self, corner_radius=8, fg_color=COLOR_CARD_SOFT,
            border_width=1, border_color=COLOR_BORDER,
            scrollbar_button_color="#C7C7C7",
            scrollbar_button_hover_color="#A8A8A8",
        )
        self.component_list.pack(fill="both", expand=True, padx=14, pady=(2, 6))

        action_bar = ctk.CTkFrame(self, fg_color="transparent")
        action_bar.pack(fill="x", padx=14, pady=(2, 12))
        self.action_status = ctk.CTkLabel(
            action_bar, text="", text_color=COLOR_MUTED, anchor="w",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
        )
        self.action_status.pack(side="left", fill="x", expand=True)
        self.adopt_btn = ctk.CTkButton(
            action_bar, text="接管当前会话", width=105, height=30, corner_radius=6,
            fg_color=COLOR_WARN_BG, text_color=COLOR_WARN_TEXT,
            hover_color="#FDECCF", border_width=1, border_color="#F6D59F",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            command=self._adopt_current,
        )
        self.adopt_btn.pack(side="right", padx=4)
        self.stop_btn = ctk.CTkButton(
            action_bar, text="停止受管会话", width=105, height=30, corner_radius=6,
            fg_color=COLOR_ERROR_BG, text_color=COLOR_ERROR_TEXT,
            hover_color="#FCE1E4", border_width=1, border_color=COLOR_ERROR_BORDER,
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
            command=self._stop_managed,
        )
        self.stop_btn.pack(side="right", padx=4)
        self.save_btn = ctk.CTkButton(
            action_bar, text="保存策略", width=84, height=30, corner_radius=6,
            fg_color=COLOR_ACCENT, text_color="#FFFFFF", hover_color=COLOR_ACCENT_HOVER,
            font=ctk.CTkFont(family=FONT_FAMILY, size=11, weight="bold"),
            command=self._save_policies,
        )
        self.save_btn.pack(side="right", padx=4)

        self.gpu_var = ctk.BooleanVar(value=True)
        self._load_policies()

    def _make_switch(self, parent, title, variable, description):
        frame = ctk.CTkFrame(parent, fg_color=COLOR_CARD_SOFT, corner_radius=6)
        switch = ctk.CTkSwitch(
            frame, text=title, variable=variable, progress_color=COLOR_ACCENT,
            button_color="#FFFFFF", button_hover_color="#FFFFFF",
            fg_color=COLOR_BORDER_STRONG, text_color=COLOR_TEXT,
            font=ctk.CTkFont(family=FONT_FAMILY, size=11, weight="bold"),
        )
        switch.pack(anchor="w", padx=10, pady=(7, 1))
        ctk.CTkLabel(
            frame, text=description, anchor="w", text_color=COLOR_MUTED,
            font=ctk.CTkFont(family=FONT_FAMILY, size=10),
        ).pack(fill="x", padx=10, pady=(0, 6))
        return frame

    def _on_app_selected(self, label: str):
        app_id = next((key for key, value in APP_LABELS.items() if value == label), "codex")
        self._switch_app(app_id)

    def _switch_app(self, app_id: str):
        self.app_id = app_id
        self.app_selector.set(APP_LABELS[app_id])
        self._load_policies()
        self._refresh_async()

    def _load_policies(self):
        config = self.config_mgr.config.get("apps", {}).get(self.app_id, {})
        self.batch_var.set(config.get("enabled_in_batch", True))
        self.gpu_var.set(config.get("components", {}).get("gpu_acceleration", True))
        if self.app_id == "antigravity":
            self.shim_var.set(config.get("enable_browser_shim", True))
            switch = self.secondary_switch.winfo_children()[0]
            label = self.secondary_switch.winfo_children()[1]
            switch.configure(text="浏览器代理补丁", state="normal")
            label.configure(text="Antigravity 下次启动生效")
        else:
            self.shim_var.set(True)
            switch = self.secondary_switch.winfo_children()[0]
            label = self.secondary_switch.winfo_children()[1]
            switch.configure(text="API / WebSocket 代理", state="disabled")
            label.configure(text="必要组件，始终随 Codex 启动")

    def _save_policies(self):
        app = self.config_mgr.config.setdefault("apps", {}).setdefault(self.app_id, {})
        app["enabled_in_batch"] = bool(self.batch_var.get())
        app.setdefault("components", {})["gpu_acceleration"] = bool(self.gpu_var.get())
        if self.app_id == "antigravity":
            app["enable_browser_shim"] = bool(self.shim_var.get())
        else:
            app["components"]["websocket_proxy"] = True
        if self.config_mgr.save():
            self.action_status.configure(text="策略已保存；GPU 设置在下次启动生效。", text_color=COLOR_SUCCESS_TEXT)
            if self.on_saved:
                self.on_saved()
        else:
            self.action_status.configure(text="保存失败，请检查配置目录权限。", text_color=COLOR_ERROR_TEXT)

    def _refresh_async(self):
        if self._closed:
            return
        if self._refreshing:
            self._refresh_requested = True
            return
        self._refreshing = True
        self._refresh_requested = False
        self.refresh_btn.configure(state="disabled", text="读取中")
        requested_app = self.app_id

        def worker():
            try:
                snapshot = self.engine.get_component_snapshot(requested_app)
                self._dispatch_ui(lambda: self._apply_snapshot(snapshot) if requested_app == self.app_id else None)
            except Exception as exc:
                self._dispatch_ui(
                    lambda: self._show_refresh_error(str(exc)) if requested_app == self.app_id else None
                )
            finally:
                self._dispatch_ui(self._finish_refresh)

        threading.Thread(target=worker, daemon=True, name="component-snapshot").start()

    def _finish_refresh(self):
        self._refreshing = False
        self.refresh_btn.configure(state="normal", text="刷新")
        if self._refresh_requested and not self._closed:
            self._refresh_requested = False
            self._refresh_async()

    def _show_refresh_error(self, message: str):
        self.summary_title.configure(text="无法读取进程状态")
        self.summary_badge.configure(text="读取失败", fg_color=COLOR_ERROR_BG, text_color=COLOR_ERROR_TEXT)
        self.summary_detail.configure(text=message)

    def _apply_snapshot(self, snapshot: Dict):
        self._snapshot = snapshot
        app_label = APP_LABELS.get(self.app_id, self.app_id)
        count = snapshot.get("process_count", 0)
        memory = snapshot.get("memory_mb", 0.0)
        owned = snapshot.get("owned_count", 0)
        external = snapshot.get("external_count", 0)
        residual_pids = snapshot.get("residual_pids", [])

        self.summary_title.configure(text=f"{app_label} · {count} 个进程 · {memory:.1f} MiB")
        if not snapshot.get("running") and residual_pids:
            self.summary_badge.configure(text="已退出 · 有残留", fg_color=COLOR_WARN_BG, text_color=COLOR_WARN_TEXT)
            detail = f"主进程已经退出，检测到 {len(residual_pids)} 个存活辅助进程；它们不会阻止重新启动，可在下方安全清理。"
        elif not snapshot.get("running"):
            self.summary_badge.configure(text="未运行", fg_color=COLOR_CARD_SOFT, text_color=COLOR_SUBTLE)
            detail = "当前没有检测到存活的应用进程；保存的启动策略会在下次启动时应用。"
        elif owned:
            self.summary_badge.configure(text="受管会话", fg_color=COLOR_SUCCESS_BG, text_color=COLOR_SUCCESS_TEXT)
            detail = f"启动器管理 {owned} 个进程；另有 {external} 个外部或残留进程保持只读。"
            if residual_pids:
                detail += f" 其中 {len(residual_pids)} 个可单独清理。"
        else:
            self.summary_badge.configure(text="外部实例", fg_color=COLOR_WARN_BG, text_color=COLOR_WARN_TEXT)
            detail = f"检测到 {external} 个进程，但尚未建立会话归属；执行停止或重启时会先请求确认并接管根进程。"
        self.summary_detail.configure(text=detail)

        for child in self.component_list.winfo_children():
            child.destroy()
        for component in snapshot.get("components", []):
            self._build_component_row(component)

        if snapshot.get("external_root_pids"):
            self.adopt_btn.configure(text="接管当前会话", state="normal")
        elif residual_pids:
            self.adopt_btn.configure(text="清理残留进程", state="normal")
        else:
            self.adopt_btn.configure(text="接管当前会话", state="disabled")
        external_roots = snapshot.get("external_root_pids", [])
        if owned:
            self.stop_btn.configure(text="停止受管会话", state="normal")
        elif external_roots:
            self.stop_btn.configure(text="接管并停止", state="normal")
        else:
            self.stop_btn.configure(text="停止受管会话", state="disabled")

    def _build_component_row(self, component: Dict):
        running = bool(component.get("running"))
        residual_count = int(component.get("residual_count", 0))
        present = bool(component.get("present", running or residual_count))
        row = ctk.CTkFrame(
            self.component_list, corner_radius=6,
            fg_color=COLOR_CARD_BG if present else "#FAFAFA",
            border_width=1, border_color=COLOR_BORDER,
        )
        row.pack(fill="x", padx=4, pady=4)

        if component.get("control") == "policy":
            control = ctk.CTkCheckBox(
                row, text="", width=24, variable=self.gpu_var,
                checkbox_width=18, checkbox_height=18, corner_radius=3,
                border_width=1, border_color=COLOR_BORDER_STRONG,
                fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER,
            )
        else:
            state_var = ctk.BooleanVar(value=present)
            control = ctk.CTkCheckBox(
                row, text="", width=24, variable=state_var, state="disabled",
                checkbox_width=18, checkbox_height=18, corner_radius=3,
                border_width=1, border_color=COLOR_BORDER_STRONG,
                fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER,
            )
        control.pack(side="left", padx=(10, 4))

        text_frame = ctk.CTkFrame(row, fg_color="transparent")
        text_frame.pack(side="left", fill="x", expand=True, padx=4, pady=7)
        ctk.CTkLabel(
            text_frame, text=component.get("label", "未知组件"), anchor="w",
            text_color=COLOR_TEXT if present else COLOR_MUTED,
            font=ctk.CTkFont(family=FONT_FAMILY, size=11, weight="bold"),
        ).pack(fill="x")
        mode_note = {
            "required": "应用必需",
            "policy": "下次启动生效",
            "observe": "仅观察",
        }.get(component.get("control"), "仅观察")
        ctk.CTkLabel(
            text_frame,
            text=f"{component.get('description', '')} · {mode_note}",
            anchor="w", text_color=COLOR_MUTED,
            font=ctk.CTkFont(family=FONT_FAMILY, size=10),
        ).pack(fill="x", pady=(1, 0))

        pids = component.get("pids", [])
        pid_text = "PID " + ", ".join(str(pid) for pid in pids[:5]) if pids else "—"
        if len(pids) > 5:
            pid_text += f" 等 {len(pids)} 个"
        ctk.CTkLabel(
            row, text=pid_text, width=150, anchor="e",
            text_color=COLOR_MUTED, font=ctk.CTkFont(family="Consolas", size=9),
        ).pack(side="right", padx=(4, 10))
        if running:
            badge_text = f"运行中 · {component.get('active_count', component.get('count', 0))}"
            badge_bg, badge_fg = COLOR_SUCCESS_BG, COLOR_SUCCESS_TEXT
        elif residual_count:
            badge_text = f"残留 · {residual_count}"
            badge_bg, badge_fg = COLOR_WARN_BG, COLOR_WARN_TEXT
        else:
            badge_text = "未运行"
            badge_bg, badge_fg = COLOR_CARD_SOFT, COLOR_SUBTLE
        ctk.CTkLabel(
            row, text=badge_text, width=74, corner_radius=5,
            fg_color=badge_bg,
            text_color=badge_fg,
            font=ctk.CTkFont(family=FONT_FAMILY, size=10, weight="bold"),
        ).pack(side="right", padx=4)

    def _adopt_current(self):
        roots = self._snapshot.get("roots", [])
        external = [item for item in roots if not item.get("owned")]
        if not external:
            residual_pids = self._snapshot.get("residual_pids", [])
            if not residual_pids:
                return
            label = APP_LABELS.get(self.app_id, self.app_id)
            if not messagebox.askyesno(
                "清理残留进程",
                f"{label} 主进程已经退出。\n\n将只结束检测到的 {len(residual_pids)} 个孤立辅助进程，不会影响其他活动会话。是否继续？",
                parent=self,
            ):
                return
            self._run_action(lambda: self.engine.cleanup_residuals(self.app_id))
            return
        target = max(external, key=lambda item: item.get("started_at", 0.0))
        pid = int(target["pid"])
        if not messagebox.askyesno(
            "接管当前会话",
            f"将 PID {pid} 标记为启动器受管根进程。\n\n之后“停止/重启”可以结束该进程及其子进程。是否继续？",
            parent=self,
        ):
            return
        self._run_action(lambda: self.engine.adopt_session(self.app_id, pid))

    def _stop_managed(self):
        label = APP_LABELS.get(self.app_id, self.app_id)
        roots = self._snapshot.get("roots", [])
        external = [item for item in roots if not item.get("owned")]
        if not self._snapshot.get("managed_root_pids") and external:
            target = max(external, key=lambda item: item.get("started_at", 0.0))
            pid = int(target["pid"])
            if not messagebox.askyesno(
                f"接管并停止 {label}",
                f"将先验证并接管根进程 PID {pid}，再仅结束该根进程及其实际子进程。\n\n是否继续？",
                parent=self,
            ):
                return
            self._run_action(lambda: self.engine.adopt_and_stop(self.app_id, pid))
            return
        if not messagebox.askyesno(
            "停止受管会话",
            f"将结束启动器已登记的 {label} 根进程及其子进程。\n外部会话不会被结束。是否继续？",
            parent=self,
        ):
            return
        action = self.engine.stop_codex if self.app_id == "codex" else self.engine.stop_antigravity
        self._run_action(action)

    def _run_action(self, action):
        self.action_status.configure(text="正在执行…", text_color=COLOR_MUTED)
        self.adopt_btn.configure(state="disabled")
        self.stop_btn.configure(state="disabled")

        def worker():
            try:
                ok, message = action()
            except Exception as exc:
                ok, message = False, str(exc)
            self._dispatch_ui(lambda: self._after_action(ok, message))

        threading.Thread(target=worker, daemon=True, name="component-action").start()

    def _after_action(self, ok: bool, message: str):
        self.action_status.configure(
            text=message,
            text_color=COLOR_SUCCESS_TEXT if ok else COLOR_ERROR_TEXT,
        )
        self._refresh_async()

    def _close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.destroy()
        finally:
            if self.on_close:
                try:
                    self.on_close()
                except Exception:
                    pass
