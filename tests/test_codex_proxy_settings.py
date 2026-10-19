import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from core.codex_proxy import prepare_codex_proxy_settings
from core.config_manager import ConfigManager
from core.launcher_engine import LauncherEngine


class CodexProxySettingsTests(unittest.TestCase):
    def test_official_editor_repairs_both_routes_and_preserves_backup(self):
        with tempfile.TemporaryDirectory(prefix="harness 代理 '") as directory:
            root = Path(directory)
            cli = root / "app/resources/codex.exe"
            cli.parent.mkdir(parents=True)
            cli.touch()
            home = root / "home"
            home.mkdir()
            original = '# keep comments\nmodel = "test"\n[features]\nrespect_system_proxy = true\n'
            (home / "config.toml").write_text(original, encoding="utf-8")
            environment = {"CODEX_HOME": str(home), "HTTPS_PROXY": "http://127.0.0.1:7890"}
            states = {"respect_system_proxy": True, "system_proxy_fallback": True}

            def execute(command, **kwargs):
                self.assertEqual(kwargs["env"], environment)
                self.assertEqual(kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.assertEqual(kwargs["cwd"], str(cli.parent))
                if command[1:] == ["features", "list"]:
                    return SimpleNamespace(returncode=0, stdout="\n".join(
                        f"{key}   under development   {str(value).lower()}" for key, value in states.items()))
                self.assertEqual(command[1:3], ["features", "disable"])
                # The original must already be backed up before the first edit.
                backups = list((home / "harness-proxy-backups").glob("*"))
                self.assertEqual(len(backups), 1)
                self.assertEqual(backups[0].read_text(encoding="utf-8"), original)
                states[command[3]] = False
                return SimpleNamespace(returncode=0, stdout="")

            with mock.patch("core.codex_proxy.subprocess.run", side_effect=execute) as run:
                changed, backup = prepare_codex_proxy_settings(str(root / "app/ChatGPT.exe"), environment)
                self.assertEqual(changed, ("respect_system_proxy", "system_proxy_fallback"))
                self.assertTrue(Path(backup).is_file())
                self.assertEqual(run.call_count, 4)
                # Already repaired clients produce no further config writes.
                self.assertEqual(prepare_codex_proxy_settings(str(root / "app/ChatGPT.exe"), environment), ((), ""))
                self.assertEqual(run.call_count, 5)

    def test_legacy_client_without_bundled_cli_is_not_reconfigured(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch("core.codex_proxy.subprocess.run") as run:
                self.assertEqual(prepare_codex_proxy_settings(os.path.join(directory, "ChatGPT.exe"), {}), ((), ""))
                run.assert_not_called()

    def test_failed_inspection_never_edits_or_exposes_cli_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            cli = Path(directory) / "resources/codex.exe"
            cli.parent.mkdir()
            cli.touch()
            with mock.patch("core.codex_proxy.subprocess.run", return_value=SimpleNamespace(
                returncode=1, stdout="", stderr="private-config-value")) as run:
                with self.assertRaisesRegex(OSError, "退出码 1") as caught:
                    prepare_codex_proxy_settings(str(Path(directory) / "ChatGPT.exe"), {})
                self.assertNotIn("private-config-value", str(caught.exception))
                run.assert_called_once()

    def test_failed_repair_preserves_the_running_codex_session(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = LauncherEngine(ConfigManager(os.path.join(directory, "config.json")),
                                    session_state_file=os.path.join(directory, "sessions.json"),
                                    log_file=os.path.join(directory, "launcher.log"))
            with mock.patch.object(engine, "preflight_proxy", return_value={"reachable": True}), \
                 mock.patch.object(engine, "preflight_codex", return_value={
                     "exe_found": True, "exe_path": os.path.join(directory, "ChatGPT.exe"),
                     "running": True, "pids": [1234]}), \
                 mock.patch("core.launcher_engine.prepare_codex_proxy_settings", side_effect=OSError("配置不可写")), \
                 mock.patch.object(engine, "stop_codex") as stop, \
                 mock.patch("core.launcher_engine.subprocess.Popen") as spawn:
                self.assertFalse(engine.launch_codex(force_restart=True)[0])
                stop.assert_not_called()
                spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
