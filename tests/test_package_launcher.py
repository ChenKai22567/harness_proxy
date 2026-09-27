import os
import base64
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from core.config_manager import ConfigManager
from core.launcher_engine import LauncherEngine
from core.package_launcher import find_package_identity
from core.package_launcher import launch_packaged


class PackageLauncherTests(unittest.TestCase):
    def test_package_helper_is_gui_and_outer_shell_has_no_console(self):
        with tempfile.TemporaryDirectory(prefix="harness 中文 '") as directory:
            helper = Path(directory) / "package-launcher.exe"
            helper.touch()
            environment = {"HTTPS_PROXY": "http://127.0.0.1:7890", "PATH": "C:\\工具\\bin"}
            identity = {"name": "OpenAI.Codex", "root": "C:\\Store\\Codex", "app_id": "App"}
            def invoke(command, **kwargs):
                import subprocess
                import re
                script = base64.b64decode(command[-1]).decode("utf-16-le")
                self.assertIn("-Command '" + str(helper).replace("'", "''") + "'", script)
                self.assertNotIn("-Command 'C:\\Windows\\System32\\WindowsPowerShell", script)
                self.assertEqual(kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
                self.assertEqual(kwargs["startupinfo"].wShowWindow, subprocess.SW_HIDE)
                self.assertEqual(kwargs["env"], environment)
                self.assertIn("-PreventBreakaway", script)
                # The temporary request directory uses a fixed ASCII prefix.
                match = re.search(r"-Args '([^']+)'", script)
                paths = match.group(1).split('"')
                if len(paths) > 1:
                    request_path, result_path = paths[1], paths[3]
                else:
                    request_path, result_path = match.group(1).split()
                with open(request_path, encoding="utf-8") as handle:
                    request = json.load(handle)
                self.assertEqual({entry["name"]: entry["value"] for entry in request["env"]}, environment)
                self.assertEqual(request["exe"], "ChatGPT.exe")
                with open(result_path, "w", encoding="utf-8") as handle:
                    json.dump({"pid": 5678, "error": None}, handle)
                return SimpleNamespace(returncode=0, stderr=b"")
            with mock.patch("core.config_manager.get_resource_path", return_value=str(helper)), \
                 mock.patch("core.package_launcher.subprocess.run", side_effect=invoke), \
                 mock.patch("core.package_launcher.PackageProcess", side_effect=lambda pid: SimpleNamespace(pid=pid)):
                self.assertEqual(launch_packaged("ChatGPT.exe", ["--proxy-server=http://127.0.0.1:7890"],
                                                environment, identity).pid, 5678)

    def make_package(self, directory):
        root = Path(directory) / "OpenAI.Codex_26.924.0.0_x64__publisher"
        exe = root / "app" / "ChatGPT.exe"
        exe.parent.mkdir(parents=True)
        exe.touch()
        (root / "AppxManifest.xml").write_text('''
<Package xmlns="http://schemas.microsoft.com/appx/manifest/foundation/windows10">
  <Identity Name="OpenAI.Codex"/>
  <Applications>
    <Application Id="Other" Executable="app/Other.exe"/>
    <Application Id="Desktop" Executable="app\\ChatGPT.exe"/>
  </Applications>
</Package>''', encoding="utf-8")
        return str(exe), str(root)

    def test_identity_matches_manifest_executable_and_application(self):
        with tempfile.TemporaryDirectory() as directory:
            exe, root = self.make_package(directory)
            self.assertEqual(find_package_identity(exe), {
                "name": "OpenAI.Codex", "root": root, "app_id": "Desktop",
            })
            with self.assertRaises(OSError):
                find_package_identity(str(Path(root) / "app" / "Unknown.exe"))

    def test_unpacked_executable_has_no_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(find_package_identity(os.path.join(directory, "ChatGPT.exe")))

    def test_store_launch_keeps_proxy_gpu_policy_and_actual_child_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            exe, _ = self.make_package(directory)
            config = ConfigManager(os.path.join(directory, "config.json"))
            config.config["apps"]["codex"]["components"]["gpu_acceleration"] = False
            engine = LauncherEngine(config, session_state_file=os.path.join(directory, "sessions.json"),
                                    log_file=os.path.join(directory, "launcher.log"))
            process = SimpleNamespace(pid=1234, returncode=None, poll=lambda: None)
            with mock.patch.object(engine, "preflight_proxy", return_value={"reachable": True}), \
                 mock.patch.object(engine, "preflight_codex", return_value={"exe_found": True, "exe_path": exe, "running": False}), \
                 mock.patch("core.launcher_engine.launch_packaged", return_value=process) as packaged, \
                 mock.patch("core.launcher_engine.subprocess.Popen") as direct, \
                 mock.patch("core.launcher_engine.time.sleep"), \
                 mock.patch.object(engine.supervisor, "register_launch") as register:
                self.assertTrue(engine.launch_codex()[0])
                direct.assert_not_called()
                args = packaged.call_args.args
                self.assertIn("--disable-gpu", args[1])
                self.assertIn("--proxy-server=http://127.0.0.1:7890", args[1])
                self.assertEqual(args[2]["HTTPS_PROXY"], "http://127.0.0.1:7890")
                register.assert_called_once_with("codex", 1234, exe, "http://127.0.0.1:7890")
                packaged.side_effect = OSError("package activation failed")
                self.assertFalse(engine.launch_codex()[0])
                direct.assert_not_called()


if __name__ == "__main__":
    unittest.main()
