import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from system.device import DeviceLease


class ReverserRequestError(RuntimeError):
    """A valid request that conflicts with the Reverser's current state."""


@dataclass
class ReverseTask:
    task_id: str
    objective: str
    workspace: Path
    target_id: str = ""
    device_id: str = ""
    binary_path: str = ""
    username: str = ""
    password: str = ""
    enable_password: str = ""
    status: str = "created"
    summary: str = ""
    execution_backend: str = "codex"
    session_id: str | None = None
    session_initialized: bool = False
    legacy_session_id: str | None = None
    device_lease: DeviceLease | None = None
    pending_instruction: str = ""
    cancelled_while_waiting: bool = False
    last_interrupt_order: int | None = None
    session: Any = None
    active_session: Any = None
    run_lock: Any = field(default_factory=threading.RLock, repr=False)
    session_lock: Any = field(default_factory=threading.Lock, repr=False)
    active_session_lock: Any = field(default_factory=threading.Lock, repr=False)
    interrupt_requested: threading.Event = field(
        default_factory=threading.Event,
        repr=False,
    )

    @property
    def work_dir(self) -> Path:
        """Directory writable by the model execution backend."""
        return self.workspace / "work"

    @property
    def device_changes_pending(self) -> bool:
        return (self.work_dir / "device-changes-pending").exists()


@dataclass(frozen=True)
class ReverseTaskResult:
    task_id: str
    status: str
    output: str
