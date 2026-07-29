import json
from collections.abc import Callable
from typing import TYPE_CHECKING

from system.envelope import (
    Envelope,
    ResultEnvelope,
    SystemAction,
    SystemEnvelope,
    TaskEnvelope,
)

if TYPE_CHECKING:
    from agents.reverser import Reverser
    from agents.reverser_types import ReverseTaskResult


class ReverserMailbox:
    """Translate routed Envelopes into Reverser lifecycle calls."""

    def __init__(
        self,
        reverser: "Reverser",
        submit: Callable[[Envelope], None] | None,
    ) -> None:
        self._reverser = reverser
        self._submit = submit

    def receive(self, envelope: Envelope) -> None:
        if isinstance(envelope, TaskEnvelope):
            if envelope.sender != "orchestrator":
                raise PermissionError("only Orchestrator may assign Reverser tasks")
            self._receive_task(envelope)
            return
        if isinstance(envelope, SystemEnvelope):
            if envelope.sender not in {"orchestrator", "agent-system"}:
                raise PermissionError("unauthorized Reverser lifecycle command")
            self._receive_system(envelope)
            return
        raise TypeError(f"unsupported Reverser Envelope: {type(envelope).__name__}")

    def _receive_task(self, envelope: TaskEnvelope) -> None:
        if not envelope.task_id:
            self._emit_error(envelope, "TaskEnvelope requires task_id")
            return
        instruction = envelope.content
        if envelope.information:
            instruction += (
                "\n\n## Necessary Information\n\n```json\n"
                + json.dumps(envelope.information, ensure_ascii=False, indent=2)
                + "\n```"
            )
        try:
            result = self._reverser.start_task(envelope.task_id, instruction)
        except Exception as exc:
            self._emit_error(envelope, str(exc))
            return
        self._emit_result(envelope, result)

    def _receive_system(self, envelope: SystemEnvelope) -> None:
        try:
            if envelope.action == SystemAction.QUERY_TASK:
                if not envelope.task_id:
                    raise ValueError("query_task requires task_id")
                summary = self._reverser.query_task(envelope.task_id)
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
                summary = self._reverser.interrupt_task(envelope.task_id)
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
                result = self._reverser.resume_task(envelope.task_id, notes)
                self._emit_result(envelope, result)
            elif envelope.action in {
                SystemAction.STOP_AGENT,
                SystemAction.STOP_SYSTEM,
            }:
                self._reverser.stop()
                self._emit_status(envelope, "stopped", envelope.event)
            else:
                raise ValueError(f"unsupported Reverser action: {envelope.action}")
        except Exception as exc:
            self._emit_error(envelope, str(exc))

    def _emit_result(
        self,
        request: Envelope,
        result: "ReverseTaskResult",
    ) -> None:
        artifacts = ()
        if result.status == "ready_to_deliver":
            artifacts = (f"{result.task_id}/analysis/behavior-model.md",)
        self._emit(
            ResultEnvelope(
                sender="reverser",
                recipient=request.sender,
                task_id=result.task_id,
                correlation_id=request.envelope_id,
                agent_signature="reverser",
                status=result.status,
                summary=self._summary(result.output),
                artifacts=artifacts,
            )
        )
        if result.status == "ready_to_deliver" and self._submit is not None:
            self._reverser.mark_delivered(result.task_id)

    def _emit_status(
        self,
        request: Envelope,
        status: str,
        summary: str,
    ) -> None:
        self._emit(
            ResultEnvelope(
                sender="reverser",
                recipient=request.sender,
                task_id=request.task_id,
                correlation_id=request.envelope_id,
                agent_signature="reverser",
                status=status,
                summary=self._summary(summary),
            )
        )

    def _emit_error(self, request: Envelope, summary: str) -> None:
        self._emit_status(request, "error", summary)

    def _emit(self, envelope: ResultEnvelope) -> None:
        if self._submit is not None:
            self._submit(envelope)

    @staticmethod
    def _summary(content: str, limit: int = 240) -> str:
        summary = " ".join(content.split())
        if len(summary) <= limit:
            return summary
        return summary[: limit - 1].rstrip() + "…"
