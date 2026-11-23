import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, Optional

import customtkinter as ctk

from core.config_manager import ConfigManager
from core.launcher_engine import LauncherEngine
from gui.dialog_base import DialogSurface, ask_confirmation, replace_content
from gui.theme import *  # noqa: F401,F403 - shared design tokens
from gui.widgets import ActionButton, CrispCheckBox, PolishedComboBox

# Short local aliases over the shared tokens; the values themselves live in
# gui/theme.py only.
COLOR_BORDER = COLOR_CARD_BORDER
COLOR_TEXT = COLOR_TEXT_PRIMARY
COLOR_HEADING = COLOR_TEXT_HEADING
COLOR_SECTION = COLOR_TEXT_SECTION
COLOR_MUTED = COLOR_TEXT_MUTED
COLOR_SUBTLE = COLOR_TEXT_SUBTLE


APP_LABELS = {
    "antigravity": "Antigravity",
    "codex": "Codex (ChatGPT)",
}


class ComponentDialog(DialogSurface):
    """Modeless process-role viewer and safe component policy editor."""

    def __init__(
        self,
        parent,
        engine: LauncherEngine,
        config_mgr: ConfigManager,
        app_id: str = "codex",
        on_saved: Optional[Callable[[], None]] = None,
        on_close: Optional[Callable[[], None]] = None,
        worker_executor: Optional[ThreadPoolExecutor] = None,
    ):
        super().__init__(parent)
        self.engine = engine
        self.config_mgr = config_mgr
        self.app_id = app_id if app_id in APP_LABELS else "codex"
        self.on_saved = on_saved
        self.on_close = on_close
        self._ui_thread_id = threading.get_ident()
        self._worker_executor = worker_executor
        self._refreshing = False
        self._refresh_requested = False
        self._closed = False
        self._snapshot: Dict = {}
        self._component_view = None
        self._view_generation = 0

        self.title("组件与子进程 - Harness 代理启动")
        self.geometry("724x624")
        self.minsize(680, 560)
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda _event: self._close())
        self.center_over_parent(724, 624)
        self.show()
        self._refresh_async()

    def show(self, app_id: Optional[str] = None):
        if app_id in APP_LABELS and app_id != self.app_id:
            self._switch_app(app_id)
        self.present()

    def _dispatch_ui(self, callback):
        if self._closed:
            return
        dispatch = getattr(self._parent, "_dispatch_ui", None)
        if callable(dispatch):
            dispatch(lambda: callback() if self.winfo_exists() else None)

    def _build_ui(self):
        title_row = ctk.CTkFrame(self, fg_color="transparent")
        title_row.pack(fill="x", padx=WINDOW_PADDING, pady=(16, SECTION_GAP))
        ctk.CTkLabel(
            title_row, text="组件与子进程", text_color=COLOR_HEADING,
            font=ctk.CTkFont(family=FONT_FAMILY, size=21, weight="bold"),
        ).pack(side="left")

        self.app_selector = PolishedComboBox(
            title_row, values=list(APP_LABELS.values()), command=self._on_app_selected, width=182,
        )
        self.app_selector.pack(side="right")
        self.app_selector.set(APP_LABELS[self.app_id])

        summary = ctk.CTkFrame(
            self, corner_radius=CARD_RADIUS, fg_color=COLOR_CARD_BG,
            border_width=1, border_color=COLOR_BORDER,
        )
        summary.pack(fill="x", padx=WINDOW_PADDING, pady=(0, 8))
        summary_row = ctk.CTkFrame(summary, fg_color="transparent")
        summary_row.pack(fill="x", padx=12, pady=(7, 3))
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
            summary, text="",
            anchor="w", justify="left", text_color=COLOR_MUTED,
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
        )
        self.summary_detail.pack(fill="x", padx=12, pady=(0, 7))

        policy = ctk.CTkFrame(
            self, corner_radius=CARD_RADIUS, fg_color=COLOR_CARD_BG,
            border_width=1, border_color=COLOR_BORDER,
        )
        policy.pack(fill="x", padx=WINDOW_PADDING, pady=(0, 8))
        ctk.CTkLabel(
            policy, text="启动策略", anchor="w", text_color=COLOR_SECTION,
            font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold"),
        ).pack(fill="x", padx=12, pady=(7, 3))
        policy_row = ctk.CTkFrame(policy, fg_color="transparent")
        policy_row.pack(fill="x", padx=12, pady=(2, 7))
        self.batch_var = ctk.BooleanVar(value=True)
        self.batch_switch, _, _ = self._make_switch(
            policy_row, "纳入一键启动", self.batch_var,
            "决定批量启动是否包含此应用",
        )
        self.batch_switch.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.shim_var = ctk.BooleanVar(value=True)
        self.secondary_switch, self._secondary_switch, self._secondary_switch_label = self._make_switch(
            policy_row, "浏览器代理补丁", self.shim_var,
            "Antigravity 下次启动生效",
        )
        self.secondary_switch.pack(side="left", fill="x", expand=True, padx=(8, 0))

        list_header = ctk.CTkFrame(self, fg_color="transparent")
        list_header.pack(fill="x", padx=WINDOW_PADDING, pady=(0, 6))
        ctk.CTkLabel(
            list_header, text="运行组件", text_color=COLOR_SECTION,
            font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold"),
        ).pack(side="left")
        self.refresh_btn = ActionButton(
            list_header, text="刷新", width=64, height=28, command=self._refresh_async,
        )
        self.refresh_btn.pack(side="right")

        self.component_list = ctk.CTkScrollableFrame(
            self, corner_radius=CARD_RADIUS, fg_color=COLOR_CARD_SOFT,
            border_width=1, border_color=COLOR_BORDER,
            scrollbar_button_color=COLOR_SCROLLBAR,
            scrollbar_button_hover_color=COLOR_SCROLLBAR_HOVER,
        )
        self.component_list.pack(fill="both", expand=True, padx=WINDOW_PADDING, pady=(0, 8))

        action_bar = ctk.CTkFrame(self, fg_color="transparent")
        action_bar.pack(fill="x", padx=WINDOW_PADDING, pady=(0, 16))
        self.action_status = ctk.CTkLabel(
            action_bar, text="", text_color=COLOR_MUTED, anchor="w",
            font=ctk.CTkFont(family=FONT_FAMILY, size=11),
        )
        self.action_status.pack(side="left", fill="x", expand=True)
        self.adopt_btn = ActionButton(
            action_bar, text="接管当前会话", width=105, state="disabled",
            command=self._adopt_current,
        )
        self.stop_btn = ActionButton(
            action_bar, text="停止受管会话", width=105, kind="danger", state="disabled",
            command=self._stop_managed,
        )
        self.save_btn = ActionButton(
            action_bar, text="保存策略", width=84, kind="primary",
            command=self._save_policies,
        )
        self.save_btn.pack(side="right")
        self.stop_btn.pack(side="right", padx=8)
        self.adopt_btn.pack(side="right")

        self.gpu_var = ctk.BooleanVar(value=True)
        self._load_policies()

    def _make_switch(self, parent, title, variable, description):
        """Return (frame, switch, label) so callers never depend on the
        widget packing order inside the frame."""
        frame = ctk.CTkFrame(parent, fg_color=COLOR_CARD_SOFT, corner_radius=6)
        switch = CrispCheckBox(
            frame, text=title, variable=variable,
            checkbox_width=16, checkbox_height=16, border_width=1,
            fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER, border_color=COLOR_BORDER_STRONG,
            checkmark_color=COLOR_CARD_BG, text_color=COLOR_TEXT,
            font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold"),
        )
        switch.pack(anchor="w", padx=10, pady=(7, 1))
        label = ctk.CTkLabel(
            frame, text=description, anchor="w", text_color=COLOR_MUTED,
            font=ctk.CTkFont(family=FONT_FAMILY, size=10),
        )
        label.pack(fill="x", padx=10, pady=(0, 6))
        return frame, switch, label

    def _on_app_selected(self, label: str):
        app_id = next((key for key, value in APP_LABELS.items() if value == label), "codex")
        if app_id != self.app_id:
            self._switch_app(app_id)

    def _switch_app(self, app_id: str):
        self.app_id = app_id
        self._view_generation += 1
        self._snapshot = {}
        self._replace_component_rows(())
        self.app_selector.set(APP_LABELS[app_id])
        self._load_policies()
        self.summary_title.configure(text=f"{APP_LABELS[app_id]} · 正在读取")
        self.summary_badge.configure(text="读取中", fg_color=COLOR_INACTIVE_BG, text_color=COLOR_MUTED)
        self.summary_detail.configure(text="")
        self.action_status.configure(text="")
        self.adopt_btn.configure(state="disabled")
        self.stop_btn.configure(state="disabled")
        self._refresh_async()

    def _load_policies(self):
        config = self.config_mgr.config.get("apps", {}).get(self.app_id, {})
        self.batch_var.set(config.get("enabled_in_batch", True))
        self.gpu_var.set(config.get("components", {}).get("gpu_acceleration", True))
        if self.app_id == "antigravity":
            self.shim_var.set(config.get("enable_browser_shim", True))
            self._secondary_switch.configure(text="浏览器代理补丁", state="normal")
            self._secondary_switch_label.configure(text="Antigravity 下次启动生效")
        else:
            self.shim_var.set(True)
            self._secondary_switch.configure(text="API / WebSocket 代理", state="disabled")
            self._secondary_switch_label.configure(text="必要组件，始终随 Codex 启动")

    def _save_policies(self):
        app = self.config_mgr.config.setdefault("apps", {}).setdefault(self.app_id, {})
        app["enabled_in_batch"] = bool(self.batch_var.get())
        app.setdefault("components", {})["gpu_acceleration"] = bool(self.gpu_var.get())
        if self.app_id == "antigravity":
            app["enable_browser_shim"] = bool(self.shim_var.get())
        else:
            app["components"]["websocket_proxy"] = True
        if self.config_mgr.save():
            self.action_status.configure(text="策略已保存，GPU 设置将在下次启动时生效。", text_color=COLOR_SUCCESS_TEXT)
            if self.on_saved:
                self.on_saved()
        else:
            self.action_status.configure(text="保存失败，请检查配置目录权限。", text_color=COLOR_ERROR_TEXT)

    def _run_in_background(self, worker):
        """Sample on the shared launcher executor; fall back to a bare thread."""
        if self._worker_executor is not None:
            try:
                self._worker_executor.submit(worker)
                return
            except RuntimeError:
                pass
        threading.Thread(target=worker, daemon=True, name="component-worker").start()

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
        generation = self._view_generation

        def apply_if_current(callback):
            self._dispatch_ui(lambda: callback() if generation == self._view_generation else None)

        def worker():
            try:
                snapshot = self.engine.get_component_snapshot(requested_app)
                apply_if_current(lambda: self._apply_snapshot(snapshot))
            except Exception as exc:
                message = str(exc)
                apply_if_current(lambda: self._show_refresh_error(message))
            finally:
                self._dispatch_ui(self._finish_refresh)

        self._run_in_background(worker)

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
        self._replace_component_rows(snapshot.get("components", []))
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
            detail = f"主进程已退出，可清理 {len(residual_pids)} 个残留进程。"
        elif not snapshot.get("running"):
            self.summary_badge.configure(text="未运行", fg_color=COLOR_CARD_SOFT, text_color=COLOR_SUBTLE)
            detail = "启动策略将在应用下次启动时生效。"
        elif owned:
            self.summary_badge.configure(text="受管会话", fg_color=COLOR_SUCCESS_BG, text_color=COLOR_SUCCESS_TEXT)
            detail = f"启动器管理 {owned} 个进程；另有 {external} 个外部或残留进程保持只读。"
            if residual_pids:
                detail += f" 其中 {len(residual_pids)} 个可单独清理。"
        else:
            self.summary_badge.configure(text="外部实例", fg_color=COLOR_WARN_BG, text_color=COLOR_WARN_TEXT)
            detail = f"检测到 {external} 个外部进程。停止或重启前需确认接管。"
        self.summary_detail.configure(text=detail)

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

    def _replace_component_rows(self, components):
        view = ctk.CTkFrame(self.component_list, fg_color="transparent")
        for component in components:
            self._build_component_row(view, component)
        replace_content(self._component_view, view)
        self._component_view = view

    def _build_component_row(self, parent, component: Dict):
        running = bool(component.get("running"))
        residual_count = int(component.get("residual_count", 0))
        present = bool(component.get("present", running or residual_count))
        row = ctk.CTkFrame(
            parent, corner_radius=CARD_RADIUS,
            fg_color=COLOR_CARD_BG if present else COLOR_CARD_SOFT,
            border_width=1, border_color=COLOR_BORDER,
        )
        row.pack(fill="x", padx=4, pady=3)

        if component.get("control") == "policy":
            control = CrispCheckBox(
                row, text="", width=24, variable=self.gpu_var,
                checkbox_width=16, checkbox_height=16,
                border_width=1, border_color=COLOR_BORDER_STRONG,
                fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER,
            )
        else:
            state_var = ctk.BooleanVar(value=present)
            control = CrispCheckBox(
                row, text="", width=24, variable=state_var, state="disabled",
                checkbox_width=16, checkbox_height=16,
                border_width=1, border_color=COLOR_BORDER_STRONG,
                fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER,
            )
        control.pack(side="left", padx=(10, 4))

        text_frame = ctk.CTkFrame(row, fg_color="transparent")
        text_frame.pack(side="left", fill="x", expand=True, padx=4, pady=6)
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
        app_id = self.app_id
        roots = self._snapshot.get("roots", [])
        external = [item for item in roots if not item.get("owned")]
        if not external:
            residual_pids = self._snapshot.get("residual_pids", [])
            if not residual_pids:
                return
            label = APP_LABELS[app_id]
            if not ask_confirmation(
                self, "清理残留进程",
                f"{label} 主进程已退出。将结束检测到的 {len(residual_pids)} 个残留辅助进程。是否继续？",
            ):
                return
            self._run_action(lambda: self.engine.cleanup_residuals(app_id))
            return
        target = max(external, key=lambda item: item.get("started_at", 0.0))
        pid = int(target["pid"])
        if not ask_confirmation(
            self, "接管当前会话",
            f"将 PID {pid} 标记为启动器受管根进程。\n\n之后“停止/重启”可以结束该进程及其子进程。是否继续？",
        ):
            return
        self._run_action(lambda: self.engine.adopt_session(app_id, pid))

    def _stop_managed(self):
        app_id = self.app_id
        label = APP_LABELS[app_id]
        roots = self._snapshot.get("roots", [])
        external = [item for item in roots if not item.get("owned")]
        if not self._snapshot.get("managed_root_pids") and external:
            target = max(external, key=lambda item: item.get("started_at", 0.0))
            pid = int(target["pid"])
            if not ask_confirmation(
                self, f"接管并停止 {label}",
                f"将先验证并接管根进程 PID {pid}，再仅结束该根进程及其实际子进程。\n\n是否继续？",
            ):
                return
            self._run_action(lambda: self.engine.adopt_and_stop(app_id, pid))
            return
        if not ask_confirmation(
            self, "停止受管会话",
            f"将结束启动器已登记的 {label} 根进程及其子进程。是否继续？",
        ):
            return
        action = self.engine.stop_codex if app_id == "codex" else self.engine.stop_antigravity
        self._run_action(action)

    def _run_action(self, action):
        generation = self._view_generation
        self.action_status.configure(text="正在执行…", text_color=COLOR_MUTED)
        self.adopt_btn.configure(state="disabled")
        self.stop_btn.configure(state="disabled")

        def worker():
            try:
                ok, message = action()
            except Exception as exc:
                ok, message = False, str(exc)
            self._dispatch_ui(lambda: self._after_action(ok, message) if generation == self._view_generation else None)

        self._run_in_background(worker)

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
