import os
import sys
import shutil
import re
import winreg
import ctypes
from ctypes import wintypes
from urllib.parse import urlparse
from typing import Optional, Dict, Any, List, Tuple


def _get_psutil():
    """Import psutil on first runtime inspection, after the UI is visible."""
    import psutil
    return psutil

# Windows API definitions for fast process snapshots (never blocks on individual processes)
class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ('dwSize', wintypes.DWORD),
        ('cntUsage', wintypes.DWORD),
        ('th32ProcessID', wintypes.DWORD),
        ('th32DefaultHeapID', ctypes.c_void_p),
        ('th32ModuleID', wintypes.DWORD),
        ('cntThreads', wintypes.DWORD),
        ('th32ParentProcessID', wintypes.DWORD),
        ('pcPriClassBase', wintypes.LONG),
        ('dwFlags', wintypes.DWORD),
        ('szExeFile', ctypes.c_wchar * 260)
    ]

def get_process_snapshot_entries() -> List[Tuple[int, int, str, int]]:
    """Return ``(pid, ppid, exe_name, thread_count)`` in one native snapshot.

    ``cntThreads`` is especially useful on Windows because an already-closed
    Store app can remain enumerable for a while with zero threads.  Reading it
    here avoids a separate, comparatively expensive ``psutil.status()`` and
    ``num_threads()`` query for every Electron helper.
    """
    entries: List[Tuple[int, int, str, int]] = []
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    hSnapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0) # TH32CS_SNAPPROCESS
    if not hSnapshot or hSnapshot == ctypes.c_void_p(-1).value:
        return entries

    pe = PROCESSENTRY32W()
    pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)

    try:
        if kernel32.Process32FirstW(hSnapshot, ctypes.byref(pe)):
            while True:
                try:
                    exe_name = str(pe.szExeFile).lower()
                    pid = int(pe.th32ProcessID)
                    entries.append((
                        pid,
                        int(pe.th32ParentProcessID),
                        exe_name,
                        int(pe.cntThreads),
                    ))
                except Exception:
                    pass
                if not kernel32.Process32NextW(hSnapshot, ctypes.byref(pe)):
                    break
    finally:
        kernel32.CloseHandle(hSnapshot)

    return entries


def get_running_process_entries() -> List[Tuple[int, int, str]]:
    """Return ``(pid, ppid, exe_name)`` without opening every process."""
    return [
        (pid, ppid, exe_name)
        for pid, ppid, exe_name, _thread_count in get_process_snapshot_entries()
    ]


def process_map_from_entries(
    entries: List[Tuple[int, int, str, int]],
) -> Dict[str, List[int]]:
    """Build the legacy name-to-PID view from one detailed snapshot."""
    proc_map: Dict[str, List[int]] = {}
    for pid, _ppid, exe_name, _thread_count in entries:
        proc_map.setdefault(exe_name, []).append(pid)
    return proc_map


def get_running_process_map() -> Dict[str, List[int]]:
    """
    Ultra-fast process snapshot using Win32 Toolhelp API.
    Takes ~50-90ms for hundreds of processes without calling OpenProcess on every PID.
    """
    return process_map_from_entries(get_process_snapshot_entries())

def resolve_display_icon(icon_val: str) -> Optional[str]:
    if not icon_val:
        return None
    expanded = os.path.expandvars(icon_val).strip()
    parts = expanded.split(",")
    candidate = parts[0].strip().strip('"')
    if os.path.isfile(candidate):
        return os.path.abspath(candidate)
    return None

def find_antigravity_executable(custom_path: str = "") -> Optional[str]:
    """Finds the Antigravity executable path via registry and local fallbacks."""
    if custom_path and os.path.isfile(custom_path):
        return os.path.abspath(custom_path)

    roots = [
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall", 0),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall", winreg.KEY_WOW64_64KEY),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall", winreg.KEY_WOW64_32KEY),
    ]

    for root, subkey, access_flag in roots:
        try:
            with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ | access_flag) as key:
                num_subkeys = winreg.QueryInfoKey(key)[0]
                for i in range(num_subkeys):
                    try:
                        child_name = winreg.EnumKey(key, i)
                        with winreg.OpenKey(key, child_name) as app_key:
                            try:
                                disp_name, _ = winreg.QueryValueEx(app_key, "DisplayName")
                                if disp_name and str(disp_name).strip().lower().startswith("antigravity"):
                                    try:
                                        icon_val, _ = winreg.QueryValueEx(app_key, "DisplayIcon")
                                        resolved = resolve_display_icon(icon_val)
                                        if resolved:
                                            return resolved
                                    except FileNotFoundError:
                                        pass
                                    try:
                                        loc, _ = winreg.QueryValueEx(app_key, "InstallLocation")
                                        if loc:
                                            cand = os.path.join(str(loc), "Antigravity.exe")
                                            if os.path.isfile(cand):
                                                return os.path.abspath(cand)
                                    except FileNotFoundError:
                                        pass
                            except FileNotFoundError:
                                pass
                    except Exception:
                        continue
        except Exception:
            continue

    # Fallback paths
    local_app = os.environ.get("LOCALAPPDATA", "")
    prog_files = os.environ.get("PROGRAMFILES", "")
    fallbacks = [
        os.path.join(local_app, "Programs", "Antigravity", "Antigravity.exe"),
        os.path.join(local_app, "Programs", "antigravity", "Antigravity.exe"),
        os.path.join(prog_files, "Antigravity", "Antigravity.exe"),
    ]
    for cand in fallbacks:
        if os.path.isfile(cand):
            return os.path.abspath(cand)

    return None

def find_codex_executable(custom_path: str = "") -> Tuple[Optional[str], Optional[str]]:
    """
    Finds Codex (ChatGPT) executable via direct registry AppModel lookup.
    Never spawns PowerShell console windows! Runs in < 1ms.
    """
    if custom_path and os.path.isfile(custom_path):
        return os.path.abspath(custom_path), os.path.dirname(os.path.abspath(custom_path))

    # Fast Registry Lookup for Store Package OpenAI.Codex
    package_candidates = []
    try:
        reg_key = r"Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppModel\Repository\Packages"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_key) as k:
            num = winreg.QueryInfoKey(k)[0]
            for i in range(num):
                sub = winreg.EnumKey(k, i)
                if sub.lower().startswith("openai.codex_"):
                    with winreg.OpenKey(k, sub) as pk:
                        try:
                            loc, _ = winreg.QueryValueEx(pk, "PackageRootFolder")
                            if loc and os.path.isdir(loc):
                                exe = os.path.join(loc, "app", "ChatGPT.exe")
                                if os.path.isfile(exe):
                                    package_candidates.append((sub, os.path.abspath(exe), os.path.abspath(loc)))
                                    continue
                                alt_exe = os.path.join(loc, "ChatGPT.exe")
                                if os.path.isfile(alt_exe):
                                    package_candidates.append((sub, os.path.abspath(alt_exe), os.path.abspath(loc)))
                        except Exception:
                            pass
    except Exception:
        pass

    if package_candidates:
        def _version_key(candidate):
            match = re.search(r"OpenAI\.Codex_([0-9.]+)_", candidate[0], re.IGNORECASE)
            if not match:
                return ()
            return tuple(int(part) for part in match.group(1).split(".") if part.isdigit())

        # Registry enumeration order is not a version guarantee. During Store
        # updates old and new package keys may briefly coexist, so explicitly
        # choose the highest valid installed version.
        _, exe, root = max(package_candidates, key=_version_key)
        return exe, root

    # Fallbacks
    local_app = os.environ.get("LOCALAPPDATA", "")
    prog_files = os.environ.get("PROGRAMFILES", "")
    candidates = [
        os.path.join(local_app, "Programs", "ChatGPT", "ChatGPT.exe"),
        os.path.join(local_app, "Programs", "OpenAI", "Codex", "ChatGPT.exe"),
        os.path.join(prog_files, "ChatGPT", "ChatGPT.exe"),
    ]
    for cand in candidates:
        if os.path.isfile(cand):
            return os.path.abspath(cand), os.path.dirname(os.path.abspath(cand))

    return None, None

def find_node_and_npx(custom_node_path: str = "", shim_dir: str = "") -> Dict[str, Any]:
    """Finds node.exe and real npx without spawning command windows."""
    node_path = None
    if custom_node_path and os.path.isfile(custom_node_path):
        node_path = os.path.abspath(custom_node_path)
    else:
        node_path = shutil.which("node")

    real_npx = None
    for name in ["npx.cmd", "npx.exe", "npx.ps1"]:
        found = shutil.which(name)
        if found:
            if shim_dir:
                try:
                    found_abs = os.path.abspath(found).lower()
                    shim_abs = os.path.abspath(shim_dir).lower()
                    if found_abs.startswith(shim_abs):
                        continue
                except Exception:
                    pass
            real_npx = os.path.abspath(found)
            break

    return {
        "node_found": node_path is not None and os.path.isfile(node_path),
        "node_path": node_path,
        "npx_found": real_npx is not None and os.path.isfile(real_npx),
        "npx_path": real_npx
    }

def get_antigravity_process_info(
    proc_map: Optional[Dict[str, List[int]]] = None,
    process_entries: Optional[List[Tuple[int, int, str, int]]] = None,
) -> Dict[str, Any]:
    """Fast Antigravity running check using snapshot map."""
    if proc_map is None:
        if process_entries is None:
            process_entries = get_process_snapshot_entries()
        proc_map = process_map_from_entries(process_entries)

    thread_counts = (
        {pid: threads for pid, _ppid, _name, threads in process_entries}
        if process_entries is not None else None
    )
    parent_map = (
        {pid: ppid for pid, ppid, _name, _threads in process_entries}
        if process_entries is not None else None
    )

    candidates = list(proc_map.get("antigravity.exe", []))
    has_proxy = False
    psutil = _get_psutil()
    root_pids = []
    ignored_pids = []

    for pid in candidates:
        try:
            process = psutil.Process(pid)
            if not _snapshot_pid_is_live(pid, thread_counts, process):
                ignored_pids.append(pid)
                continue
            command_line = process.cmdline()
            if command_line and not any(
                str(arg).lower().startswith("--type=") for arg in command_line[1:]
            ):
                root_pids.append(pid)
        except Exception:
            ignored_pids.append(pid)

    pids = [
        pid for pid in candidates
        if pid not in ignored_pids and _belongs_to_roots(pid, root_pids, psutil, parent_map)
    ]
    residual_pids = [
        pid for pid in candidates
        if pid not in ignored_pids and pid not in pids
    ]

    for pid in root_pids:
        try:
            p = psutil.Process(pid)
            cmd = p.cmdline()
            if any("--proxy-server" in arg for arg in cmd):
                has_proxy = True
                break
        except Exception:
            continue

    return {
        "running": bool(root_pids),
        "pids": pids,
        "root_pids": root_pids,
        "residual_pids": residual_pids,
        "ignored_pids": ignored_pids,
        "residual_running": bool(residual_pids),
        "has_proxy": has_proxy,
        "count": len(pids)
    }


def _is_root_process(pid: int, expected_name: str) -> bool:
    psutil = _get_psutil()
    try:
        process = psutil.Process(pid)
        if not _is_live_process(process):
            return False
        command_line = process.cmdline()
        return bool(command_line) and process.name().lower() == expected_name and not any(
            str(arg).lower().startswith("--type=") for arg in command_line[1:]
        )
    except Exception:
        return False


def _is_live_process(process) -> bool:
    """Reject Windows' zero-thread, already-terminated process artifacts."""
    psutil = _get_psutil()
    try:
        inactive = {
            value for value in (
                getattr(psutil, "STATUS_DEAD", None),
                getattr(psutil, "STATUS_STOPPED", None),
                getattr(psutil, "STATUS_ZOMBIE", None),
            ) if value
        }
        if process.status() in inactive:
            return False
        return process.is_running() and process.num_threads() > 0
    except Exception:
        return False


def _snapshot_pid_is_live(
    pid: int,
    thread_counts: Optional[Dict[int, int]],
    process,
) -> bool:
    """Use the native snapshot when available; fall back for injected maps."""
    if thread_counts is not None and pid in thread_counts:
        return thread_counts[pid] > 0
    return _is_live_process(process)


def _belongs_to_roots(
    pid: int,
    root_pids: List[int],
    psutil,
    parent_map: Optional[Dict[int, int]] = None,
) -> bool:
    roots = set(int(value) for value in root_pids)
    current_pid = int(pid)
    visited = set()
    for _ in range(24):
        if current_pid in roots:
            return True
        if current_pid <= 0 or current_pid in visited:
            return False
        visited.add(current_pid)
        if parent_map is not None:
            current_pid = int(parent_map.get(current_pid, 0))
        else:
            try:
                current_pid = int(psutil.Process(current_pid).ppid())
            except Exception:
                return False
    return False


def _proxy_value_matches(value: str, proxy_port: int) -> bool:
    if not value:
        return False
    raw = str(value).strip()
    try:
        parsed = urlparse(raw if "://" in raw else f"http://{raw}")
        if parsed.port == int(proxy_port):
            return True
    except (TypeError, ValueError):
        pass
    return f":{int(proxy_port)}" in raw


def get_windows_user_proxy_info(proxy_port: int = 7890) -> Dict[str, Any]:
    """Read the current-user Windows proxy without changing system state."""
    try:
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            enabled = bool(winreg.QueryValueEx(key, "ProxyEnable")[0])
            try:
                server = str(winreg.QueryValueEx(key, "ProxyServer")[0])
            except FileNotFoundError:
                server = ""
        return {
            "enabled": enabled,
            "server": server,
            "matches": enabled and _proxy_value_matches(server, proxy_port),
        }
    except Exception:
        return {"enabled": False, "server": "", "matches": False}


def get_codex_process_info(
    proc_map: Optional[Dict[str, List[int]]] = None,
    proxy_port: int = 7890,
    process_entries: Optional[List[Tuple[int, int, str, int]]] = None,
) -> Dict[str, Any]:
    """Check active desktop roots separately from stopped/orphan artifacts."""
    if proc_map is None:
        if process_entries is None:
            process_entries = get_process_snapshot_entries()
        proc_map = process_map_from_entries(process_entries)

    thread_counts = (
        {pid: threads for pid, _ppid, _name, threads in process_entries}
        if process_entries is not None else None
    )
    parent_map = (
        {pid: ppid for pid, ppid, _name, _threads in process_entries}
        if process_entries is not None else None
    )

    chatgpt_pids = list(proc_map.get("chatgpt.exe", []))
    codex_candidates = list(proc_map.get("codex.exe", []))
    psutil = _get_psutil()
    root_pids: List[int] = []
    root_started: Dict[int, float] = {}
    app_server_pids: List[int] = []
    ignored_pids: List[int] = []
    browser_proxy = False
    api_proxy = False
    websocket_proxy = False

    for pid in chatgpt_pids:
        try:
            process = psutil.Process(pid)
            if not _snapshot_pid_is_live(pid, thread_counts, process):
                ignored_pids.append(pid)
                continue
            command_line = process.cmdline()
            if not command_line:
                ignored_pids.append(pid)
                continue
            is_root = not any(str(arg).lower().startswith("--type=") for arg in command_line[1:])
            if is_root:
                root_pids.append(pid)
                try:
                    root_started[pid] = float(process.create_time())
                except Exception:
                    root_started[pid] = 0.0
                if any(
                    str(arg).lower().startswith("--proxy-server=")
                    and _proxy_value_matches(str(arg).split("=", 1)[-1], proxy_port)
                    for arg in command_line
                ):
                    browser_proxy = True
        except Exception:
            ignored_pids.append(pid)

    # Multiple Store sessions can coexist briefly after an update. Present
    # the newest usable root as the primary session instead of a stale,
    # suspended shell that happens to have the lower PID/enumeration order.
    root_pids.sort(key=lambda pid: root_started.get(pid, 0.0), reverse=True)

    active_chatgpt_pids = [
        pid for pid in chatgpt_pids
        if pid not in ignored_pids and _belongs_to_roots(pid, root_pids, psutil, parent_map)
    ]
    codex_pids = []
    residual_codex_pids = []
    for pid in codex_candidates:
        try:
            process = psutil.Process(pid)
            if not _snapshot_pid_is_live(pid, thread_counts, process):
                ignored_pids.append(pid)
                continue
            command_line = [str(arg).lower() for arg in process.cmdline()]
            is_app_server = "app-server" in command_line
            is_desktop_helper = is_app_server and any(
                "mcp_servers.codex_app=" in arg or "codex_app_tools" in arg
                for arg in command_line
            )
            if _belongs_to_roots(pid, root_pids, psutil, parent_map):
                codex_pids.append(pid)
            elif is_desktop_helper:
                residual_codex_pids.append(pid)
        except Exception:
            ignored_pids.append(pid)

    residual_chatgpt_pids = [
        pid for pid in chatgpt_pids
        if pid not in ignored_pids and pid not in active_chatgpt_pids
    ]
    residual_pids = sorted(set(residual_chatgpt_pids + residual_codex_pids))
    pids = active_chatgpt_pids + codex_pids

    for pid in codex_pids:
        try:
            process = psutil.Process(pid)
            command_line = process.cmdline()
            if "app-server" not in [str(arg).lower() for arg in command_line]:
                continue
            app_server_pids.append(pid)
            environment = process.environ()
            standard_proxy = any(
                _proxy_value_matches(environment.get(key, ""), proxy_port)
                for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
            )
            api_proxy = api_proxy or standard_proxy
            # Codex maps wss:// routing to its HTTPS proxy route.  WS_PROXY and
            # WSS_PROXY are not required by the current app-server, but retain
            # recognition for older/custom builds that may expose them.
            websocket_proxy = websocket_proxy or any(
                _proxy_value_matches(environment.get(key, ""), proxy_port)
                for key in ("WS_PROXY", "WSS_PROXY", "ws_proxy", "wss_proxy")
            ) or standard_proxy
        except Exception:
            continue

    system_proxy = get_windows_user_proxy_info(proxy_port)
    app_server_ready = bool(app_server_pids) and api_proxy
    websocket_ready = bool(app_server_pids) and (websocket_proxy or system_proxy["matches"])
    has_proxy = bool(root_pids) and browser_proxy and app_server_ready and websocket_ready
    if not root_pids:
        proxy_mode = "应用已退出（存在可清理残留）" if residual_pids else "应用未运行"
    elif api_proxy and websocket_proxy:
        proxy_mode = "独立进程代理（API / WebSocket）"
    elif system_proxy["matches"]:
        proxy_mode = "Windows 系统代理（进程环境缺失）"
    elif api_proxy or browser_proxy:
        proxy_mode = "代理不完整"
    else:
        proxy_mode = "未检测到代理"

    return {
        "running": bool(root_pids),
        "pids": pids,
        "root_pids": root_pids,
        "app_server_pids": app_server_pids,
        "residual_pids": residual_pids,
        "ignored_pids": sorted(set(ignored_pids)),
        "residual_running": bool(residual_pids),
        "has_proxy": has_proxy,
        "browser_proxy": browser_proxy,
        "api_proxy": api_proxy,
        "websocket_proxy": websocket_proxy,
        "system_proxy": system_proxy,
        "proxy_mode": proxy_mode,
        "proxy_connections": 0,
        "count": len(pids)
    }

def terminate_process_tree(
    pids: List[int],
    timeout: float = 2.0,
    *,
    protected_pids: Optional[List[int]] = None,
) -> Tuple[int, List[str]]:
    """Terminate a process forest once, without repeatedly walking children.

    The previous implementation recursively enumerated the same Electron tree
    once for every matching PID. With 10+ renderer/utility processes this was
    effectively quadratic and could make a restart take several seconds.
    """
    psutil = _get_psutil()
    killed_count = 0
    errors = []
    seed_processes = {}
    for pid in dict.fromkeys(pids):
        try:
            process = psutil.Process(pid)
            seed_processes[process.pid] = process
        except psutil.NoSuchProcess:
            continue
        except Exception as e:
            errors.append(f"PID {pid}: {e}")

    seed_ids = set(seed_processes)
    roots = []
    for process in seed_processes.values():
        try:
            if process.ppid() not in seed_ids:
                roots.append(process)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            roots.append(process)

    targets = dict(seed_processes)
    for root in roots:
        try:
            for child in root.children(recursive=True):
                targets[child.pid] = child
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    # A launcher can itself be started from a Codex terminal.  In that case it
    # is a real descendant of the Codex root, but killing it midway through a
    # restart prevents the replacement process from ever being spawned.  If a
    # protected process occurs inside the target forest, preserve both it and
    # its subtree.  When the launcher is the normal parent of Codex there is no
    # intersection, so the Codex subtree remains fully controllable.
    protected_ids = {int(pid) for pid in (protected_pids or [])}
    protected_subtree = set(protected_ids)
    for pid in protected_ids.intersection(targets):
        try:
            protected = psutil.Process(pid)
            protected_subtree.update(child.pid for child in protected.children(recursive=True))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    targets = {
        pid: process for pid, process in targets.items()
        if pid not in protected_subtree
    }

    for process in targets.values():
        try:
            process.kill()
            killed_count += 1
        except psutil.NoSuchProcess:
            continue
        except Exception as e:
            errors.append(f"PID {process.pid}: {e}")

    if targets:
        try:
            psutil.wait_procs(list(targets.values()), timeout=max(0.0, min(timeout, 1.0)))
        except Exception:
            pass
    return killed_count, errors
