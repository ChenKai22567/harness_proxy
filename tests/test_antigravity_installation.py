import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from core.config_manager import ConfigManager
from core.launcher_engine import LauncherEngine
from core.process_detector import get_antigravity_installation_issues, get_antigravity_update_pids


class AntigravityInstallationTests(unittest.TestCase):
    def make_installation(self, directory, *, unpacked=False):
        root = Path(directory) / "Antigravity"
        for relative in ("Antigravity.exe", "icudtl.dat", "resources.pak",
                         "resources/app/package.json" if unpacked else "resources/app.asar"):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"test")
        return str(root / "Antigravity.exe")

    def make_engine(self, directory, exe, *, running=False):
        engine = LauncherEngine(
            ConfigManager(os.path.join(directory, "config.json")),
            session_state_file=os.path.join(directory, "sessions.json"),
            log_file=os.path.join(directory, "launcher.log"),
        )
        engine.log = mock.Mock()
        engine.preflight_proxy = mock.Mock(return_value={"reachable": True})
        engine.preflight_antigravity = mock.Mock(return_value={
            "exe_found": True, "exe_path": exe, "running": running, "pids": [701],
            "node_found": False, "npx_found": False,
        })
        return engine

    def test_hub_and_ide_resources_are_supported(self):
        for unpacked in (False, True):
            with self.subTest(unpacked=unpacked), tempfile.TemporaryDirectory() as directory:
                exe = self.make_installation(directory, unpacked=unpacked)
                self.assertEqual(get_antigravity_installation_issues(exe), [])

    def test_stalled_update_with_only_exe_reports_missing_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            exe = Path(directory) / "Antigravity.exe"
            exe.write_bytes(b"test")
            issues = get_antigravity_installation_issues(str(exe))
            self.assertIn("icudtl.dat", issues)
            self.assertIn("resources.pak", issues)
            self.assertTrue(any("app.asar" in issue for issue in issues))

    def test_empty_icu_is_not_considered_a_complete_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            exe = self.make_installation(directory)
            Path(exe).with_name("icudtl.dat").write_bytes(b"")
            self.assertEqual(get_antigravity_installation_issues(exe), ["icudtl.dat"])

    def test_updater_detection_ignores_other_installers_and_exited_records(self):
        with tempfile.TemporaryDirectory() as directory:
            updater = Path(directory) / "antigravity-updater"
            images = {
                1: str(updater / "pending" / "Antigravity-x64.exe"),
                2: str(Path(directory) / "other" / "installer.exe"),
                3: str(Path(directory) / "antigravity-updater-old" / "installer.exe"),
            }
            entries = [(1, 0, "antigravity-x64.exe", 4), (2, 0, "installer.exe", 4),
                       (3, 0, "installer.exe", 4), (4, 0, "installer.exe", 0)]
            fake_psutil = SimpleNamespace(
                Process=lambda pid: SimpleNamespace(exe=lambda: images[pid]), Error=RuntimeError,
            )
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": directory}), \
                 mock.patch("core.process_detector.get_process_snapshot_entries", return_value=entries), \
                 mock.patch("core.process_detector._get_psutil", return_value=fake_psutil):
                self.assertEqual(get_antigravity_update_pids(), [1])

    def test_incomplete_installation_is_blocked_before_stopping_a_session(self):
        with tempfile.TemporaryDirectory() as directory:
            exe = self.make_installation(directory)
            Path(exe).with_name("icudtl.dat").unlink()
            engine = self.make_engine(directory, exe, running=True)
            with mock.patch("core.launcher_engine.get_antigravity_update_pids", return_value=[]), \
                 mock.patch.object(engine, "stop_antigravity") as stop, \
                 mock.patch("core.launcher_engine.subprocess.Popen") as spawn:
                ok, message = engine.launch_antigravity(force_restart=True)
            self.assertFalse(ok)
            self.assertIn("icudtl.dat", message)
            stop.assert_not_called()
            spawn.assert_not_called()

    def test_update_starting_on_exit_prevents_replacement_spawn(self):
        with tempfile.TemporaryDirectory() as directory:
            exe = self.make_installation(directory)
            engine = self.make_engine(directory, exe, running=True)
            with mock.patch("core.launcher_engine.get_antigravity_update_pids", side_effect=[[], [900]]), \
                 mock.patch.object(engine, "stop_antigravity", return_value=(True, "stopped")), \
                 mock.patch.object(engine, "_wait_for_antigravity_exit", return_value=True), \
                 mock.patch("core.launcher_engine.subprocess.Popen") as spawn:
                ok, message = engine.launch_antigravity(force_restart=True)
            self.assertFalse(ok)
            self.assertIn("更新安装器", message)
            spawn.assert_not_called()

    def test_resources_removed_during_spawn_explain_breakpoint_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            exe = self.make_installation(directory)
            engine = self.make_engine(directory, exe)
            process = SimpleNamespace(returncode=2147483651, poll=lambda: 2147483651)
            def spawn(*args, **kwargs):
                Path(exe).with_name("icudtl.dat").unlink()
                return process
            with mock.patch("core.launcher_engine.get_antigravity_update_pids", return_value=[]), \
                 mock.patch("core.launcher_engine.subprocess.Popen", side_effect=spawn), \
                 mock.patch("core.launcher_engine.time.sleep"):
                ok, message = engine.launch_antigravity()
            self.assertFalse(ok)
            self.assertIn("0x80000003", message)
            self.assertIn("icudtl.dat", message)


if __name__ == "__main__":
    unittest.main()
