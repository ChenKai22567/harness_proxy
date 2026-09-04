from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class ComponentDefinition:
    """Stable UI-facing definition for one process role."""

    key: str
    label: str
    description: str
    control: str  # required | policy | observe
    config_key: Optional[str] = None


@dataclass(frozen=True)
class ProcessRecord:
    """Small immutable process record used by the component view."""

    pid: int
    ppid: int
    name: str
    executable: str
    command_line: Tuple[str, ...]
    role: str
    started_at: float
    memory_mb: float
    owned: bool = False
