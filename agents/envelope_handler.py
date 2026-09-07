from abc import ABC, abstractmethod
from collections.abc import Callable

from system.envelope import (
    Envelope,
    ResultEnvelope,
    SystemEnvelope,
    TaskEnvelope,
)


class EnvelopeHandler(ABC):
    """Adapt routed Envelopes to one main Agent's business methods."""

    def __init__(
        self,
        agent_id: str,
        submit: Callable[[Envelope], None] | None,
    ) -> None:
        if not agent_id:
            raise ValueError("agent_id must not be empty")
        if submit is not None and not callable(submit):
            raise TypeError("submit must be callable")
        self.agent_id = agent_id
        self._submit = submit

    def receive(self, envelope: Envelope) -> None:
        """Authorize and dispatch one Envelope without queuing it."""
        if isinstance(envelope, TaskEnvelope):
            if envelope.sender != "orchestrator":
                raise PermissionError(
                    f"only Orchestrator may assign {self.agent_id} tasks"
                )
            self._receive_task(envelope)
            return
        if isinstance(envelope, SystemEnvelope):
            if envelope.sender not in {"orchestrator", "agent-system"}:
                raise PermissionError(
                    f"unauthorized {self.agent_id} lifecycle command"
                )
            self._receive_system(envelope)
            return
        raise TypeError(f"unsupported Agent Envelope: {type(envelope).__name__}")

    @abstractmethod
    def _receive_task(self, envelope: TaskEnvelope) -> None:
        """Handle one authorized task request."""

    @abstractmethod
    def _receive_system(self, envelope: SystemEnvelope) -> None:
        """Handle one authorized system request."""

    def _emit_status(
        self,
        request: Envelope,
        status: str,
        summary: str,
        artifacts: tuple[str, ...] = (),
        recipient: str | None = None,
    ) -> bool:
        """Submit a correlated ResultEnvelope; return whether it was submitted."""
        envelope = ResultEnvelope(
            sender=self.agent_id,
            recipient=recipient or request.sender,
            task_id=request.task_id,
            correlation_id=request.envelope_id,
            agent_signature=self.agent_id,
            status=status,
            summary=self._summary(summary),
            artifacts=artifacts,
        )
        if self._submit is None:
            return False
        self._submit(envelope)
        return True

    def _emit_error(self, request: Envelope, summary: str) -> bool:
        return self._emit_status(request, "error", summary)

    @staticmethod
    def _summary(content: str, limit: int = 240) -> str:
        summary = " ".join(content.split())
        if len(summary) <= limit:
            return summary
        return summary[: limit - 1].rstrip() + "…"
