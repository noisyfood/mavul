import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class SystemAction(StrEnum):
    REPORT_FAILURE = "report_failure"
    QUERY_TASK = "query_task"
    INTERRUPT_TASK = "interrupt_task"
    INTERRUPT_AGENT = "interrupt_agent"
    RESUME_TASK = "resume_task"
    STOP_AGENT = "stop_agent"
    STOP_SYSTEM = "stop_system"
    DEVICE_AVAILABLE = "device_available"
    DEVICE_PREEMPTED = "device_preempted"
    DEVICE_QUARANTINED = "device_quarantined"


@dataclass(frozen=True, kw_only=True)
class Envelope:
    """Common routing header for all in-process AgentSystem messages."""

    sender: str
    recipient: str
    task_id: str | None = None
    correlation_id: uuid.UUID | None = None
    envelope_id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    envelope_type: str = field(init=False, default="envelope")

    def __post_init__(self) -> None:
        if not self.sender or not self.recipient:
            raise ValueError("Envelope sender and recipient must not be empty")
        if self.created_at.tzinfo is None:
            raise ValueError("Envelope created_at must be timezone-aware")


@dataclass(frozen=True, kw_only=True)
class TaskEnvelope(Envelope):
    content: str
    information: dict[str, Any] = field(default_factory=dict)
    envelope_type: str = field(init=False, default="task")

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.content.strip():
            raise ValueError("TaskEnvelope content must not be empty")


@dataclass(frozen=True, kw_only=True)
class ResultEnvelope(Envelope):
    agent_signature: str
    status: str
    summary: str
    artifacts: tuple[str, ...] = ()
    envelope_type: str = field(init=False, default="result")

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.agent_signature or not self.status:
            raise ValueError("ResultEnvelope signature and status are required")


@dataclass(frozen=True, kw_only=True)
class SystemEnvelope(Envelope):
    event: str
    action: SystemAction
    details: dict[str, Any] = field(default_factory=dict)
    arguments: dict[str, Any] = field(default_factory=dict)
    envelope_type: str = field(init=False, default="system")

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.event:
            raise ValueError("SystemEnvelope event must not be empty")
