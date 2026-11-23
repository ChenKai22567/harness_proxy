import os
from urllib.parse import urlsplit

import customtkinter as ctk

from gui.dialog_base import DialogSurface, replace_content
from gui.theme import *  # noqa: F401,F403 - shared design tokens
from gui.widgets import ActionButton


ANTIGRAVITY_DOWNLOAD = "https://antigravity.google/download"
NETWORK_STAGES = (("connect", "HTTP CONNECT"), ("tls", "TLS 握手"), ("http", "目标 HTTP"))


class DiagnosticDialog(DialogSurface):
    """Modeless diagnostic with a copyable report and explicit retry."""

    def __init__(self, parent, app_id, app_name, check):
        super().__init__(parent)
        self._app_id = app_id
        self._check = check
        self._busy = False
        self._closed = False
        self._lines = []
        self._report = None

        caption = f"{app_name} 预检"
        self.title(caption)
        self.minsize(524, 350)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", self.close)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=WINDOW_PADDING, pady=(16, SECTION_GAP))
        ctk.CTkLabel(
            header, text=caption, anchor="w", text_color=COLOR_TEXT_HEADING,
            font=ctk.CTkFont(family=FONT_FAMILY, size=21, weight="bold"),
        ).pack(fill="x")
        self.summary = ctk.CTkLabel(
            header, text="", anchor="nw", justify="left", wraplength=465, height=38,
            text_color=COLOR_TEXT_MUTED, font=ctk.CTkFont(family=FONT_FAMILY, size=13),
        )
        self.summary.pack(fill="x", pady=(3, 0))

        self.body = ctk.CTkScrollableFrame(
            self, fg_color=COLOR_BG, scrollbar_button_color=COLOR_SCROLLBAR,
            scrollbar_button_hover_color=COLOR_SCROLLBAR_HOVER,
        )
        self.body.pack(fill="both", expand=True, padx=(WINDOW_PADDING, 12))

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(fill="x", padx=WINDOW_PADDING, pady=(8, 16))
        self.copy_btn = ActionButton(footer, text="复制详情", width=82, command=self._copy_report)
        self.copy_btn.pack(side="left")
        if app_id == "antigravity":
            ActionButton(
                footer, text="打开官方下载", width=116, command=self._open_download,
            ).pack(side="left", padx=(8, 0))
        self.retry_btn = ActionButton(footer, text="复检", width=76, kind="primary", command=self.retry)
        close_btn = ActionButton(footer, text="关闭", width=70, command=self.close)
        close_btn.pack(side="right")
        self.retry_btn.pack(side="right", padx=(0, 8))

        self.center_over_parent(640, 560)
        self.retry()
        self.show()

    def show(self):
        self.present()

    def close(self, _event=None):
        self._closed = True
        self.destroy()

    def retry(self):
        if self._busy or self._closed:
            return
        self._busy = True
        self.retry_btn.configure(state="disabled", text="检测中…")
        self.copy_btn.configure(state="disabled", text="复制详情")
        self.summary.configure(text="正在检测…", text_color=COLOR_TEXT_MUTED)
        self._lines.clear()

        def worker():
            try:
                result = self._check()
            except Exception as error:
                message = str(error) or type(error).__name__
                self._parent._dispatch_ui(lambda: self._complete(error=message))
            else:
                self._parent._dispatch_ui(lambda: self._complete(result=result))

        try:
            self._parent._executor.submit(worker)
        except RuntimeError as error:
            self._complete(error=str(error))

    def _complete(self, result=None, error=None):
        if self._closed:
            return
        self._prepared_report = ctk.CTkFrame(self.body, fg_color="transparent")
        if error is not None:
            self._set_summary("诊断执行失败，可以复检。", COLOR_ERROR_TEXT)
            self._row(self._section("错误详情"), "原因", error, COLOR_ERROR_TEXT)
        elif self._app_id == "codex":
            self._show_codex(result)
        else:
            self._show_antigravity(result)
        replace_content(self._report, self._prepared_report)
        self._report = self._prepared_report
        self.body._parent_canvas.yview_moveto(0)
        summary, color = self._summary_value
        self.summary.configure(text=summary, text_color=color)
        self._busy = False
        self.retry_btn.configure(state="normal", text="复检")
        self.copy_btn.configure(state="normal")

    def _set_summary(self, text, color):
        self._summary_value = (text, color)
        self._lines.append(text)

    def _section(self, title):
        card = ctk.CTkFrame(
            self._prepared_report, corner_radius=CARD_RADIUS, fg_color=COLOR_CARD_BG, border_width=1,
            border_color=COLOR_CARD_BORDER,
        )
        card.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(
            card, text=title, anchor="w", text_color=COLOR_TEXT_HEADING,
            font=ctk.CTkFont(family=FONT_FAMILY, size=14, weight="bold"),
        ).pack(fill="x", padx=12, pady=(8, 5))
        self._lines.extend(("", title))
        return card

    def _row(self, card, label, value, color=COLOR_TEXT_PRIMARY):
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=(0, 6))
        ctk.CTkLabel(
            row, text=label, width=108, height=18, anchor="nw", text_color=COLOR_TEXT_MUTED,
            font=ctk.CTkFont(family=FONT_FAMILY, size=12),
        ).pack(side="left", anchor="n")
        ctk.CTkLabel(
            row, text=value, height=18, anchor="w", justify="left", wraplength=327,
            text_color=color, font=ctk.CTkFont(family=FONT_FAMILY, size=12),
        ).pack(side="left", fill="x", expand=True)
        self._lines.append(f"{label}  {value}")

    def _probe_rows(self, card, result):
        stages = {stage.name: stage for stage in result.stages} if result else {}
        tcp = stages.get("tcp")
        if tcp and not tcp.ok:
            self._row(card, "连接代理", f"{tcp.message} · {tcp.elapsed_ms:g} ms", COLOR_ERROR_TEXT)
        for name, label in NETWORK_STAGES:
            stage = stages.get(name)
            if not stage:
                self._row(card, label, "未执行", COLOR_TEXT_MUTED)
                continue
            if not stage.ok:
                color = COLOR_ERROR_TEXT
            elif name == "http" and stage.status_code >= 400:
                color = COLOR_WARN_TEXT
            else:
                color = COLOR_SUCCESS_TEXT
            self._row(card, label, f"{stage.message} · {stage.elapsed_ms:g} ms", color)

    def _show_codex(self, result):
        listener = result["listener"]
        address = f"{listener['host']}:{listener['port']}"
        primary, control = result["primary"], result["control"]
        if not listener["reachable"]:
            summary = f"代理端口 {address} 无法连接，请检查代理程序和端口。"
            color = COLOR_ERROR_TEXT
        elif primary.received_http_response:
            code = primary.stages[-1].status_code
            elapsed = sum(stage.elapsed_ms for stage in primary.stages)
            summary = f"ChatGPT 站点已响应 · HTTP {code} · {elapsed / 1000:.2f} 秒"
            color = COLOR_SUCCESS_TEXT if code < 400 else COLOR_WARN_TEXT
        elif control and control.received_http_response:
            summary = "代理可访问对照站点，ChatGPT 出站失败。请检查 ChatGPT 分流和当前节点。"
            color = COLOR_WARN_TEXT
        elif control:
            summary = "两个目标均未完成 HTTPS。请检查当前代理节点后复检。"
            color = COLOR_ERROR_TEXT
        else:
            summary = "代理连接失败，请查看下方原因后复检。"
            color = COLOR_ERROR_TEXT
        self._set_summary(summary, color)

        route = self._section("ChatGPT 代理出站")
        listener_status = f"正常 · {listener['latency_ms']:g} ms" if listener["reachable"] else listener["message"]
        tone = COLOR_SUCCESS_TEXT if listener["reachable"] else COLOR_ERROR_TEXT
        self._row(route, "代理端口", f"{address} · {listener_status}", tone)
        self._probe_rows(route, primary)
        if control:
            self._probe_rows(self._section(f"对照站点 · {urlsplit(control.url).netloc}"), control)

        process = result["process"]
        config = self._section("应用代理配置")
        for label, key in (
            ("Chromium", "browser_proxy"), ("API", "api_proxy"), ("WebSocket", "websocket_proxy"),
        ):
            value = "已检测到" if process.get(key) else "未检测到"
            self._row(config, label, value if process.get("running") else "待应用启动")
        self._row(config, "代理模式", process.get("proxy_mode") or "未知")
        system_proxy = process.get("system_proxy", {})
        if system_proxy.get("enabled"):
            self._row(config, "Windows 代理", system_proxy.get("server") or "已开启")
        self._show_process(process)

    def _show_antigravity(self, result):
        health = result["installation"]
        status = health["status"]
        if status == "ready":
            self._set_summary("安装检查通过。", COLOR_SUCCESS_TEXT)
        elif status == "updating":
            self._set_summary("更新安装器正在运行，请完成更新后复检。", COLOR_WARN_TEXT)
        elif status == "missing":
            self._set_summary("未找到安装。可在偏好设置指定路径，或从官网下载。", COLOR_ERROR_TEXT)
        else:
            self._set_summary("安装需要修复，请用同产品的官方安装包覆盖安装后复检。", COLOR_ERROR_TEXT)

        if status != "missing":
            card = self._section("安装状态")
            if health["updater_pids"]:
                self._row(card, "安装器 PID", self._format_pids(health["updater_pids"]))
            for issue in health["issues"]:
                self._row(card, "检查结果", issue, COLOR_ERROR_TEXT)
            if status == "ready":
                self._row(card, "关键资源", "文件可读，已识别的 V8 版本一致", COLOR_SUCCESS_TEXT)
        process = result["process"]
        self._show_process(process)
        runtime = self._section("浏览器代理扩展")
        self._row(runtime, "Node.js", process.get("node_path") or "未找到")
        self._row(runtime, "npx", process.get("npx_path") or "未找到")

    @staticmethod
    def _format_pids(pids):
        return ", ".join(map(str, pids))

    def _show_process(self, info):
        card = self._section("应用与进程")
        self._row(card, "可执行文件", info.get("exe_path") or "未找到")
        if info.get("running"):
            owner = "受管会话" if info.get("managed") else "外部会话"
            self._row(card, "运行状态", f"运行中 · {owner}")
        else:
            self._row(card, "运行状态", "未运行")
        if self._app_id == "antigravity" and info.get("running"):
            self._row(card, "代理参数", "已检测到" if info.get("has_proxy") else "未检测到")
        for label, key in (
            ("主进程 PID", "root_pids"), ("进程 PID", "pids"),
            ("残留 PID", "residual_pids"), ("已终止记录", "ignored_pids"),
        ):
            if info.get(key):
                self._row(card, label, self._format_pids(info[key]))

    def _copy_report(self):
        self.clipboard_clear()
        self.clipboard_append("\n".join([self.title(), *self._lines]))
        self.copy_btn.configure(text="已复制")

    def _open_download(self):
        try:
            os.startfile(ANTIGRAVITY_DOWNLOAD)
        except OSError:
            self.summary.configure(
                text=f"无法打开默认浏览器，可访问 {ANTIGRAVITY_DOWNLOAD}", text_color=COLOR_ERROR_TEXT,
            )
