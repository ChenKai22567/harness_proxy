import json
import os
import tempfile
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from core.process_classifier import classify_process


class SessionSupervisor:
    """Persist and validate launcher-owned application roots.

    Process names alone are never an ownership boundary. A stored PID is only
    trusted when its creation time, executable and root role still match.
    """

    def __init__(self, state_file: str, enabled: bool = True):
        self.state_file = state_file
        self.enabled = enabled
        self._lock = threading.RLock()
        self._state: Dict[str, Any] = {"version": 1, "sessions": {}}
        if self.enabled:
            self._load()

    def _load(self):
        if not os.path.isfile(self.state_file):
            return
        try:
            with open(self.state_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict) and isinstance(data.get("sessions"), dict):
                self._state = data
        except Exception:
            self._state = {"version": 1, "sessions": {}}

    def _save_locked(self):
        directory = os.path.dirname(os.path.abspath(self.state_file))
        os.makedirs(directory, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix=".managed-sessions-", suffix=".json", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._state, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.state_file)
        finally:
            if os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    @staticmethod
    def _root_matches(app_id: str, process) -> bool:
        try:
            name = process.name().lower()
            command_line = process.cmdline()
        except Exception:
            return False
        return classify_process(app_id, name, command_line) == "core"

    def register_launch(self, app_id: str, pid: int, executable: str, proxy_url: str) -> bool:
        if not self.enabled:
            return False
        import psutil
        try:
            process = psutil.Process(int(pid))
            if not self._root_matches(app_id, process):
                return False
            created_at = process.create_time()
            actual_executable = process.exe()
        except Exception:
            return False

        with self._lock:
            self._state["sessions"][app_id] = {
                "session_id": uuid.uuid4().hex,
                "root_pid": int(pid),
                "root_created_at": float(created_at),
                "executable": os.path.abspath(actual_executable or executable),
                "launched_at": time.time(),
                "proxy_url": proxy_url,
            }
            self._save_locked()
        return True

    def adopt(self, app_id: str, root_pid: int, proxy_url: str = "") -> Tuple[bool, str]:
        if not self.enabled:
            return False, "当前运行环境禁止进程控制"
        import psutil
        try:
            process = psutil.Process(int(root_pid))
            if not self._root_matches(app_id, process):
                return False, "所选 PID 不是可识别的应用根进程"
            executable = process.exe()
            created_at = process.create_time()
        except Exception as exc:
            return False, f"无法读取所选根进程: {exc}"

        with self._lock:
            self._state["sessions"][app_id] = {
                "session_id": uuid.uuid4().hex,
                "root_pid": int(root_pid),
                "root_created_at": float(created_at),
                "executable": os.path.abspath(executable),
                "launched_at": time.time(),
                "proxy_url": proxy_url,
                "adopted": True,
            }
            self._save_locked()
        return True, f"已接管根进程 PID {root_pid}"

    def _validated_root_locked(self, app_id: str):
        import psutil
        session = self._state.get("sessions", {}).get(app_id)
        if not session:
            return None
        try:
            process = psutil.Process(int(session["root_pid"]))
            if abs(process.create_time() - float(session["root_created_at"])) > 1.0:
                raise psutil.NoSuchProcess(process.pid)
            if not self._root_matches(app_id, process):
                raise psutil.NoSuchProcess(process.pid)
            stored_exe = os.path.normcase(os.path.abspath(session.get("executable", "")))
            actual_exe = os.path.normcase(os.path.abspath(process.exe()))
            if stored_exe and stored_exe != actual_exe:
                raise psutil.NoSuchProcess(process.pid)
            return process
        except Exception:
            self._state.get("sessions", {}).pop(app_id, None)
            try:
                self._save_locked()
            except Exception:
                pass
            return None

    def owned_pids(self, app_id: str) -> List[int]:
        if not self.enabled:
            return []
        with self._lock:
            root = self._validated_root_locked(app_id)
            if root is None:
                return []
            processes = {root.pid: root}
            try:
                for child in root.children(recursive=True):
                    processes[child.pid] = child
            except Exception:
                pass
            return sorted(processes)

    def session(self, app_id: str) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        with self._lock:
            if self._validated_root_locked(app_id) is None:
                return None
            return dict(self._state["sessions"][app_id])

    def forget(self, app_id: str):
        if not self.enabled:
            return
        with self._lock:
            if self._state.get("sessions", {}).pop(app_id, None) is not None:
                self._save_locked()
