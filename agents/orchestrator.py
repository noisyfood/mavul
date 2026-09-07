import json
import threading
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from system.envelope import (
    Envelope,
    ResultEnvelope,
    SystemAction,
    SystemEnvelope,
    TaskEnvelope,
)


OutputSink = Callable[[str], None]


def _print_line(line: str) -> None:
    print(line, flush=True)


class Orchestrator:
    """Route newline-delimited JSON requests to the current Reverser."""

    agent_id = "orchestrator"

    def __init__(
        self,
        submit: Callable[[Envelope], None],
        stop_system: Callable[[], object],
        output: OutputSink = _print_line,
    ) -> None:
        if not callable(submit) or not callable(stop_system):
            raise TypeError("submit and stop_system must be callable")
        if not callable(output):
            raise TypeError("output must be callable")
        self._submit = submit
        self._stop_system = stop_system
        self._output = output
        self._output_lock = threading.Lock()
        self._task_lock = threading.Lock()
        self._accepted_task_ids: set[str] = set()

    def receive(self, envelope: Envelope) -> None:
        if isinstance(envelope, TaskEnvelope):
            self._receive_request(envelope.content)
            return
        if isinstance(envelope, ResultEnvelope):
            self._emit_result(envelope)
            return
        if isinstance(envelope, SystemEnvelope):
            self._receive_system(envelope)
            return
        raise TypeError(
            f"unsupported Orchestrator Envelope: {type(envelope).__name__}"
        )

    def _receive_request(self, content: str) -> None:
        try:
            request = json.loads(content)
        except json.JSONDecodeError as exc:
            self._emit({"type": "error", "message": f"invalid JSON: {exc.msg}"})
            return
        if not isinstance(request, dict):
            self._emit({"type": "error", "message": "request must be a JSON object"})
            return

        action = request.get("action")
        try:
            if action == "reverse":
                self._reverse(request)
            elif action == "query":
                self._control(request, SystemAction.QUERY_TASK)
            elif action == "interrupt":
                self._control(request, SystemAction.INTERRUPT_TASK)
            elif action == "resume":
                self._resume(request)
            elif action == "stop":
                self._emit({"type": "stopped"})
                self._stop_system()
            else:
                raise ValueError(f"unsupported action: {action!r}")
        except ValueError as exc:
            error: dict[str, Any] = {"type": "error", "message": str(exc)}
            if isinstance(request.get("task_id"), str):
                error["task_id"] = request["task_id"]
            self._emit(error)

    def _reverse(self, request: dict[str, Any]) -> None:
        fields = (
            "target_id",
            "device_id",
            "binary_path",
            "objective",
            "username",
            "password",
            "enable_password",
        )
        information = {
            field: self._required_text(request, field) for field in fields
        }
        task_id = str(uuid.uuid4())
        with self._task_lock:
            self._accepted_task_ids.add(task_id)
        self._emit({"type": "accepted", "task_id": task_id})
        submitted = self._submit_request(
            TaskEnvelope(
                sender=self.agent_id,
                recipient="reverser",
                task_id=task_id,
                content=information["objective"],
                information=information,
            ),
            task_id,
        )
        if not submitted:
            with self._task_lock:
                self._accepted_task_ids.discard(task_id)

    def _control(
        self,
        request: dict[str, Any],
        action: SystemAction,
    ) -> None:
        task_id = self._required_text(request, "task_id")
        arguments = {}
        if action == SystemAction.QUERY_TASK:
            with self._task_lock:
                if task_id in self._accepted_task_ids:
                    arguments["accepted_task"] = True
        self._submit_request(
            SystemEnvelope(
                sender=self.agent_id,
                recipient="reverser",
                task_id=task_id,
                event=action.value,
                action=action,
                arguments=arguments,
            ),
            task_id,
        )

    def _resume(self, request: dict[str, Any]) -> None:
        task_id = self._required_text(request, "task_id")
        notes = self._required_text(request, "notes")
        self._submit_request(
            SystemEnvelope(
                sender=self.agent_id,
                recipient="reverser",
                task_id=task_id,
                event=SystemAction.RESUME_TASK.value,
                action=SystemAction.RESUME_TASK,
                arguments={"notes": notes},
            ),
            task_id,
        )

    def _submit_request(self, envelope: Envelope, task_id: str) -> bool:
        try:
            self._submit(envelope)
        except (LookupError, RuntimeError, TypeError, ValueError) as exc:
            self._emit(
                {
                    "type": "result",
                    "task_id": task_id,
                    "status": "error",
                    "summary": str(exc),
                    "artifacts": [],
                }
            )
            return False
        return True

    def _emit_result(self, envelope: ResultEnvelope) -> None:
        if envelope.status not in {"starting", "status", "interrupt_processed"}:
            with self._task_lock:
                if envelope.task_id is not None:
                    self._accepted_task_ids.discard(envelope.task_id)
        self._emit(
            {
                "type": "result",
                "task_id": envelope.task_id,
                "status": envelope.status,
                "summary": envelope.summary,
                "artifacts": list(envelope.artifacts),
            }
        )

    def _receive_system(self, envelope: SystemEnvelope) -> None:
        if envelope.action != SystemAction.REPORT_FAILURE:
            self._emit(
                {
                    "type": "error",
                    "task_id": envelope.task_id,
                    "message": f"unsupported system action: {envelope.action}",
                }
            )
            return
        summary = str(envelope.details.get("error", envelope.event))
        with self._task_lock:
            if envelope.task_id is not None:
                self._accepted_task_ids.discard(envelope.task_id)
        self._emit(
            {
                "type": "result",
                "task_id": envelope.task_id,
                "status": "error",
                "summary": summary,
                "artifacts": [],
            }
        )

    def _emit(self, message: Mapping[str, Any]) -> None:
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._output_lock:
            self._output(line)

    @staticmethod
    def _required_text(request: Mapping[str, Any], field: str) -> str:
        value = request.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a non-empty string")
        return value


def register(
    agent_system: Any,
    _config: Mapping[str, Any],
    output: OutputSink = _print_line,
) -> Orchestrator:
    """Create and register the deterministic Orchestrator."""
    orchestrator = Orchestrator(
        agent_system.submit,
        agent_system.stop,
        output,
    )
    agent_system.register_agent(
        orchestrator.agent_id,
        orchestrator.receive,
        (TaskEnvelope, ResultEnvelope, SystemEnvelope),
        max_concurrency=1,
    )
    return orchestrator
