from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol


class ExecutionStatus(StrEnum):
    """Runtime-neutral outcome of one model turn."""

    COMPLETED = "completed"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class ExecutionResult:
    status: ExecutionStatus
    output: str


class ReviewVerdict(StrEnum):
    APPROVE = "approve"
    REVISE = "revise"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class ReviewDecision:
    verdict: ReviewVerdict
    feedback: str = ""


class ExecutionError(RuntimeError):
    """The execution backend failed independently of the task domain."""


class UninitializedSessionError(ExecutionError):
    """A persisted session ID never acquired resumable turn history."""


class ExecutionSession(Protocol):
    """One resumable model session without SDK-specific types."""

    @property
    def session_id(self) -> str:
        ...

    def run(
        self,
        prompt: str,
        output_schema: Mapping[str, Any] | None = None,
        on_started: Callable[[], None] | None = None,
    ) -> ExecutionResult:
        ...

    def query(self, prompt: str) -> str:
        ...

    def interrupt(self) -> None:
        ...

    def close(self) -> None:
        ...


class ExecutionBackend(Protocol):
    """Factory and lifecycle boundary for Reverser model execution."""

    @property
    def name(self) -> str:
        ...

    def open_worker(
        self,
        workspace: Path,
        session_id: str | None,
    ) -> ExecutionSession:
        ...

    def open_reviewer(self, workspace: Path) -> ExecutionSession:
        ...

    def close(self) -> None:
        ...
