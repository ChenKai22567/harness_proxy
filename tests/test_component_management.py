import json
import os
import tempfile
import unittest
from unittest import mock

from core.config_manager import ConfigManager
from core.launcher_engine import LauncherEngine
from core.process_classifier import classify_process
from core.process_detector import get_codex_process_info, terminate_process_tree


def make_engine(root: str, *, allow_process_control: bool = False) -> LauncherEngine:
    config = ConfigManager(os.path.join(root, "config.json"))
    with mock.patch("core.launcher_engine.find_antigravity_executable", return_value=None), \
         mock.patch("core.launcher_engine.find_node_and_npx", return_value={}), \
         mock.patch("core.launcher_engine.find_codex_executable", return_value=(None, None)):
        engine = LauncherEngine(
            config,
            allow_process_control=allow_process_control,
            session_state_file=os.path.join(root, "managed_sessions.json"),
        )
        # Test PIDs and simulated failures must not pollute the user's real
        # launcher log with messages that look like actual process control.
        engine.log = mock.Mock()
        return engine


class ProxyEnvironmentTests(unittest.TestCase):
    def test_codex_proxy_environment_matches_known_good_standard_variables(self):
        stale_proxy_environment = {
            "WS_PROXY": "http://127.0.0.1:9999",
            "WSS_PROXY": "http://127.0.0.1:9999",
            "NODE_USE_ENV_PROXY": "1",
            "ELECTRON_GET_USE_PROXY": "1",
        }
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            os.environ,
            stale_proxy_environment,
            clear=False,
        ):
            engine = make_engine(root)
            env, proxy_url, env_bypass, chromium_bypass = engine._build_proxy_environment()

            self.assertEqual(proxy_url, "http://127.0.0.1:7890")
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
                self.assertEqual(env[key], proxy_url)
            self.assertEqual(env["NO_PROXY"], env_bypass)
            for key in (
                "WS_PROXY",
                "WSS_PROXY",
                "ws_proxy",
                "wss_proxy",
                "NODE_USE_ENV_PROXY",
                "ELECTRON_GET_USE_PROXY",
            ):
                self.assertNotIn(key, env)
            self.assertIn("[::1]", chromium_bypass)

    def test_antigravity_can_opt_into_runtime_proxy_extensions(self):
        with tempfile.TemporaryDirectory() as root:
            engine = make_engine(root)
            env, proxy_url, _env_bypass, _chromium_bypass = engine._build_proxy_environment(
                include_runtime_extensions=True,
            )

            self.assertEqual(env["WS_PROXY"], proxy_url)
            self.assertEqual(env["WSS_PROXY"], proxy_url)
            self.assertEqual(env["NODE_USE_ENV_PROXY"], "1")
            self.assertEqual(env["ELECTRON_GET_USE_PROXY"], "1")

    def test_codex_diagnostic_maps_standard_https_proxy_to_websocket(self):
        class FakeProcess:
            def __init__(self, pid, ppid, command, environment=None):
                self.pid = pid
                self._ppid = ppid
                self._command = command
                self._environment = environment or {}

            def ppid(self):
                return self._ppid

            def cmdline(self):
                return list(self._command)

            def environ(self):
                return dict(self._environment)

            def status(self):
                return "running"

            def is_running(self):
                return True

            def num_threads(self):
                return 4

            def create_time(self):
                return float(self.pid)

        processes = {
            100: FakeProcess(100, 1, ["ChatGPT.exe", "--proxy-server=http://127.0.0.1:7890"]),
            200: FakeProcess(200, 100, ["codex.exe", "app-server"], {
                "HTTP_PROXY": "http://127.0.0.1:7890",
                "HTTPS_PROXY": "http://127.0.0.1:7890",
            }),
            # Standalone CLI process: it must not affect desktop status.
            300: FakeProcess(300, 1, ["codex.exe", "app-server"], {
                "WSS_PROXY": "http://127.0.0.1:7890",
            }),
        }

        class FakePsutil:
            @staticmethod
            def Process(pid):
                return processes[pid]

        with mock.patch("core.process_detector._get_psutil", return_value=FakePsutil), \
             mock.patch(
                 "core.process_detector.get_windows_user_proxy_info",
                 return_value={"enabled": False, "server": "", "matches": False},
             ):
            info = get_codex_process_info(
                {"chatgpt.exe": [100], "codex.exe": [200, 300]},
                proxy_port=7890,
            )
            self.assertTrue(info["has_proxy"])
            self.assertTrue(info["api_proxy"])
            self.assertTrue(info["websocket_proxy"])
            self.assertNotIn(300, info["pids"])

            processes[200]._environment = {
                "WSS_PROXY": "http://127.0.0.1:7890",
            }
            info = get_codex_process_info(
                {"chatgpt.exe": [100], "codex.exe": [200, 300]},
                proxy_port=7890,
            )
            self.assertFalse(info["has_proxy"])
            self.assertFalse(info["api_proxy"])
            self.assertTrue(info["websocket_proxy"])

    def test_stopped_shell_artifact_does_not_count_as_running(self):
        class StoppedProcess:
            pid = 400

            @staticmethod
            def status():
                return "stopped"

            @staticmethod
            def is_running():
                return True

            @staticmethod
            def num_threads():
                return 0

        class FakePsutil:
            STATUS_STOPPED = "stopped"
            STATUS_ZOMBIE = "zombie"
            STATUS_DEAD = "dead"

            @staticmethod
            def Process(pid):
                self.assertEqual(pid, 400)
                return StoppedProcess()

        with mock.patch("core.process_detector._get_psutil", return_value=FakePsutil), \
             mock.patch(
                 "core.process_detector.get_windows_user_proxy_info",
                 return_value={"enabled": False, "server": "", "matches": False},
             ):
            info = get_codex_process_info({"chatgpt.exe": [400]}, proxy_port=7890)

        self.assertFalse(info["running"])
        self.assertFalse(info["residual_running"])
        self.assertEqual(info["pids"], [])
        self.assertEqual(info["ignored_pids"], [400])
        self.assertEqual(info["proxy_mode"], "应用未运行")

    def test_native_zero_thread_snapshot_skips_stopped_shell_without_slow_queries(self):
        class SnapshotProcess:
            def status(self):
                raise AssertionError("status() must not be queried when Toolhelp has thread count")

            def num_threads(self):
                raise AssertionError("num_threads() must not be queried per PID")

        class FakePsutil:
            @staticmethod
            def Process(pid):
                self.assertEqual(pid, 402)
                return SnapshotProcess()

        with mock.patch("core.process_detector._get_psutil", return_value=FakePsutil), \
             mock.patch(
                 "core.process_detector.get_windows_user_proxy_info",
                 return_value={"enabled": False, "server": "", "matches": False},
             ):
            info = get_codex_process_info(
                {"chatgpt.exe": [402]},
                proxy_port=7890,
                process_entries=[(402, 1, "chatgpt.exe", 0)],
            )

        self.assertFalse(info["running"])
        self.assertEqual(info["ignored_pids"], [402])

    def test_orphan_renderer_is_residual_and_does_not_block_launch_state(self):
        class OrphanRenderer:
            pid = 401

            @staticmethod
            def status():
                return "running"

            @staticmethod
            def is_running():
                return True

            @staticmethod
            def num_threads():
                return 3

            @staticmethod
            def cmdline():
                return ["ChatGPT.exe", "--type=renderer"]

            @staticmethod
            def ppid():
                return 400

        class FakePsutil:
            @staticmethod
            def Process(pid):
                if pid == 401:
                    return OrphanRenderer()
                raise RuntimeError("parent already exited")

        with mock.patch("core.process_detector._get_psutil", return_value=FakePsutil), \
             mock.patch(
                 "core.process_detector.get_windows_user_proxy_info",
                 return_value={"enabled": False, "server": "", "matches": False},
             ):
            info = get_codex_process_info({"chatgpt.exe": [401]}, proxy_port=7890)

        self.assertFalse(info["running"])
        self.assertTrue(info["residual_running"])
        self.assertEqual(info["pids"], [])
        self.assertEqual(info["residual_pids"], [401])


class ComponentClassificationTests(unittest.TestCase):
    def test_stable_electron_roles(self):
        self.assertEqual(
            classify_process("codex", "ChatGPT.exe", ["ChatGPT.exe", "--type=renderer"]),
            "renderer",
        )
        self.assertEqual(
            classify_process("codex", "codex.exe", ["codex.exe", "app-server"]),
            "app_server",
        )
        self.assertEqual(
            classify_process("antigravity", "Antigravity.exe", ["Antigravity.exe"]),
            "core",
        )
        self.assertEqual(classify_process("codex", "ChatGPT.exe", []), "unknown")


class SafeProcessControlTests(unittest.TestCase):
    def test_external_codex_session_is_never_stopped_by_name(self):
        with tempfile.TemporaryDirectory() as root:
            engine = make_engine(root, allow_process_control=True)
            with mock.patch(
                "core.launcher_engine.get_codex_process_info",
                return_value={"running": True, "pids": [901], "has_proxy": False},
            ), mock.patch.object(engine.supervisor, "owned_pids", return_value=[]), \
                 mock.patch("core.launcher_engine.terminate_process_tree") as terminate:
                ok, message = engine.stop_codex()

            self.assertFalse(ok)
            self.assertIn("接管", message)
            terminate.assert_not_called()

    def test_managed_codex_stop_uses_exact_owned_processes(self):
        with tempfile.TemporaryDirectory() as root:
            engine = make_engine(root, allow_process_control=True)
            with mock.patch(
                "core.launcher_engine.get_codex_process_info",
                return_value={"running": True, "pids": [901, 902], "has_proxy": True},
            ), mock.patch.object(engine.supervisor, "owned_pids", return_value=[901, 902]), \
                 mock.patch.object(engine.supervisor, "forget") as forget, \
                 mock.patch(
                     "core.launcher_engine.terminate_process_tree",
                     return_value=(2, []),
                 ) as terminate:
                ok, _ = engine.stop_codex()

            self.assertTrue(ok)
            terminate.assert_called_once_with([901, 902], protected_pids=[os.getpid()])
            forget.assert_called_once_with("codex")

    def test_nested_launcher_and_its_children_are_preserved_during_restart(self):
        killed = []

        class FakeProcess:
            def __init__(self, pid, parent, descendants=None):
                self.pid = pid
                self._parent = parent
                self._descendants = descendants or []

            def ppid(self):
                return self._parent

            def children(self, recursive=False):
                return [processes[pid] for pid in self._descendants]

            def kill(self):
                killed.append(self.pid)

        processes = {
            100: FakeProcess(100, 0, [101, 200, 201]),
            101: FakeProcess(101, 100),
            200: FakeProcess(200, 100, [201]),
            201: FakeProcess(201, 200),
        }

        class FakePsutil:
            class NoSuchProcess(Exception):
                pass

            class AccessDenied(Exception):
                pass

            @staticmethod
            def Process(pid):
                return processes[pid]

            @staticmethod
            def wait_procs(_processes, timeout):
                return [], []

        with mock.patch("core.process_detector._get_psutil", return_value=FakePsutil):
            count, errors = terminate_process_tree([100], protected_pids=[200])

        self.assertEqual(errors, [])
        self.assertEqual(count, 2)
        self.assertEqual(set(killed), {100, 101})
        self.assertNotIn(200, killed)
        self.assertNotIn(201, killed)

    def test_codex_restart_waits_before_spawning_replacement(self):
        with tempfile.TemporaryDirectory() as root:
            engine = make_engine(root, allow_process_control=True)
            fake_process = mock.Mock(pid=1234)
            fake_process.poll.return_value = None
            with mock.patch.object(engine, "preflight_proxy", return_value={"reachable": True}), \
                 mock.patch.object(engine, "preflight_codex", return_value={
                     "exe_found": True, "exe_path": r"C:\Codex\ChatGPT.exe",
                     "running": True, "pids": [901],
                 }), mock.patch.object(engine, "stop_codex", return_value=(True, "stopped")) as stop, \
                 mock.patch.object(engine, "_wait_for_codex_exit", return_value=True) as wait, \
                 mock.patch("core.launcher_engine.subprocess.Popen", return_value=fake_process) as spawn, \
                 mock.patch("core.launcher_engine.time.sleep"), \
                 mock.patch.object(engine.supervisor, "register_launch") as register:
                order = mock.Mock()
                order.attach_mock(stop, "stop")
                order.attach_mock(wait, "wait")
                order.attach_mock(spawn, "spawn")
                ok, _ = engine.restart_codex()

            self.assertTrue(ok)
            self.assertEqual([call[0] for call in order.mock_calls[:3]], ["stop", "wait", "spawn"])
            register.assert_called_once()

    def test_codex_restart_cancels_if_old_root_has_not_exited(self):
        with tempfile.TemporaryDirectory() as root:
            engine = make_engine(root, allow_process_control=True)
            with mock.patch.object(engine, "preflight_proxy", return_value={"reachable": True}), \
                 mock.patch.object(engine, "preflight_codex", return_value={
                     "exe_found": True, "exe_path": r"C:\Codex\ChatGPT.exe",
                     "running": True, "pids": [901],
                 }), mock.patch.object(engine, "stop_codex", return_value=(True, "stopped")), \
                 mock.patch.object(engine, "_wait_for_codex_exit", return_value=False), \
                 mock.patch("core.launcher_engine.subprocess.Popen") as spawn:
                ok, message = engine.restart_codex()

            self.assertFalse(ok)
            self.assertIn("取消重启", message)
            spawn.assert_not_called()

    def test_confirmed_external_session_is_adopted_before_control(self):
        with tempfile.TemporaryDirectory() as root:
            engine = make_engine(root, allow_process_control=True)
            with mock.patch.object(
                engine,
                "adopt_session",
                return_value=(True, "adopted"),
            ) as adopt, mock.patch.object(
                engine,
                "stop_codex",
                return_value=(True, "stopped"),
            ) as stop:
                result = engine.adopt_and_stop("codex", 901)

            self.assertEqual(result, (True, "stopped"))
            adopt.assert_called_once_with("codex", 901)
            stop.assert_called_once_with()

    def test_failed_adoption_never_restarts_external_codex(self):
        with tempfile.TemporaryDirectory() as root:
            engine = make_engine(root, allow_process_control=True)
            with mock.patch.object(
                engine,
                "adopt_session",
                return_value=(False, "invalid root"),
            ), mock.patch.object(engine, "restart_codex") as restart:
                result = engine.adopt_and_restart_codex(901)

            self.assertEqual(result, (False, "invalid root"))
            restart.assert_not_called()

    def test_residual_cleanup_never_includes_active_tree(self):
        with tempfile.TemporaryDirectory() as root:
            engine = make_engine(root, allow_process_control=True)
            with mock.patch(
                "core.launcher_engine.get_codex_process_info",
                return_value={
                    "running": True,
                    "pids": [100, 101],
                    "residual_pids": [901, 902],
                    "ignored_pids": [999],
                },
            ), mock.patch(
                "core.launcher_engine.terminate_process_tree",
                return_value=(2, []),
            ) as terminate:
                ok, message = engine.cleanup_residuals("codex")

            self.assertTrue(ok)
            self.assertIn("2", message)
            terminate.assert_called_once_with([901, 902], protected_pids=[os.getpid()])


class ConfigMigrationTests(unittest.TestCase):
    def test_stale_preset_and_malformed_port_are_repaired(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({
                    "proxy": {"host": "127.0.0.1", "port": 7890, "preset": "Clash (7890)"},
                    "presets": [
                        {"name": "broken", "host": "127.0.0.1", "port": "not-a-number"},
                        {"name": "Clash / Mihomo (7890)", "host": "127.0.0.1", "port": 7890},
                    ],
                }, handle)

            config = ConfigManager(path)
            self.assertEqual(config.config["proxy"]["preset"], "Clash / Mihomo (7890)")
            self.assertTrue(config.config["apps"]["codex"]["components"]["websocket_proxy"])


if __name__ == "__main__":
    unittest.main()
