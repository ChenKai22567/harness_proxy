"""Keep the native Codex client on the launcher's explicit proxy route."""
from datetime import datetime
from pathlib import Path
import re
import shutil
import subprocess
from typing import Dict, Tuple


SYSTEM_PROXY_FEATURES = ("respect_system_proxy", "system_proxy_fallback")


def _read_proxy_features(cli: Path, environment: Dict[str, str]) -> Dict[str, bool]:
    output = _run_cli(cli, ["features", "list"], environment)
    states = {}
    for line in output.splitlines():
        match = re.fullmatch(r"(\S+)\s+.+?\s+(true|false)\s*", line)
        if match and match[1] in SYSTEM_PROXY_FEATURES:
            states[match[1]] = match[2] == "true"
    return states


def _run_cli(cli: Path, arguments, environment: Dict[str, str]) -> str:
    try:
        result = subprocess.run(
            [str(cli), *arguments], env=environment,
            cwd=str(cli.parent), capture_output=True,
            encoding="utf-8", errors="replace", timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise OSError("检查 Codex 原生代理配置超时，已取消启动") from exc
    if result.returncode:
        # CLI diagnostics can include private configuration; never echo them.
        raise OSError(f"Codex 官方代理配置命令失败 (退出码 {result.returncode})")
    return result.stdout


def prepare_codex_proxy_settings(
    executable: str, environment: Dict[str, str],
) -> Tuple[Tuple[str, ...], str]:
    """Back up and disable conflicting native system-proxy features.

    Newer clients may select Windows' DIRECT route ahead of HTTPS_PROXY, or
    retry a slow bootstrap GET through that route after five seconds. Use
    the bundled CLI's config editor so comments and unrelated options survive.
    Windows proxy settings and sandbox permissions are never edited here.
    Older/unbundled clients without the CLI keep their existing behavior.
    """
    cli = Path(executable).parent / "resources" / "codex.exe"
    if not cli.is_file():
        return (), ""
    states = _read_proxy_features(cli, environment)
    changes = tuple(key for key in SYSTEM_PROXY_FEATURES if states.get(key))
    if not changes:
        return (), ""

    home = Path(environment.get("CODEX_HOME") or Path.home() / ".codex")
    config = home / "config.toml"
    backup = ""
    if config.is_file():
        directory = home / "harness-proxy-backups"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / ("config-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".toml.bak")
        shutil.copy2(config, target)
        backup = str(target)

    for key in changes:
        _run_cli(cli, ["features", "disable", key], environment)
    effective = _read_proxy_features(cli, environment)
    if any(effective.get(key) is not False for key in changes):
        raise OSError("Codex 配置仍要求使用系统代理，请检查原生客户端的配置策略")
    return changes, backup
