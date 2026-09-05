import os
import sys
import json
import tempfile
import threading
from typing import Dict, Any

# Single fallback for the proxy probe timeout, shared by the engine, the
# monitor loop and _normalize so the pre-2000 defaults can never diverge.
DEFAULT_PROXY_TIMEOUT_MS = 2000

def get_app_root_dir() -> str:
    """Return the writable application directory.

    In a PyInstaller build this is intentionally the directory beside the
    executable, because configuration and logs must remain writable.
    """
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_resource_root_dir() -> str:
    """Return the read-only root that contains bundled application assets."""
    if getattr(sys, "frozen", False):
        bundle_root = getattr(sys, "_MEIPASS", "")
        if bundle_root and os.path.isdir(bundle_root):
            return os.path.abspath(bundle_root)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_resource_path(*parts: str) -> str:
    """Resolve an asset in both source and PyInstaller one-folder builds."""
    return os.path.join(get_resource_root_dir(), *parts)

DEFAULT_CONFIG: Dict[str, Any] = {
    "proxy": {
        "host": "127.0.0.1",
        "port": 7890,
        "preset": "Clash / Mihomo (7890)",
        "bypass": "localhost,127.0.0.1,::1",
        "timeout_ms": DEFAULT_PROXY_TIMEOUT_MS
    },
    "presets": [
        {"name": "Clash / Mihomo (7890)", "host": "127.0.0.1", "port": 7890},
        {"name": "Clash Verge (7897)", "host": "127.0.0.1", "port": 7897},
        {"name": "v2rayN / Xray (10808)", "host": "127.0.0.1", "port": 10808},
        {"name": "Sing-box (2080)", "host": "127.0.0.1", "port": 2080},
        {"name": "自定义端口", "host": "127.0.0.1", "port": 7890}
    ],
    "apps": {
        "antigravity": {
            "auto_detect": True,
            "custom_path": "",
            "enable_browser_shim": True,
            "custom_node_path": "",
            "enabled_in_batch": True,
            "components": {
                "gpu_acceleration": True
            }
        },
        "codex": {
            "auto_detect": True,
            "custom_path": "",
            "detect_ms_store": True,
            "enabled_in_batch": True,
            "components": {
                "gpu_acceleration": True,
                "websocket_proxy": True
            }
        }
    },
    "launcher": {
        "minimize_to_tray_on_close": True,
        "check_clash_before_launch": True,
        "start_minimized": False
    }
}

class ConfigManager:
    def __init__(self, config_file: str = None):
        if config_file is None:
            self.config_file = os.path.join(get_app_root_dir(), "config.json")
        else:
            self.config_file = config_file
        self._lock = threading.RLock()
        self.config: Dict[str, Any] = {}
        self.load()

    def load(self) -> Dict[str, Any]:
        """Loads configuration from JSON file with recursive default fallbacks."""
        with self._lock:
            if os.path.exists(self.config_file):
                try:
                    with open(self.config_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    self.config = self._normalize(self._deep_merge(DEFAULT_CONFIG, data))
                except Exception as e:
                    print(f"[ConfigManager] Error reading config file, using defaults: {e}")
                    self.config = json.loads(json.dumps(DEFAULT_CONFIG))
            else:
                self.config = json.loads(json.dumps(DEFAULT_CONFIG))
                self.save()
            return self.config

    def save(self) -> bool:
        """Saves current configuration to file."""
        with self._lock:
            temp_path = ""
            try:
                directory = os.path.dirname(os.path.abspath(self.config_file))
                os.makedirs(directory, exist_ok=True)
                self.config = self._normalize(self.config)
                fd, temp_path = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=directory)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self.config, f, indent=2, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp_path, self.config_file)
                return True
            except Exception as e:
                print(f"[ConfigManager] Failed to save config: {e}")
                return False
            finally:
                if temp_path and os.path.exists(temp_path):
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass

    def reset_to_defaults(self):
        with self._lock:
            self.config = json.loads(json.dumps(DEFAULT_CONFIG))
            self.save()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self.config))

    def get_proxy_url(self) -> str:
        host = self.config["proxy"]["host"]
        port = self.config["proxy"]["port"]
        return f"http://{host}:{port}"

    def get_proxy_bypass_for_env(self) -> str:
        return self.config["proxy"].get("bypass", "localhost,127.0.0.1,::1")

    def get_proxy_bypass_for_chromium(self) -> str:
        # Chromium expects semicolons
        bypass = self.get_proxy_bypass_for_env()
        items = [x.strip() for x in bypass.replace(";", ",").split(",") if x.strip()]
        fixed = []
        for item in items:
            if item == "::1":
                fixed.append("[::1]")
            else:
                fixed.append(item)
        return ";".join(fixed)

    def _deep_merge(self, default_dict: dict, user_dict: dict) -> dict:
        result = json.loads(json.dumps(default_dict))
        for key, value in user_dict.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = value
        return result

    def _normalize(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Repair harmless stale values while retaining every unknown key."""
        proxy = config.setdefault("proxy", {})
        proxy["host"] = str(proxy.get("host") or "127.0.0.1").strip()
        try:
            proxy["port"] = max(1, min(65535, int(proxy.get("port", 7890))))
        except (TypeError, ValueError):
            proxy["port"] = 7890
        try:
            proxy["timeout_ms"] = max(
                200, min(15000, int(proxy.get("timeout_ms", DEFAULT_PROXY_TIMEOUT_MS)))
            )
        except (TypeError, ValueError):
            proxy["timeout_ms"] = DEFAULT_PROXY_TIMEOUT_MS

        presets = config.get("presets")
        if not isinstance(presets, list) or not presets:
            presets = json.loads(json.dumps(DEFAULT_CONFIG["presets"]))
            config["presets"] = presets
        names = [str(item.get("name", "")) for item in presets if isinstance(item, dict)]
        if proxy.get("preset") not in names:
            def _preset_port(item: Dict[str, Any]) -> int:
                try:
                    return int(item.get("port", -1))
                except (TypeError, ValueError):
                    return -1

            match = next((
                item.get("name") for item in presets
                if isinstance(item, dict)
                and str(item.get("host", "")) == proxy["host"]
                and _preset_port(item) == proxy["port"]
            ), None)
            proxy["preset"] = match or ("自定义端口" if "自定义端口" in names else names[0])

        for app_id in ("antigravity", "codex"):
            app = config.setdefault("apps", {}).setdefault(app_id, {})
            app["enabled_in_batch"] = bool(app.get("enabled_in_batch", True))
            components = app.setdefault("components", {})
            components["gpu_acceleration"] = bool(components.get("gpu_acceleration", True))
        config["apps"]["antigravity"]["enable_browser_shim"] = bool(
            config["apps"]["antigravity"].get("enable_browser_shim", True)
        )
        # Codex routes secure WebSocket over the standard HTTPS proxy.  This
        # capability is deliberately locked on (the UI disables the switch),
        # so a hand-edited "false" is corrected instead of honoured.
        config["apps"]["codex"]["components"]["websocket_proxy"] = True
        return config
