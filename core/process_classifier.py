from collections import defaultdict, deque
from typing import Any, Dict, Iterable, List, Sequence, Set

from core.component_registry import get_component_definitions
from core.process_model import ProcessRecord
from core.process_detector import get_process_snapshot_entries


def classify_process(app_id: str, name: str, command_line: Sequence[str]) -> str:
    """Translate volatile Electron process arguments into stable user roles."""

    exe_name = (name or "").lower()
    args = [str(value).lower() for value in (command_line or ())]
    joined = " ".join(args)

    if exe_name == "codex.exe" and "app-server" in args:
        return "app_server"
    if "--type=renderer" in joined:
        return "renderer"
    if "--type=gpu-process" in joined:
        return "gpu"
    if "crashpad" in joined or "crashpad" in exe_name:
        return "crash_handler"
    if "network.mojom.networkservice" in joined:
        return "network"
    if "storage.mojom.storageservice" in joined:
        return "storage"
    if "--type=utility" in joined or exe_name in {"node.exe", "npx.exe"}:
        return "helper"

    root_names = {
        "antigravity": {"antigravity.exe"},
        "codex": {"chatgpt.exe"},
    }
    if args and exe_name in root_names.get(app_id, set()) and "--type=" not in joined:
        return "core"
    if exe_name == "codex.exe":
        return "helper"
    return "unknown"


def _selected_process_ids(app_id: str, basics: Dict[int, Dict[str, Any]], psutil) -> Set[int]:
    if app_id == "codex":
        # A standalone Codex CLI also uses codex.exe. Only include codex.exe
        # when it descends from the desktop ChatGPT shell, otherwise the
        # component page could claim ownership of an unrelated terminal job.
        roots = set()
        residual_seeds = set()
        for pid, item in basics.items():
            if item["name"] not in {"chatgpt.exe", "codex.exe"}:
                continue
            command_line = _safe_cmdline_for_pid(psutil, pid)
            if item["threads"] <= 0:
                continue
            try:
                if not psutil.Process(pid).is_running():
                    continue
            except Exception:
                continue
            role = classify_process(app_id, item["name"], command_line)
            if role == "core":
                roots.add(pid)
            elif item["name"] == "chatgpt.exe":
                residual_seeds.add(pid)
            elif role == "app_server" and any(
                "mcp_servers.codex_app=" in str(arg).lower()
                or "codex_app_tools" in str(arg).lower()
                for arg in command_line
            ):
                residual_seeds.add(pid)
    else:
        roots = set()
        residual_seeds = set()
        for pid, item in basics.items():
            if item["name"] != "antigravity.exe":
                continue
            command_line = _safe_cmdline_for_pid(psutil, pid)
            if item["threads"] <= 0:
                continue
            try:
                if not psutil.Process(pid).is_running():
                    continue
            except Exception:
                continue
            if classify_process(app_id, item["name"], command_line) == "core":
                roots.add(pid)
            else:
                residual_seeds.add(pid)

    children: Dict[int, List[int]] = defaultdict(list)
    for pid, item in basics.items():
        children[item["ppid"]].append(pid)

    selected = set(roots)
    pending = deque(roots)
    while pending:
        parent = pending.popleft()
        for child in children.get(parent, ()):
            if child not in selected:
                selected.add(child)
                pending.append(child)
    # Include living desktop helpers left behind after their root exited, but
    # keep stopped process objects and unrelated Codex CLI jobs invisible.
    for seed in residual_seeds - selected:
        selected.add(seed)
        pending.append(seed)
    while pending:
        parent = pending.popleft()
        for child in children.get(parent, ()):
            if child not in selected:
                selected.add(child)
                pending.append(child)
    return selected


def _safe_cmdline_for_pid(psutil, pid: int) -> Sequence[str]:
    try:
        return psutil.Process(pid).cmdline()
    except Exception:
        return ()


def collect_component_snapshot(app_id: str, owned_pids: Iterable[int] = ()) -> Dict[str, Any]:
    """Collect process roles only when the component window is visible.

    A lightweight first pass builds ancestry without reading every process
    command line. Expensive command line and memory queries are limited to the
    selected application tree.
    """

    import psutil

    # Toolhelp provides PID/parent/name in a single native snapshot. Using
    # psutil.process_iter(attrs=...) opens hundreds of processes on Windows and
    # took ~20 seconds on a busy developer machine.
    basics: Dict[int, Dict[str, Any]] = {
        int(pid): {
            "ppid": int(ppid),
            "name": str(name or "").lower(),
            "threads": int(thread_count),
        }
        for pid, ppid, name, thread_count in get_process_snapshot_entries()
    }

    selected = _selected_process_ids(app_id, basics, psutil)
    owned = {int(pid) for pid in owned_pids}
    records: List[ProcessRecord] = []

    for pid in sorted(selected):
        item = basics.get(pid)
        if not item:
            continue
        try:
            process = psutil.Process(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        # Thread count came from the same native snapshot as PID/parent/name.
        # A zero-thread Store shell is an exited artifact, while is_running()
        # cheaply catches a process that disappeared after that snapshot.
        if item["threads"] <= 0 or not process.is_running():
            continue
        try:
            command_line = tuple(process.cmdline())
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            command_line = ()
        try:
            executable = process.exe()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            executable = ""
        try:
            memory_mb = round(process.memory_info().rss / (1024 * 1024), 1)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            memory_mb = 0.0
        try:
            started_at = float(process.create_time())
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            started_at = 0.0

        records.append(ProcessRecord(
            pid=pid,
            ppid=item["ppid"],
            name=item["name"],
            executable=executable,
            command_line=command_line,
            role=classify_process(app_id, item["name"], command_line),
            started_at=started_at,
            memory_mb=memory_mb,
            owned=pid in owned,
        ))

    definitions = get_component_definitions(app_id)
    components = []
    for definition in definitions:
        members = [record for record in records if record.role == definition.key]
        components.append({
            "key": definition.key,
            "label": definition.label,
            "description": definition.description,
            "control": definition.control,
            "config_key": definition.config_key,
            "running": bool(members),
            "count": len(members),
            "pids": [record.pid for record in members],
            "owned_count": sum(1 for record in members if record.owned),
            "memory_mb": round(sum(record.memory_mb for record in members), 1),
        })

    roots = [record for record in records if record.role == "core"]
    active_ids = set(record.pid for record in roots)
    pending = deque(active_ids)
    children: Dict[int, List[int]] = defaultdict(list)
    for record in records:
        children[record.ppid].append(record.pid)
    while pending:
        for child in children.get(pending.popleft(), ()):
            if child not in active_ids:
                active_ids.add(child)
                pending.append(child)
    residual_ids = [record.pid for record in records if record.pid not in active_ids]
    residual_set = set(residual_ids)
    for component in components:
        component_pids = set(component["pids"])
        component["active_count"] = len(component_pids & active_ids)
        component["residual_count"] = len(component_pids & residual_set)
        component["present"] = bool(component_pids)
        component["running"] = component["active_count"] > 0
    return {
        "app_id": app_id,
        "running": bool(roots),
        "residual_running": bool(residual_ids),
        "residual_pids": residual_ids,
        "process_count": len(records),
        "memory_mb": round(sum(record.memory_mb for record in records), 1),
        "owned_count": sum(1 for record in records if record.owned),
        "external_count": sum(1 for record in records if not record.owned),
        "root_pids": [record.pid for record in roots],
        "managed_root_pids": [record.pid for record in roots if record.owned],
        "external_root_pids": [record.pid for record in roots if not record.owned],
        "roots": [
            {
                "pid": record.pid,
                "started_at": record.started_at,
                "owned": record.owned,
                "executable": record.executable,
            }
            for record in roots
        ],
        "components": components,
    }
