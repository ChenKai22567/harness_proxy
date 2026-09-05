import os
import time
import subprocess
import threading
from datetime import datetime
from typing import Optional, Callable, Dict, Any, List, Tuple

from core.config_manager import (
    DEFAULT_PROXY_TIMEOUT_MS,
    ConfigManager,
    get_app_root_dir,
    get_resource_path,
)
from core.proxy_prober import probe_tcp_port, probe_https_via_proxy
from core.process_detector import (
    find_antigravity_executable,
    find_codex_executable,
    find_node_and_npx,
    get_antigravity_process_info,
    get_codex_process_info,
    terminate_process_tree
)
from core.process_classifier import collect_component_snapshot
from core.supervisor import SessionSupervisor

# On-disk log rotation: one active file plus this many rotated backups.
LOG_ROTATE_BYTES = 1024 * 1024
LOG_ROTATE_BACKUPS = 5

_NODE_BOOTSTRAP_MARKER = "node-proxy-bootstrap.cjs"


def _merge_node_options(existing: str, bootstrap_path: str) -> str:
    """Attach the bootstrap ``--require`` to NODE_OPTIONS.

    Requires pointing at an older install of the same bootstrap are replaced
    instead of deduplicated by filename: after the launcher moves, a stale
    path would both skip the new injection and break every Node start with
    "Cannot find module".  Unrelated ``--require`` entries are preserved.
    """
    require_arg = f'--require="{bootstrap_path}"'
    if not existing:
        return require_arg
    kept = [
        part for part in existing.split()
        if not (
            part.lower().startswith("--require=")
            and _NODE_BOOTSTRAP_MARKER in part.lower()
        )
    ]
    kept.append(require_arg)
    return " ".join(kept)


class LauncherEngine:
    def __init__(
        self,
        config_mgr: ConfigManager,
        log_callback: Optional[Callable[[str, str], None]] = None,
        allow_process_control: bool = True,
        session_state_file: Optional[str] = None,
        log_file: Optional[str] = None,
    ):
        self.config_mgr = config_mgr
        self.log_callback = log_callback
        self.app_root = get_app_root_dir()
        self.assets_dir = get_resource_path("assets")
        self.shims_dir = os.path.join(self.assets_dir, "shims")
        self.node_bootstrap = os.path.join(self.assets_dir, "node-proxy-bootstrap.cjs")
        self.log_dir = os.path.join(self.app_root, "logs")
        os.makedirs(self.log_dir, exist_ok=True)
        self.log_file = log_file or os.path.join(self.log_dir, "launcher.log")
        self._log_lock = threading.Lock()
        self.allow_process_control = allow_process_control
        self.supervisor = SessionSupervisor(
            session_state_file or os.path.join(self.app_root, "managed_sessions.json"),
            enabled=allow_process_control,
        )

        # Cached paths for zero-lag monitoring.  Resolution is intentionally
        # lazy: the registry/PATH scans run inside the first background
        # preflight (or a manual refresh) instead of delaying the first
        # window paint; ``preflight_*`` triggers them when needed.
        self.cached_anti_path: Optional[str] = None
        self.cached_codex_path: Optional[str] = None
        self.cached_codex_dir: Optional[str] = None
        self.cached_node_info: Optional[Dict[str, Any]] = None
        self._paths_initialized = False
        self._path_cache_lock = threading.RLock()

    def refresh_cached_paths(self):
        """Resolves executable paths once and caches them."""
        with self._path_cache_lock:
            try:
                cfg_a = self.config_mgr.config.get("apps", {}).get("antigravity", {})
                if cfg_a.get("auto_detect", True):
                    self.cached_anti_path = find_antigravity_executable("")
                else:
                    custom_anti = cfg_a.get("custom_path", "")
                    self.cached_anti_path = (
                        os.path.abspath(custom_anti)
                        if custom_anti and os.path.isfile(custom_anti)
                        else None
                    )
                self.cached_node_info = find_node_and_npx(cfg_a.get("custom_node_path", ""), self.shims_dir)

                self._refresh_codex_path_locked()
            except Exception as e:
                self.log(f"刷新路径缓存异常: {e}", "WARN")
            finally:
                # A valid "not installed" result must also be cached. Otherwise
                # a missing application triggers a full registry scan every
                # monitor cycle.
                self._paths_initialized = True

    def _refresh_codex_path_locked(self):
        cfg_c = self.config_mgr.config.get("apps", {}).get("codex", {})
        if cfg_c.get("auto_detect", True):
            self.cached_codex_path, self.cached_codex_dir = find_codex_executable("")
        else:
            custom_codex = cfg_c.get("custom_path", "")
            if custom_codex and os.path.isfile(custom_codex):
                self.cached_codex_path = os.path.abspath(custom_codex)
                self.cached_codex_dir = os.path.dirname(self.cached_codex_path)
            else:
                self.cached_codex_path, self.cached_codex_dir = None, None

    def refresh_codex_path(self):
        """Refresh only the versioned Codex/MSIX path after an app update."""
        with self._path_cache_lock:
            self._refresh_codex_path_locked()
            self._paths_initialized = True
            return self.cached_codex_path, self.cached_codex_dir

    def _build_proxy_environment(
        self,
        *,
        include_runtime_extensions: bool = False,
    ) -> Tuple[Dict[str, str], str, str, str]:
        """Build an isolated proxy environment for a launched application.

        Codex resolves secure WebSocket traffic through the standard
        ``HTTPS_PROXY``/``HTTP_PROXY``/``ALL_PROXY`` chain.  Keep its process
        environment aligned with the known-good legacy launcher and do not
        inject non-standard WS(S) variables or Node/Electron proxy switches.
        Antigravity can still opt into those compatibility extensions.
        """
        proxy_url = self.config_mgr.get_proxy_url()
        env_bypass = self.config_mgr.get_proxy_bypass_for_env()
        chrom_bypass = self.config_mgr.get_proxy_bypass_for_chromium()

        # A launcher can itself be running under a different proxy setup.  Do
        # not leak stale or incompatible proxy switches into a new Codex tree.
        managed_keys = {
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "WS_PROXY",
            "WSS_PROXY",
            "ELECTRON_GET_USE_PROXY",
            "NODE_USE_ENV_PROXY",
        }
        env = {
            key: value
            for key, value in os.environ.items()
            if key.upper() not in managed_keys
        }
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            env[key] = proxy_url
        env["NO_PROXY"] = env_bypass

        if include_runtime_extensions:
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "WS_PROXY", "WSS_PROXY"):
                env[key] = proxy_url
                env[key.lower()] = proxy_url
            env["no_proxy"] = env_bypass
            env["ELECTRON_GET_USE_PROXY"] = "1"
            env["NODE_USE_ENV_PROXY"] = "1"
        return env, proxy_url, env_bypass, chrom_bypass

    def log(self, message: str, level: str = "INFO"):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        formatted = f"[{timestamp}] [{level}] {message}"
        with self._log_lock:
            try:
                self._rotate_log_locked()
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write(formatted + "\n")
            except Exception:
                pass
        if self.log_callback:
            try:
                self.log_callback(formatted, level)
            except Exception:
                pass
        # A frozen --noconsole build may expose no usable stdout.
        try:
            print(formatted)
        except Exception:
            pass

    def _rotate_log_locked(self):
        """Shift launcher.log to launcher.log.1..N once it exceeds the cap."""
        try:
            if os.path.getsize(self.log_file) < LOG_ROTATE_BYTES:
                return
        except OSError:
            return
        try:
            for index in range(LOG_ROTATE_BACKUPS - 1, 0, -1):
                source = f"{self.log_file}.{index}"
                if os.path.exists(source):
                    os.replace(source, f"{self.log_file}.{index + 1}")
            os.replace(self.log_file, f"{self.log_file}.1")
        except OSError:
            pass

    def preflight_proxy(self) -> Dict[str, Any]:
        cfg = self.config_mgr.config["proxy"]
        host = cfg.get("host", "127.0.0.1")
        port = cfg.get("port", 7890)
        timeout = cfg.get("timeout_ms", DEFAULT_PROXY_TIMEOUT_MS)

        reachable, latency, msg = probe_tcp_port(host, port, timeout_ms=timeout)
        return {
            "reachable": reachable,
            "host": host,
            "port": port,
            "latency_ms": latency,
            "message": msg
        }

    # ==========================================
    # Antigravity Controls
    # ==========================================
    def preflight_antigravity(
        self,
        proc_map: Optional[Dict[str, List[int]]] = None,
        process_entries: Optional[List[Tuple[int, int, str, int]]] = None,
    ) -> Dict[str, Any]:
        if not self._paths_initialized:
            self.refresh_cached_paths()

        proc_info = get_antigravity_process_info(proc_map, process_entries=process_entries)
        node_info = self.cached_node_info or {"node_found": False, "node_path": "", "npx_found": False, "npx_path": ""}

        owned_pids = self.supervisor.owned_pids("antigravity")
        return {
            "exe_found": self.cached_anti_path is not None,
            "exe_path": self.cached_anti_path,
            "running": proc_info["running"],
            "pids": proc_info["pids"],
            "root_pids": proc_info.get("root_pids", []),
            "residual_pids": proc_info.get("residual_pids", []),
            "ignored_pids": proc_info.get("ignored_pids", []),
            "residual_running": proc_info.get("residual_running", False),
            "owned_pids": owned_pids,
            "managed": bool(owned_pids),
            "has_proxy": proc_info["has_proxy"],
            "node_found": node_info.get("node_found", False),
            "npx_found": node_info.get("npx_found", False),
            "node_path": node_info.get("node_path", ""),
            "npx_path": node_info.get("npx_path", "")
        }

    def launch_antigravity(self, force_restart: bool = False) -> Tuple[bool, str]:
        if not self.allow_process_control:
            return False, "当前运行环境禁止真实进程控制"
        self.log("准备启动 Antigravity...", "INFO")
        proxy_check = self.preflight_proxy()
        check_clash = self.config_mgr.config["launcher"].get("check_clash_before_launch", True)

        if check_clash and not proxy_check["reachable"]:
            msg = f"Clash 代理未启动或端口 {proxy_check['host']}:{proxy_check['port']} 未监听 ({proxy_check['message']})"
            self.log(msg, "ERROR")
            return False, msg

        anti_info = self.preflight_antigravity()
        if not anti_info["exe_found"]:
            msg = "未找到 Antigravity 可执行文件，请在设置中手动指定路径。"
            self.log(msg, "ERROR")
            return False, msg

        exe_path = anti_info["exe_path"]

        if anti_info["running"]:
            if force_restart:
                self.log("正在安全终止已运行的 Antigravity 进程...", "WARN")
                stopped, stop_message = self.stop_antigravity()
                if not stopped:
                    return False, stop_message
                if not self._wait_for_antigravity_exit():
                    msg = "旧 Antigravity 进程未在安全期限内退出，已取消重启以避免产生并行会话"
                    self.log(msg, "ERROR")
                    return False, msg
            else:
                msg = f"Antigravity 已经在运行中 (PID: {anti_info['pids']})。如需重启请点击【重启】。"
                self.log(msg, "WARN")
                return False, msg

        env, proxy_url, _env_bypass, chrom_bypass = self._build_proxy_environment(
            include_runtime_extensions=True,
        )
        env["ANTIGRAVITY_PROXY_URL"] = proxy_url
        env["ANTIGRAVITY_PROXY_BYPASS"] = chrom_bypass

        # Browser proxy injection shim
        cfg = self.config_mgr.config["apps"]["antigravity"]
        enable_shim = cfg.get("enable_browser_shim", True)
        if enable_shim and anti_info["node_found"] and anti_info["npx_found"]:
            env["ANTIGRAVITY_REAL_NPX"] = anti_info["npx_path"]
            env["PATH"] = f"{self.shims_dir};{env.get('PATH', '')}"
            bootstrap_path = self.node_bootstrap.replace("\\", "/")
            env["NODE_OPTIONS"] = _merge_node_options(env.get("NODE_OPTIONS", ""), bootstrap_path)
            self.log(f"已装载浏览器 DevTools 代理扩展支持 (Node: {anti_info['node_path']})", "INFO")
        else:
            self.log("跳过浏览器扩展代理补丁 (Node/npx 未检测到或已禁用)", "WARN")

        chromium_args = [
            exe_path,
            f"--proxy-server={proxy_url}",
            f"--proxy-bypass-list={chrom_bypass}"
        ]
        if not cfg.get("components", {}).get("gpu_acceleration", True):
            chromium_args.append("--disable-gpu")

        self.log(f"派生 Antigravity 进程，代理参数: --proxy-server={proxy_url}", "INFO")

        try:
            DETACHED = 0x00000008 | 0x00000200
            proc = subprocess.Popen(
                chromium_args,
                cwd=os.path.dirname(exe_path),
                env=env,
                creationflags=DETACHED,
                close_fds=True
            )
            time.sleep(0.35)

            if proc.poll() is not None:
                msg = f"Antigravity 启动后立即退出，退出代码: {proc.returncode}"
                self.log(msg, "ERROR")
                return False, msg

            self.supervisor.register_launch("antigravity", proc.pid, exe_path, proxy_url)
            self.log(f"Antigravity 启动成功！(初始 PID: {proc.pid})", "INFO")
            return True, f"启动成功 (PID: {proc.pid})"
        except Exception as e:
            msg = f"启动 Antigravity 失败: {str(e)}"
            self.log(msg, "ERROR")
            return False, msg

    def stop_antigravity(self) -> Tuple[bool, str]:
        if not self.allow_process_control:
            return False, "当前运行环境禁止真实进程控制"
        info = get_antigravity_process_info()
        if not info["running"]:
            return True, "Antigravity 当前未运行"
        owned_pids = self.supervisor.owned_pids("antigravity")
        if not owned_pids:
            msg = "检测到外部 Antigravity 实例；为避免误杀，需先在组件管理中接管会话。"
            self.log(msg, "WARN")
            return False, msg
        killed, errors = terminate_process_tree(owned_pids, protected_pids=[os.getpid()])
        for error in errors:
            self.log(f"停止 Antigravity 时部分进程未结束: {error}", "WARN")
        self.supervisor.forget("antigravity")
        self.log(f"已结束 Antigravity 相关进程 ({killed} 个)", "INFO")
        return True, f"已终止 {killed} 个进程"

    def restart_antigravity(self) -> Tuple[bool, str]:
        return self.launch_antigravity(force_restart=True)

    @staticmethod
    def _wait_for_antigravity_exit(timeout: float = 4.0) -> bool:
        """Wait for the old Electron root to release before relaunching.

        Mirrors the Codex restart guard: relaunching into a tree that is
        still dying makes the new instance defer to the old single-instance
        lock and exit immediately.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            if not get_antigravity_process_info()["running"]:
                return True
            time.sleep(0.1)
        return not get_antigravity_process_info()["running"]

    # ==========================================
    # Codex (ChatGPT) Controls
    # ==========================================
    def preflight_codex(
        self,
        proc_map: Optional[Dict[str, List[int]]] = None,
        process_entries: Optional[List[Tuple[int, int, str, int]]] = None,
    ) -> Dict[str, Any]:
        # Microsoft Store updates replace the versioned WindowsApps directory.
        # A launcher kept running across that update otherwise retains a path
        # that no longer exists and Popen fails with WinError 2.
        if (
            not self._paths_initialized
            or not self.cached_codex_path
            or not os.path.isfile(self.cached_codex_path)
        ):
            old_path = self.cached_codex_path
            self.refresh_codex_path()
            if old_path and self.cached_codex_path and old_path != self.cached_codex_path:
                self.log("检测到 Codex 已更新，已自动切换到新版本路径。", "INFO")

        port = self.config_mgr.config["proxy"].get("port", 7890)
        proc_info = get_codex_process_info(
            proc_map,
            proxy_port=port,
            process_entries=process_entries,
        )

        owned_pids = self.supervisor.owned_pids("codex")
        return {
            "exe_found": self.cached_codex_path is not None,
            "exe_path": self.cached_codex_path,
            "install_dir": self.cached_codex_dir,
            "running": proc_info["running"],
            "pids": proc_info["pids"],
            "root_pids": proc_info.get("root_pids", []),
            "app_server_pids": proc_info.get("app_server_pids", []),
            "residual_pids": proc_info.get("residual_pids", []),
            "ignored_pids": proc_info.get("ignored_pids", []),
            "residual_running": proc_info.get("residual_running", False),
            "owned_pids": owned_pids,
            "managed": bool(owned_pids),
            "has_proxy": proc_info["has_proxy"],
            "browser_proxy": proc_info.get("browser_proxy", False),
            "api_proxy": proc_info.get("api_proxy", False),
            "websocket_proxy": proc_info.get("websocket_proxy", False),
            "system_proxy": proc_info.get("system_proxy", {}),
            "proxy_mode": proc_info.get("proxy_mode", "未知"),
            "proxy_connections": 0
        }

    def launch_codex(self, force_restart: bool = False) -> Tuple[bool, str]:
        if not self.allow_process_control:
            return False, "当前运行环境禁止真实进程控制"
        self.log("准备启动 Codex...", "INFO")
        proxy_check = self.preflight_proxy()
        check_clash = self.config_mgr.config["launcher"].get("check_clash_before_launch", True)

        if check_clash and not proxy_check["reachable"]:
            msg = f"Clash 代理未启动或端口 {proxy_check['host']}:{proxy_check['port']} 未监听"
            self.log(msg, "ERROR")
            return False, msg

        codex_info = self.preflight_codex()
        if not codex_info["exe_found"]:
            msg = "未找到 Codex (ChatGPT) 安装，请在设置中手动指定 ChatGPT.exe 路径。"
            self.log(msg, "ERROR")
            return False, msg

        exe_path = codex_info["exe_path"]

        if codex_info["running"]:
            if force_restart:
                self.log("正在安全终止已运行的 Codex 进程...", "WARN")
                stopped, stop_message = self.stop_codex()
                if not stopped:
                    return False, stop_message
                if not self._wait_for_codex_exit():
                    msg = "旧 Codex 进程未在安全期限内退出，已取消重启以避免产生并行会话"
                    self.log(msg, "ERROR")
                    return False, msg
            else:
                msg = f"Codex 已经在运行中 (PID: {codex_info['pids']})。如需重启请点击【重启】。"
                self.log(msg, "WARN")
                return False, msg

        env, proxy_url, _env_bypass, chrom_bypass = self._build_proxy_environment()

        self.log(f"派生 Codex 进程，参数: --proxy-server={proxy_url}", "INFO")

        DETACHED = 0x00000008 | 0x00000200

        codex_cfg = self.config_mgr.config.get("apps", {}).get("codex", {})

        def _spawn(path: str):
            arguments = [
                path,
                f"--proxy-server={proxy_url}",
                f"--proxy-bypass-list={chrom_bypass}",
            ]
            if not codex_cfg.get("components", {}).get("gpu_acceleration", True):
                arguments.append("--disable-gpu")
            return subprocess.Popen(
                arguments,
                cwd=os.path.dirname(path),
                env=env,
                creationflags=DETACHED,
                close_fds=True
            )

        try:
            proc = _spawn(exe_path)
        except OSError as first_error:
            # Close the small race where an MSIX update swaps directories after
            # preflight but immediately before process creation.
            if getattr(first_error, "winerror", None) != 2:
                msg = f"启动 Codex 失败: {str(first_error)}"
                self.log(msg, "ERROR")
                return False, msg

            old_path = exe_path
            new_path, _ = self.refresh_codex_path()
            if not new_path or new_path == old_path or not os.path.isfile(new_path):
                msg = f"启动 Codex 失败: {str(first_error)}"
                self.log(msg, "ERROR")
                return False, msg

            try:
                self.log("Codex 安装路径刚刚发生变化，正在使用新版本重试...", "INFO")
                exe_path = new_path
                proc = _spawn(exe_path)
            except OSError as retry_error:
                msg = f"启动 Codex 失败: {str(retry_error)}"
                self.log(msg, "ERROR")
                return False, msg

        try:
            time.sleep(0.35)

            if proc.poll() is not None:
                msg = f"Codex 启动后立即退出，代码: {proc.returncode}"
                self.log(msg, "ERROR")
                return False, msg

            self.supervisor.register_launch("codex", proc.pid, exe_path, proxy_url)
            self.log(f"Codex 启动成功！(初始 PID: {proc.pid})", "INFO")
            return True, f"启动成功 (PID: {proc.pid})"
        except Exception as e:
            msg = f"启动 Codex 失败: {str(e)}"
            self.log(msg, "ERROR")
            return False, msg

    def stop_codex(self) -> Tuple[bool, str]:
        if not self.allow_process_control:
            return False, "当前运行环境禁止真实进程控制"
        info = get_codex_process_info()
        if not info["running"]:
            return True, "Codex 当前未运行"
        owned_pids = self.supervisor.owned_pids("codex")
        if not owned_pids:
            msg = "检测到外部或旧版 Codex 会话；为保护当前对话，需先在组件管理中接管会话。"
            self.log(msg, "WARN")
            return False, msg
        if os.getpid() in owned_pids:
            self.log("检测到启动器位于 Codex 进程树中，已启用启动器自保护。", "WARN")
        killed, errors = terminate_process_tree(owned_pids, protected_pids=[os.getpid()])
        for error in errors:
            self.log(f"停止 Codex 时部分进程未结束: {error}", "WARN")
        self.supervisor.forget("codex")
        self.log(f"已结束 Codex 相关进程 ({killed} 个)", "INFO")
        return True, f"已终止 {killed} 个进程"

    @staticmethod
    def _wait_for_codex_exit(timeout: float = 4.0) -> bool:
        """Wait for the Store/Electron root to release before relaunching."""
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            if not get_codex_process_info()["running"]:
                return True
            time.sleep(0.1)
        return not get_codex_process_info()["running"]

    def restart_codex(self) -> Tuple[bool, str]:
        return self.launch_codex(force_restart=True)

    def adopt_and_stop(self, app_id: str, root_pid: int) -> Tuple[bool, str]:
        """Adopt one verified application root, then stop only its process tree."""
        adopted, message = self.adopt_session(app_id, root_pid)
        if not adopted:
            return False, message
        action = self.stop_codex if app_id == "codex" else self.stop_antigravity
        return action()

    def adopt_and_restart_codex(self, root_pid: int) -> Tuple[bool, str]:
        """Adopt one verified Codex root before performing a managed restart."""
        return self._adopt_and_restart("codex", root_pid)

    def adopt_and_restart_antigravity(self, root_pid: int) -> Tuple[bool, str]:
        """Adopt one verified Antigravity root before a managed restart."""
        return self._adopt_and_restart("antigravity", root_pid)

    def _adopt_and_restart(self, app_id: str, root_pid: int) -> Tuple[bool, str]:
        adopted, message = self.adopt_session(app_id, root_pid)
        if not adopted:
            return False, message
        restart = self.restart_codex if app_id == "codex" else self.restart_antigravity
        return restart()

    # ==========================================
    # Batch Controls
    # ==========================================
    def launch_all(self) -> Dict[str, Tuple[bool, str]]:
        res = {}
        apps = self.config_mgr.config.get("apps", {})
        if apps.get("antigravity", {}).get("enabled_in_batch", True):
            res["antigravity"] = self.launch_antigravity()
            time.sleep(0.1)
        else:
            res["antigravity"] = (True, "已按组件策略跳过")
        if apps.get("codex", {}).get("enabled_in_batch", True):
            res["codex"] = self.launch_codex()
        else:
            res["codex"] = (True, "已按组件策略跳过")
        return res

    def stop_all(self) -> Dict[str, Tuple[bool, str]]:
        res = {}
        res["antigravity"] = self.stop_antigravity()
        res["codex"] = self.stop_codex()
        return res

    def get_component_snapshot(self, app_id: str) -> Dict[str, Any]:
        owned_pids = self.supervisor.owned_pids(app_id)
        snapshot = collect_component_snapshot(app_id, owned_pids)
        snapshot["session"] = self.supervisor.session(app_id)
        return snapshot

    def adopt_session(self, app_id: str, root_pid: int) -> Tuple[bool, str]:
        result = self.supervisor.adopt(app_id, root_pid, self.config_mgr.get_proxy_url())
        self.log(result[1], "INFO" if result[0] else "WARN")
        return result

    def cleanup_residuals(self, app_id: str) -> Tuple[bool, str]:
        """End only live orphan helpers; never touch an active application tree."""
        if not self.allow_process_control:
            return False, "当前运行环境禁止真实进程控制"
        if app_id == "codex":
            info = get_codex_process_info()
            label = "Codex"
        elif app_id == "antigravity":
            info = get_antigravity_process_info()
            label = "Antigravity"
        else:
            return False, "不支持的应用类型"

        residual_pids = list(info.get("residual_pids", []))
        if not residual_pids:
            if info.get("ignored_pids"):
                message = f"{label} 已退出；已终止的系统记录会由 Windows 自动回收"
            else:
                message = f"未发现 {label} 残留进程"
            self.log(message, "INFO")
            return True, message

        killed, errors = terminate_process_tree(residual_pids, protected_pids=[os.getpid()])
        if errors and killed == 0:
            message = f"清理 {label} 残留失败: {'; '.join(errors[:2])}"
            self.log(message, "WARN")
            return False, message
        message = f"已清理 {label} 残留进程 {killed} 个"
        self.log(message, "INFO")
        return True, message

    def diagnose_codex_proxy(self) -> Dict[str, Any]:
        """Run a bounded three-layer diagnostic without sending credentials."""
        proxy = self.preflight_proxy()
        outbound_ok = False
        outbound_latency = 0.0
        outbound_message = "本地端口不可用"
        if proxy["reachable"]:
            outbound_ok, outbound_latency, outbound_message = probe_https_via_proxy(
                proxy["host"],
                proxy["port"],
                timeout_ms=max(2500, self.config_mgr.config["proxy"].get("timeout_ms", 2000)),
            )
        return {
            "listener": proxy,
            "outbound_ok": outbound_ok,
            "outbound_latency_ms": outbound_latency,
            "outbound_message": outbound_message,
            "process": self.preflight_codex(),
        }
