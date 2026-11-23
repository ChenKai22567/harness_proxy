import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock
from types import SimpleNamespace

from build import APP_NAME, _copy_runtime_data, _kill_running_instances, _publish_release
from core.config_manager import ConfigManager, get_resource_path, get_resource_root_dir
from core.launcher_engine import LauncherEngine
from core.proxy_prober import CODEX_PROBE_URL, HttpsProxyResult, ProxyProbeStage
from gui.main_window import (
    COLOR_BG,
    COLOR_BORDER_STRONG,
    COLOR_CARD_HOVER,
    COLOR_LOG_BG,
    MainWindow,
)
from gui.tray_manager import TrayManager
from main import SingleInstanceGuard


class ResourcePathTests(unittest.TestCase):
    def test_source_resource_path_points_to_real_icon(self):
        self.assertTrue(os.path.isfile(get_resource_path("assets", "icon.png")))

    def test_frozen_resource_path_uses_meipass(self):
        with tempfile.TemporaryDirectory() as bundle_root:
            with mock.patch.object(sys, "frozen", True, create=True):
                with mock.patch.object(sys, "_MEIPASS", bundle_root, create=True):
                    self.assertEqual(get_resource_root_dir(), os.path.abspath(bundle_root))


class TrayIconTests(unittest.TestCase):
    def _manager(self, icon_path):
        return TrayManager(icon_path, lambda: None, lambda: None)

    def test_tray_icon_preserves_alpha(self):
        image = self._manager(get_resource_path("assets", "icon.png"))._create_image()
        self.assertEqual(image.mode, "RGBA")
        self.assertEqual(image.width, image.height)
        self.assertEqual(image.getchannel("A").getextrema()[0], 0)

    def test_missing_asset_fallback_is_not_a_solid_blue_block(self):
        image = self._manager(os.path.join("Z:\\", "missing", "icon.png"))._create_image()
        colors = image.getcolors(maxcolors=image.width * image.height)
        self.assertIsNotNone(colors)
        self.assertGreater(len(colors), 2)
        self.assertEqual(image.getchannel("A").getextrema()[0], 0)


@unittest.skipUnless(sys.platform.startswith("win"), "Windows mutex regression test")
class SingleInstanceTests(unittest.TestCase):
    def test_second_guard_cannot_acquire_named_mutex(self):
        # Isolate this regression check from a real packaged instance that may
        # legitimately hold the production mutex while the tests are running.
        test_mutex = f"Local\\HarnessProxyLauncher.Test.{uuid.uuid4()}"
        first = SingleInstanceGuard(test_mutex)
        second = SingleInstanceGuard(test_mutex)
        try:
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
        finally:
            second.close()
            first.close()


class ReleasePublishingTests(unittest.TestCase):
    def test_publish_preserves_runtime_config_and_logs(self):
        with tempfile.TemporaryDirectory() as root:
            final_release = os.path.join(root, "dist", APP_NAME)
            new_release = os.path.join(root, "staging", "dist", APP_NAME)
            os.makedirs(os.path.join(final_release, "logs"), exist_ok=True)
            os.makedirs(new_release, exist_ok=True)

            with open(os.path.join(final_release, "config.json"), "w", encoding="utf-8") as handle:
                handle.write('{"user": true}')
            with open(os.path.join(final_release, "logs", "launcher.log"), "w", encoding="utf-8") as handle:
                handle.write("existing log")
            with open(os.path.join(final_release, "managed_sessions.json"), "w", encoding="utf-8") as handle:
                handle.write('{"version": 1, "sessions": {}}')
            with open(os.path.join(new_release, f"{APP_NAME}.exe"), "wb") as handle:
                handle.write(b"new executable")

            _copy_runtime_data(final_release, new_release, os.path.join(root, "default.json"))
            staging_root = os.path.join(root, "swap")
            os.makedirs(staging_root, exist_ok=True)
            # Unit tests must never stop a real packaged launcher (or any app
            # it started) merely to exercise directory publishing.
            _publish_release(
                new_release,
                final_release,
                stop_running_instances=False,
            )

            with open(os.path.join(final_release, "config.json"), encoding="utf-8") as handle:
                self.assertEqual(handle.read(), '{"user": true}')
            with open(os.path.join(final_release, "logs", "launcher.log"), encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "existing log")
            with open(os.path.join(final_release, "managed_sessions.json"), encoding="utf-8") as handle:
                self.assertEqual(handle.read(), '{"version": 1, "sessions": {}}')
            self.assertTrue(os.path.isfile(os.path.join(final_release, f"{APP_NAME}.exe")))

            previous = os.path.join(staging_root, "previous-release")
            if os.path.isdir(previous):
                shutil.rmtree(previous)

    @unittest.skipUnless(sys.platform.startswith("win"), "Windows taskkill regression test")
    def test_build_never_terminates_launcher_descendants(self):
        with mock.patch("subprocess.run") as run, mock.patch("time.sleep"):
            _kill_running_instances()
        arguments = run.call_args.args[0]
        self.assertIn(f"{APP_NAME}.exe", arguments)
        self.assertNotIn("/T", arguments)


class CodexUpdateTests(unittest.TestCase):
    def test_stale_msix_path_is_refreshed_before_preflight(self):
        with tempfile.TemporaryDirectory() as root:
            config = ConfigManager(os.path.join(root, "config.json"))
            current_exe = os.path.join(root, "OpenAI.Codex_new", "app", "ChatGPT.exe")
            os.makedirs(os.path.dirname(current_exe), exist_ok=True)
            with open(current_exe, "wb") as handle:
                handle.write(b"test")

            with mock.patch("core.launcher_engine.find_antigravity_executable", return_value=None), \
                 mock.patch("core.launcher_engine.find_node_and_npx", return_value={}), \
                 mock.patch("core.launcher_engine.find_codex_executable", return_value=(current_exe, os.path.dirname(os.path.dirname(current_exe)))):
                engine = LauncherEngine(config)
                engine.cached_codex_path = os.path.join(root, "OpenAI.Codex_old", "app", "ChatGPT.exe")
                engine.cached_codex_dir = os.path.dirname(os.path.dirname(engine.cached_codex_path))
                info = engine.preflight_codex(proc_map={})

            self.assertEqual(info["exe_path"], current_exe)
            self.assertTrue(info["exe_found"])

    def test_launch_uses_refreshed_codex_path(self):
        with tempfile.TemporaryDirectory() as root:
            config = ConfigManager(os.path.join(root, "config.json"))
            current_exe = os.path.join(root, "OpenAI.Codex_new", "app", "ChatGPT.exe")
            os.makedirs(os.path.dirname(current_exe), exist_ok=True)
            with open(current_exe, "wb") as handle:
                handle.write(b"test")

            with mock.patch("core.launcher_engine.find_antigravity_executable", return_value=None), \
                 mock.patch("core.launcher_engine.find_node_and_npx", return_value={}), \
                 mock.patch("core.launcher_engine.find_codex_executable", return_value=(current_exe, os.path.dirname(os.path.dirname(current_exe)))):
                engine = LauncherEngine(config)
                engine.cached_codex_path = os.path.join(root, "OpenAI.Codex_old", "app", "ChatGPT.exe")
                engine.cached_codex_dir = os.path.dirname(os.path.dirname(engine.cached_codex_path))

                fake_process = SimpleNamespace(pid=1234, returncode=None, poll=lambda: None)
                with mock.patch.object(engine, "preflight_proxy", return_value={"reachable": True}), \
                     mock.patch("core.launcher_engine.get_codex_process_info", return_value={"running": False, "pids": [], "residual_pids": [9876], "residual_running": True, "has_proxy": False, "count": 0}), \
                     mock.patch("core.launcher_engine.subprocess.Popen", return_value=fake_process) as popen, \
                     mock.patch("core.launcher_engine.time.sleep"):
                    ok, _ = engine.launch_codex()

            self.assertTrue(ok)
            self.assertEqual(popen.call_args.args[0][0], current_exe)
            launch_env = popen.call_args.kwargs["env"]
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
                self.assertEqual(launch_env[key], "http://127.0.0.1:7890")
            self.assertEqual(launch_env["NODE_USE_ENV_PROXY"], "1")
            for key in (
                "WS_PROXY",
                "WSS_PROXY",
                "ELECTRON_GET_USE_PROXY",
            ):
                self.assertNotIn(key, launch_env)

@unittest.skipUnless(sys.platform.startswith("win"), "Windows UI regression test")
class WindowLifecycleTests(unittest.TestCase):
    def _wait_until(self, app, ready):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            app.update()
            if ready():
                return
            time.sleep(0.01)
        self.fail("The GUI did not apply the worker result")

    @mock.patch.object(LauncherEngine, "log", autospec=True)
    @mock.patch.object(MainWindow, "_start_single_instance_server", autospec=True)
    @mock.patch.object(MainWindow, "_start_background_monitor", autospec=True)
    @mock.patch.object(TrayManager, "start", autospec=True)
    @mock.patch.object(TrayManager, "stop", autospec=True)
    def test_windows_are_complete_before_mapping(
        self,
        _tray_stop,
        _tray_start,
        _monitor,
        _server,
        _log,
    ):
        import tkinter as tk

        app = MainWindow(start_services=False, allow_process_control=False)
        callback_errors = []
        app.report_callback_exception = lambda _type, error, _trace: callback_errors.append(error)
        try:
            app.update()
            self.assertEqual(app.state(), "normal")
            self.assertFalse(app.log_drawer_visible)
            self.assertFalse(app.log_container.winfo_manager())
            self.assertFalse(app.app_cards["antigravity"].feedback.winfo_manager())
            self.assertEqual(str(app.log_textbox.cget("state")), "disabled")
            self.assertEqual(app.log_textbox.cget("fg_color"), COLOR_LOG_BG)
            self.assertEqual(str(app.preset_combo.cget("state")), "readonly")
            self.assertTrue(app.preset_combo.get())
            self.assertEqual(app.cget("fg_color"), COLOR_BG)
            self.assertEqual(app.preset_combo.cget("button_color"), "#FFFFFF")
            self.assertEqual(app.preset_combo.cget("button_hover_color"), COLOR_CARD_HOVER)
            self.assertEqual(app.preset_combo.cget("corner_radius"), 6)
            self.assertEqual(app.preset_combo.cget("width"), 180)
            self.assertEqual(app.preset_combo.cget("border_color"), COLOR_BORDER_STRONG)
            self.assertTrue(app.preset_combo._canvas.find_withtag("polished_toggle_separator"))
            self.assertEqual(
                app.preset_combo._canvas.itemcget("border_parts_right", "fill").lower(),
                COLOR_BORDER_STRONG.lower(),
            )
            combo = app.preset_combo
            canvas = combo._canvas
            x = canvas.winfo_width() - canvas.winfo_height() // 2
            y = canvas.winfo_height() // 2
            canvas.event_generate("<Motion>", x=x, y=y)
            canvas.event_generate("<ButtonPress-1>", x=x, y=y)
            app.update()
            canvas.event_generate("<ButtonRelease-1>", x=x, y=y)
            settled = tk.BooleanVar(app, False)
            app.after(200, lambda: settled.set(True))
            app.wait_variable(settled)
            self.assertTrue(combo._is_popup_open())
            # The same ButtonPress event bubbles to the owner after opening;
            # it must be recognized as an inside click instead of immediately
            # destroying the new popup.
            combo._on_owner_click(SimpleNamespace(widget=canvas))
            app.update()
            self.assertTrue(combo._is_popup_open())
            self.assertEqual(len(combo._popup_buttons), len(combo.cget("values")))
            self.assertEqual(combo._popup_frame.cget("corner_radius"), 7)
            self.assertNotIn("✓", combo._popup_buttons[combo._popup_index].cget("text"))
            self.assertIs(
                combo._popup_buttons[combo._popup_index].cget("image"),
                combo._check_image,
            )
            self.assertIsNone(app.grab_current())
            self.assertEqual(float(canvas.itemcget("polished_chevron", "width")), 1.0)
            self.assertNotEqual(
                combo._popup.cget("bg").lower(),
                combo._popup_border_color.lower(),
            )
            if combo._popup_transparent_key:
                self.assertEqual(
                    str(combo._popup.wm_attributes("-transparentcolor")).lower(),
                    combo._popup_transparent_key.lower(),
                )

            with mock.patch.object(combo, "_command") as selected:
                combo._on_keyboard_open(SimpleNamespace(keysym="Down"))
                expected = combo.cget("values")[combo._popup_index]
                combo._on_keyboard_open(SimpleNamespace(keysym="Return"))
            selected.assert_called_once_with(expected)
            self.assertEqual(combo.get(), expected)
            self.assertFalse(combo._is_popup_open())

            combo._clicked()
            combo._on_owner_click(SimpleNamespace(widget=app.log_textbox))
            self.assertFalse(combo._is_popup_open())

            anti_info = {
                "exe_found": False, "exe_path": None, "running": True,
                "pids": [201, 202], "root_pids": [201], "has_proxy": True,
                "node_found": False, "npx_found": False, "node_path": "",
                "managed": False, "owned_pids": [], "residual_pids": [],
            }
            codex_info = {
                "exe_found": True, "exe_path": "ChatGPT.exe", "running": True,
                "pids": [101, 102], "root_pids": [101], "has_proxy": True,
                "browser_proxy": True, "api_proxy": True, "websocket_proxy": True,
                "proxy_mode": "独立进程代理", "system_proxy": {"matches": False},
                "managed": False, "owned_pids": [], "residual_pids": [],
            }
            app._apply_status_updates(
                "127.0.0.1", 7890, True, 1.0, "ok", anti_info, codex_info,
            )
            codex_card = app.app_cards["codex"]
            self.assertEqual(codex_card.menu.buttons["restart"].cget("state"), "normal")
            self.assertEqual(codex_card.menu.buttons["stop"].cget("state"), "normal")
            self.assertIn("外部会话", codex_card.summary.cget("text"))
            # External Antigravity sessions must behave exactly like Codex:
            # controls stay usable and route through the confirm-adopt flow.
            anti_card = app.app_cards["antigravity"]
            self.assertEqual(anti_card.menu.buttons["restart"].cget("state"), "normal")
            self.assertEqual(anti_card.menu.buttons["stop"].cget("state"), "normal")
            self.assertIn("外部会话", anti_card.summary.cget("text"))
            with mock.patch("gui.main_window.ask_confirmation", return_value=True), \
                 mock.patch.object(app, "_async_action") as async_action:
                app._request_app_control("codex", "restart")
            async_action.assert_called_once()
            self.assertEqual(async_action.call_args.args[0].__name__, "adopt_then_restart_codex")

            with mock.patch("gui.main_window.ask_confirmation", return_value=True), \
                 mock.patch.object(app, "_async_action") as async_action:
                app._request_app_control("antigravity", "restart")
            async_action.assert_called_once()
            self.assertEqual(async_action.call_args.args[0].__name__, "adopt_then_restart_antigravity")

            with mock.patch("gui.main_window.ask_confirmation", return_value=True), \
                 mock.patch.object(app, "_async_action") as async_action:
                app._request_app_control("antigravity", "stop")
            async_action.assert_called_once()
            self.assertEqual(async_action.call_args.args[0].__name__, "adopt_then_stop_antigravity")

            app._open_settings()
            app.update()
            dialog = app._settings_dialog
            self.assertTrue(dialog.winfo_exists())
            self.assertEqual(dialog.state(), "normal")
            self.assertEqual(str(dialog.preset_combo.cget("state")), "readonly")
            self.assertTrue(dialog.preset_combo.get())
            self.assertEqual(dialog.cget("fg_color"), COLOR_BG)
            if app.winfo_rooty() >= 24:
                self.assertLess(dialog.winfo_rooty(), app.winfo_rooty())
            self.assertEqual(dialog.preset_combo.cget("button_color"), "#FFFFFF")
            self.assertTrue(dialog.preset_combo._canvas.find_withtag("polished_toggle_separator"))
            self.assertEqual(
                dialog.preset_combo._canvas.itemcget("border_parts_right", "fill").lower(),
                COLOR_BORDER_STRONG.lower(),
            )
            dialog.preset_combo._open_dropdown_menu()
            dialog.update()
            self.assertTrue(dialog.preset_combo._is_popup_open())
            self.assertIsNone(dialog.grab_current())
            dialog.preset_combo._close_popup(restore_focus=False)
            self.assertIsNone(dialog.grab_current())
            self.assertFalse(dialog.start_minimized_var.get())
            with mock.patch("gui.settings_dialog.probe_tcp_port", return_value=(True, 1.0, "ok")) as probe:
                dialog.test_btn.invoke()
                self._wait_until(app, lambda: dialog.test_btn.cget("state") == "normal")
                probe.assert_called_once()
                self.assertIn("正常", dialog.test_status_label.cget("text"))
            with mock.patch("gui.settings_dialog.scan_available_proxies", return_value=[]) as scan:
                dialog.scan_btn.invoke()
                self._wait_until(app, lambda: dialog.scan_btn.cget("state") == "normal")
                scan.assert_called_once()
                self.assertIn("未发现", dialog.test_status_label.cget("text"))

            # The settings window is deliberately modeless: even if Windows
            # ever fails to map it, it cannot capture all clicks invisibly.
            was_visible = app.log_drawer_visible
            app._toggle_log_drawer()
            self.assertNotEqual(app.log_drawer_visible, was_visible)
            dialog._close()
            app.update_idletasks()
            self.assertIsNone(app._settings_dialog)
            self.assertEqual(callback_errors, [])
        finally:
            if app.winfo_exists():
                app._force_exit_app()

    @mock.patch.object(LauncherEngine, "log", autospec=True)
    def test_diagnostic_can_retry_after_an_exception_and_discard_closed_result(self, _log):
        app = MainWindow(start_services=False, allow_process_control=False)
        pending = threading.Event()
        result = {
            "listener": {"reachable": True, "host": "127.0.0.1", "port": 7890, "latency_ms": 1},
            "primary": HttpsProxyResult(
                CODEX_PROBE_URL, (ProxyProbeStage("http", True, 100, "站点已响应（HTTP 403）", 403),),
            ),
            "control": None,
            "process": {"running": False},
        }

        def delayed_check():
            pending.wait(3)
            return result

        try:
            with mock.patch.object(
                app.engine, "diagnose_codex_proxy",
                side_effect=[RuntimeError("模拟预检异常"), result],
            ) as check:
                app._open_diagnostics("codex")
                dialog = app._diagnostic_dialogs["codex"]
                self._wait_until(app, lambda: dialog.retry_btn.cget("state") == "normal")
                self.assertIn("模拟预检异常", "\n".join(dialog._lines))
                dialog.retry_btn.invoke()
                self._wait_until(app, lambda: dialog.retry_btn.cget("state") == "normal")
                self.assertIn("站点已响应 · HTTP 403", dialog.summary.cget("text"))

                with mock.patch.object(dialog, "_complete", wraps=dialog._complete) as complete:
                    visible_report = dialog._report
                    check.side_effect = delayed_check
                    dialog.retry_btn.invoke()
                    self._wait_until(app, lambda: check.call_count == 3)
                    self.assertIs(dialog._report, visible_report)
                    self.assertTrue(visible_report.winfo_ismapped())
                    dialog.close()
                    pending.set()
                    self._wait_until(app, lambda: complete.called)
                self.assertFalse(dialog.winfo_exists())
                self.assertFalse(dialog._lines)
        finally:
            pending.set()
            app._force_exit_app()

    @mock.patch.object(LauncherEngine, "log", autospec=True)
    def test_compact_layout_aligns_at_both_scales_and_restores_from_tray(self, _log):
        import customtkinter as ctk
        from customtkinter.windows.widgets.scaling.scaling_tracker import ScalingTracker
        from gui.main_window import MIN_WINDOW_HEIGHT, MIN_WINDOW_WIDTH
        from gui.theme import BUTTON_RADIUS, CARD_RADIUS

        app = MainWindow(start_services=False, allow_process_control=False)
        original_widget = ScalingTracker.widget_scaling
        original_window = ScalingTracker.window_scaling
        try:
            app.update()
            dpi = ScalingTracker.get_window_dpi_scaling(app)
            for scale in (1.0, 1.5):
                with self.subTest(scale=scale):
                    ctk.set_widget_scaling(scale / dpi)
                    ctk.set_window_scaling(scale / dpi)
                    app.geometry(f"{MIN_WINDOW_WIDTH}x{MIN_WINDOW_HEIGHT}")
                    app._apply_status_updates(
                        "255.255.255.255", 65535, True, 999.9, "",
                        {"exe_found": True}, {"exe_found": True, "running": True, "managed": True},
                    )
                    app.update()
                    self.assertAlmostEqual(app.winfo_width(), MIN_WINDOW_WIDTH * scale, delta=2)
                    controls = [app.preset_combo, app.toggle_log_btn, app.stop_all_btn]
                    for card in app.app_cards.values():
                        self.assertEqual(card.cget("corner_radius"), CARD_RADIUS)
                        self.assertEqual(card.more_btn.cget("corner_radius"), BUTTON_RADIUS)
                        self.assertEqual(card.launch_btn.winfo_rooty(), card.more_btn.winfo_rooty())
                        controls.extend((card.launch_btn, card.check_btn, card.more_btn))
                    for control in controls:
                        self.assertTrue(control.winfo_ismapped())
                        self.assertLessEqual(
                            control.winfo_rootx() + control.winfo_width(),
                            app.winfo_rootx() + app.winfo_width(),
                        )
                        self.assertLessEqual(
                            control.winfo_rooty() + control.winfo_height(),
                            app.winfo_rooty() + app.winfo_height(),
                        )
                    for name in ("polished_toggle_separator", "polished_chevron"):
                        coords = app.preset_combo._canvas.coords(name)
                        self.assertTrue(coords and all(value.is_integer() for value in coords))
                    card = app.app_cards["codex"]
                    card.more_btn.invoke()
                    app.update()
                    self.assertTrue(card.menu.winfo_ismapped())
                    self.assertLessEqual(card.menu.winfo_rootx() + card.menu.winfo_width(),
                                         app.winfo_rootx() + app.winfo_width())
                    card.menu._on_escape(None)
                    self.assertFalse(card.menu.winfo_manager())

            app.attributes("-alpha", 0.15)
            self.assertEqual(float(app.attributes("-alpha")), 1.0)
            app.config_mgr.config["launcher"]["minimize_to_tray_on_close"] = True
            app._on_window_close()
            self.assertEqual(app.state(), "withdrawn")
            app._restore_from_tray()
            self._wait_until(app, lambda: app.is_window_visible)
            self.assertEqual(app.state(), "normal")
            self.assertFalse(app.log_container.winfo_manager())
        finally:
            app._force_exit_app()
            ctk.set_widget_scaling(original_widget)
            ctk.set_window_scaling(original_window)

    @mock.patch.object(LauncherEngine, "log", autospec=True)
    def test_component_rows_are_built_in_a_hidden_frame_before_display(self, _log):
        from gui.component_dialog import ComponentDialog

        app = MainWindow(start_services=False, allow_process_control=False)
        built_hidden = []
        original = ComponentDialog._build_component_row

        def observe(dialog, parent, component):
            built_hidden.append(not parent.winfo_manager())
            original(dialog, parent, component)

        component = {"label": "渲染", "description": "窗口渲染", "running": True, "count": 1}
        try:
            with mock.patch.object(ComponentDialog, "_build_component_row", observe), \
                 mock.patch.object(app.engine, "get_component_snapshot", return_value={
                     "running": True, "components": [component, component], "process_count": 2,
                 }):
                app._open_components("codex")
                dialog = app._component_dialog
                self._wait_until(app, lambda: dialog._component_view is not None)
                self.assertEqual(built_hidden, [True, True])
                self.assertTrue(dialog._component_view.winfo_ismapped())
                previous = dialog._component_view
                dialog.show("antigravity")
                self._wait_until(app, lambda: len(built_hidden) == 4)
                self.assertEqual(built_hidden, [True] * 4)
                self.assertFalse(previous.winfo_exists())
                self.assertTrue(dialog._component_view.winfo_ismapped())
        finally:
            app._force_exit_app()


if __name__ == "__main__":
    unittest.main()
