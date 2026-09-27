"""Launch Store desktop apps with package identity and an isolated environment."""
import base64
import ctypes
import json
import tempfile
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import time
import xml.etree.ElementTree as ET


def find_package_identity(executable):
    """Match the selected executable to its manifest (including custom paths)."""
    path = Path(executable).resolve()
    for root in list(path.parents)[:3]:
        manifest = root / "AppxManifest.xml"
        if not manifest.is_file():
            continue
        try:
            document = ET.parse(manifest)
        except (ET.ParseError, OSError) as exc:
            raise OSError(f"无法读取程序包清单: {exc}") from exc
        ns = {"p": "http://schemas.microsoft.com/appx/manifest/foundation/windows10"}
        identity = document.find("p:Identity", ns)
        if identity is None:
            raise OSError("程序包清单缺少 Identity")
        for app in document.findall("p:Applications/p:Application", ns):
            relative = app.get("Executable", "").replace("\\", "/")
            if relative and os.path.normcase(str((root / relative).resolve())) == os.path.normcase(str(path)):
                return {"name": identity.get("Name"), "root": str(root), "app_id": app.get("Id")}
        raise OSError("所选可执行文件未在程序包清单中注册")
    return None


def _literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def _encoded(script):
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


class PackageProcess:
    """Own a Windows process handle so polling cannot mistake a recycled PID."""
    def __init__(self, pid):
        self.pid = int(pid)
        self.returncode = None
        self._api = ctypes.WinDLL("kernel32", use_last_error=True)
        self._api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._api.OpenProcess.restype = wintypes.HANDLE
        self._api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        self._api.GetExitCodeProcess.restype = wintypes.BOOL
        self._api.CloseHandle.argtypes = [wintypes.HANDLE]
        self._handle = self._api.OpenProcess(0x1000, False, self.pid)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def poll(self):
        code = wintypes.DWORD()
        if not self._api.GetExitCodeProcess(self._handle, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.returncode = None if code.value == 259 else code.value
        return self.returncode

    def __del__(self):
        if getattr(self, "_handle", None):
            self._api.CloseHandle(self._handle)


def launch_packaged(executable, arguments, environment, identity):
    """Use a GUI-subsystem package helper to preserve identity and explicit env.

    Only the outer PowerShell runs, with CREATE_NO_WINDOW from creation time.
    Appx launches a WindowsApplication helper that never allocates a console.
    """
    from core.config_manager import get_resource_path
    helper = get_resource_path("assets", "package-launcher.exe")
    if not os.path.isfile(helper):
        raise OSError("缺少程序包启动组件，请运行 python build.py 后重试")
    powershell = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                              "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    with tempfile.TemporaryDirectory(prefix="harness-package-") as directory:
        request = os.path.join(directory, "request.json")
        result = os.path.join(directory, "result.json")
        with open(request, "w", encoding="utf-8") as handle:
            json.dump({"exe": executable, "args": subprocess.list2cmdline(arguments),
                       "env": [{"name": key, "value": value} for key, value in environment.items()]},
                      handle, ensure_ascii=False)
        script = f"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
try {{
    $package = Get-AppxPackage -Name {_literal(identity['name'])} | Where-Object {{ $_.InstallLocation -eq {_literal(identity['root'])} }} | Select-Object -First 1
    if (-not $package) {{ throw '所选程序包未注册或已被更新，请刷新安装路径' }}
    Invoke-CommandInDesktopPackage -PackageFamilyName $package.PackageFamilyName -AppId {_literal(identity['app_id'])} -Command {_literal(helper)} -Args {_literal(subprocess.list2cmdline([request, result]))} -PreventBreakaway
}} catch {{
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}}
"""
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        try:
            completed = subprocess.run(
                [powershell, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
                 "-EncodedCommand", _encoded(script)], env=environment,
                creationflags=subprocess.CREATE_NO_WINDOW, startupinfo=startup,
                capture_output=True, timeout=20,
            )
        except subprocess.TimeoutExpired as exc:
            raise OSError("程序包启动超时，请检查应用运行状态后重试") from exc
        if completed.returncode:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise OSError(f"程序包启动失败: {detail}")
        deadline = time.monotonic() + 10
        while not os.path.isfile(result) and time.monotonic() < deadline:
            time.sleep(0.05)
        if not os.path.isfile(result):
            raise OSError("程序包启动未返回应用 PID，请检查应用运行状态后重试")
        with open(result, encoding="utf-8-sig") as handle:
            response = json.load(handle)
        if response.get("error"):
            raise OSError(response["error"])
        return PackageProcess(response["pid"])
