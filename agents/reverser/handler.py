from collections.abc import Callable
from typing import TYPE_CHECKING

from agents.envelope_handler import EnvelopeHandler
from system.envelope import (
    Envelope,
    SystemAction,
    SystemEnvelope,
    TaskEnvelope,
)

from .models import ReverserRequestError

if TYPE_CHECKING:
    from .agent import Reverser
    from .models import ReverseTaskResult


_REQUEST_ERRORS = (LookupError, ReverserRequestError, ValueError)


class ReverserEnvelopeHandler(EnvelopeHandler):
    """Translate routed Envelopes into Reverser lifecycle calls."""

    def __init__(
        self,
        reverser: "Reverser",
        submit: Callable[[Envelope], None] | None,
    ) -> None:
        self._reverser = reverser
        super().__init__(reverser.agent_id, submit)

    def _receive_task(self, envelope: TaskEnvelope) -> None:
        if not envelope.task_id:
            self._emit_error(envelope, "TaskEnvelope requires task_id")
            return
        try:
            result = self._reverser.start_task(
                envelope.task_id,
                envelope.content,
                envelope.information,
            )
        except _REQUEST_ERRORS as exc:
            self._emit_error(envelope, str(exc))
            return
        self._emit_result(envelope, result)

    def _receive_system(self, envelope: SystemEnvelope) -> None:
        try:
            if envelope.action == SystemAction.QUERY_TASK:
                if not envelope.task_id:
                    raise ValueError("query_task requires task_id")
                try:
                    summary = self._reverser.query_task(envelope.task_id)
                except LookupError:
                    if envelope.arguments.get("accepted_task") is not True:
                        raise
                    self._emit_status(
                        envelope,
                        "starting",
                        "Task accepted and waiting to enter the Reverser.",
                    )
                    return
                self._emit_status(envelope, "status", summary)
            elif envelope.action == SystemAction.INTERRUPT_TASK:
                if not envelope.task_id:
                    raise ValueError("interrupt_task requires task_id")
                cancelled = envelope.arguments.get("cancelled_task_ids", [])
                if envelope.task_id in cancelled:
                    self._emit_status(
                        envelope,
                        "interrupt_processed",
                        "Task was cancelled before execution.",
                    )
                    return
                summary = self._reverser.interrupt_task(
                    envelope.task_id,
                    self._control_order(envelope),
                )
                self._emit_status(envelope, "interrupt_processed", summary)
            elif envelope.action == SystemAction.INTERRUPT_AGENT:
                summary = self._reverser.interrupt_all(
                    str(envelope.details.get("reason", envelope.event))
                )
                cancelled = envelope.arguments.get("cancelled_task_ids", [])
                if cancelled:
                    summary += "\nCancelled before execution: " + ", ".join(cancelled)
                self._emit_status(envelope, "interrupt_processed", summary)
            elif envelope.action == SystemAction.RESUME_TASK:
                if not envelope.task_id:
                    raise ValueError("resume_task requires task_id")
                notes = str(envelope.arguments.get("notes", "")).strip()
                result = self._reverser.resume_task(
                    envelope.task_id,
                    notes,
                    self._control_order(envelope),
                )
                self._emit_result(envelope, result)
            elif envelope.action == SystemAction.DEVICE_AVAILABLE:
                if not envelope.task_id:
                    raise ValueError("device_available requires task_id")
                result = self._reverser.device_available(envelope.task_id)
                self._emit_result(envelope, result, recipient="orchestrator")
            elif envelope.action in {
                SystemAction.STOP_AGENT,
                SystemAction.STOP_SYSTEM,
            }:
                self._reverser.stop()
                self._emit_status(envelope, "stopped", envelope.event)
            else:
                raise ValueError(f"unsupported Reverser action: {envelope.action}")
        except _REQUEST_ERRORS as exc:
            self._emit_error(envelope, str(exc))

    @staticmethod
    def _control_order(envelope: SystemEnvelope) -> int | None:
        order = envelope.arguments.get("runtime_control_order")
        if isinstance(order, int) and not isinstance(order, bool):
            return order
        return None

    def _emit_result(
        self,
        request: Envelope,
        result: "ReverseTaskResult",
        recipient: str | None = None,
    ) -> None:
        artifacts = ()
        if result.status == "ready_to_deliver":
            artifacts = (f"{result.task_id}/analysis/behavior-model.md",)
        submitted = self._emit_status(
            request,
            result.status,
            result.output,
            artifacts,
            recipient,
        )
        if result.status == "ready_to_deliver" and submitted:
            self._reverser.mark_delivered(result.task_id)
