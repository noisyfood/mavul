import json
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from system.device import DeviceEndpoint, LeaseStatus
from system.device_runtime import DeviceRuntime
from system.envelope import Envelope

from .control import (
    ACTIVE_STATUSES,
    clear_active_session,
    interrupt_active_session,
    set_active_session,
)
from .control import interrupt_all, pause_task, record_failure, stop
from .delegation import DelegationRegistry
from .execution import (
    ExecutionBackend,
    ExecutionError,
    ExecutionStatus,
    ReviewVerdict,
)
from .handler import ReverserEnvelopeHandler
from .models import ReverserRequestError, ReverseTask, ReverseTaskResult
from .reviewer import ResultReviewer
from .sessions import run_session, session_for
from .state import set_task_status
from .workspace import ReverserWorkspace


class Reverser:
    """Coordinate persistent reverse-engineering workers and review results."""

    agent_id = "reverser"

    def __init__(
        self,
        config: Mapping[str, Any],
        devices: DeviceRuntime,
        submit: Callable[[Envelope], None] | None = None,
        register_child: Callable[..., bool] | None = None,
        unregister_child: Callable[[str], None] | None = None,
        execution_backend: ExecutionBackend | None = None,
    ) -> None:
        self.config = dict(config)
        workspace = self.config.get("workspace")
        if not workspace:
            raise ValueError("missing Reverser configuration: workspace")
        max_concurrency = self.config.get("max_concurrency", 1)
        if (
            not isinstance(max_concurrency, int)
            or isinstance(max_concurrency, bool)
            or max_concurrency < 1
        ):
            raise ValueError("max_concurrency must be a positive integer")

        if execution_backend is None:
            from .codex import CodexExecutionBackend

            execution_backend = CodexExecutionBackend(self.config)
        self.max_concurrency = max_concurrency
        self._lock = threading.RLock()
        self._stop_lock = threading.Lock()
        self._stopping = False
        self._closed = False
        self._pending_interrupts: dict[str, int | None] = {}
        self._workspace = ReverserWorkspace(workspace)
        self.workspace = self._workspace.root
        self._tasks = self._workspace.load_tasks()
        self._devices = devices
        self._execution = execution_backend
        if any(
            task.execution_backend != self._execution.name
            for task in self._tasks.values()
        ):
            raise RuntimeError("Reverser task uses an incompatible execution backend")
        self._reviewer = ResultReviewer(self._workspace)
        self._envelope_handler = ReverserEnvelopeHandler(self, submit)
        self.delegation = DelegationRegistry(register_child, unregister_child)
        self.delegation.restore(self._tasks.values())

    def start_task(
        self,
        task_id: str,
        instruction: str,
        information: Mapping[str, Any],
    ) -> ReverseTaskResult:
        """Start one top-level reverse-engineering session."""
        if not instruction.strip():
            raise ValueError("instruction must not be empty")
        details = self._task_details(information)
        endpoint = self._device_endpoint(details["device_id"])
        with self._lock:
            if self._stopping:
                raise ReverserRequestError("Reverser is stopping")
            if task_id in self._tasks:
                raise ValueError(f"Reverser task already exists: {task_id}")
            task = self._workspace.create_task(
                task_id,
                instruction,
                **details,
            )
            self._tasks[task_id] = task
            if task_id in self._pending_interrupts:
                task.last_interrupt_order = self._pending_interrupts.pop(task_id)
                task.interrupt_requested.set()
        if task.interrupt_requested.is_set():
            return self._pause_task(task, "Interrupted before task start.")
        waiting = self._acquire_device(task)
        if waiting is not None:
            return waiting
        return self._run_task(
            task,
            self._task_prompt(task, endpoint),
            {"created"},
        )

    def _run_task(
        self,
        task: ReverseTask,
        instruction: str,
        allowed_statuses: set[str],
    ) -> ReverseTaskResult:
        with task.run_lock:
            with self._lock:
                if self._stopping:
                    raise ReverserRequestError("Reverser is stopping")
                if task.status not in allowed_statuses:
                    raise ReverserRequestError(
                        f"cannot run task {task.task_id} from status {task.status}"
                    )
                task.pending_instruction = ""
                if task.status != "running":
                    self._set_status(task, "running")
            if task.interrupt_requested.is_set():
                return self._pause_task(task, "Task interrupted before running.")
            return self._run_locked(task, instruction)

    def _run_locked(
        self,
        task: ReverseTask,
        instruction: str,
    ) -> ReverseTaskResult:
        try:
            migrated_without_thread = bool(
                task.legacy_session_id and task.session_id is None
            )
            session = session_for(self, task)
            prompt = instruction
            if migrated_without_thread:
                prompt = (
                    "This task was migrated from another runtime. Reconstruct "
                    f"context from workspace files.\n\nObjective: {task.objective}"
                    f"\nPrevious summary: {task.summary or 'See progress.md.'}"
                    f"\n\n{instruction}"
                )
            while True:
                if task.status != "running":
                    self._set_status(task, "running")
                if task.interrupt_requested.is_set():
                    return self._pause_task(task, "Task paused.")
                result = run_session(self, task, session, prompt)
                if (
                    task.interrupt_requested.is_set()
                    or result.status == ExecutionStatus.INTERRUPTED
                ):
                    return self._pause_task(task, "Task paused.")

                output = result.output
                self._set_status(
                    task,
                    "reviewing",
                    self._workspace.short_summary(output),
                )
                if task.interrupt_requested.is_set():
                    return self._pause_task(task, "Task paused before review.")
                reviewer = self._execution.open_reviewer(task.work_dir)
                try:
                    set_active_session(task, reviewer)
                    decision = self._reviewer.review(task, output, reviewer)
                finally:
                    clear_active_session(task, reviewer)
                    reviewer.close()
                if task.interrupt_requested.is_set():
                    return self._pause_task(task, "Task paused during review.")

                if decision.verdict == ReviewVerdict.APPROVE:
                    self._workspace.save_result(task, output)
                    cleanup_result = self._cleanup_device(task, session)
                    if cleanup_result is not None:
                        return cleanup_result
                    self._set_status(
                        task,
                        "ready_to_deliver",
                        self._workspace.short_summary(output),
                    )
                    return ReverseTaskResult(task.task_id, task.status, output)
                if decision.verdict == ReviewVerdict.BLOCKED:
                    if task.device_changes_pending:
                        return self._block_task(task, decision.feedback)
                    cleanup_result = self._cleanup_device(task, session)
                    if cleanup_result is not None:
                        return cleanup_result
                    return self._block_task(task, decision.feedback)
                prompt = (
                    "The lead Reverser rejected the result. Continue the same "
                    "session and address this review:\n\n"
                    + decision.feedback
                )
        except ExecutionError as exc:
            return self._execution_failed(task, exc)
        except Exception as exc:
            record_failure(self, task, exc)
            raise

    def _pause_task(self, task: ReverseTask, reason: str) -> ReverseTaskResult:
        return pause_task(self, task, reason)

    def _block_task(self, task: ReverseTask, reason: str) -> ReverseTaskResult:
        self._workspace.save_progress(task, reason, reason)
        with self._lock:
            if not task.interrupt_requested.is_set():
                self._set_status(task, "blocked", self._workspace.short_summary(reason))
                return ReverseTaskResult(task.task_id, "blocked", reason)
        return self._pause_task(task, "Task paused after review.")

    def _cleanup_device(
        self,
        task: ReverseTask,
        session: Any,
    ) -> ReverseTaskResult | None:
        self._set_status(task, "cleaning_up")
        result = run_session(
            self,
            task,
            session,
            """
Finish this task on its leased Device:

1. Undo every Device change made by this task.
2. Remove `device-changes-pending` only after the undo succeeds.
3. Append the undo commands, Device output, errors, and Telnet exit to
   `evidence/device-operations.md` in execution order.
4. Exit Telnet so no Device process remains open.

Reply with exactly `CLEANUP_COMPLETE` only after all four steps succeed. If a
step fails, explain the failure and leave `device-changes-pending` in place.
""".strip(),
        )
        if (
            task.interrupt_requested.is_set()
            or result.status == ExecutionStatus.INTERRUPTED
        ):
            return self._pause_task(task, "Task paused during Device cleanup.")
        if result.output.strip() != "CLEANUP_COMPLETE":
            return self._block_task(
                task,
                "Device cleanup did not complete: " + result.output.strip(),
            )
        if task.device_changes_pending:
            return self._block_task(
                task,
                "Device cleanup reported success but changes are still pending.",
            )
        try:
            with task.session_lock:
                if task.session is session:
                    session.close()
                    task.session = None
        except Exception as exc:
            return self._block_task(task, f"Unable to close Telnet session: {exc}")
        try:
            self._release_device(task)
        except Exception as exc:
            return self._block_task(task, f"Unable to release Device: {exc}")
        return None

    def _execution_failed(
        self,
        task: ReverseTask,
        error: ExecutionError,
    ) -> ReverseTaskResult:
        reason = f"Execution failed: {error}"
        with task.session_lock:
            session = task.session
        if (
            session is not None
            and task.status != "cleaning_up"
            and not task.device_changes_pending
        ):
            try:
                cleanup_result = self._cleanup_device(task, session)
            except ExecutionError as cleanup_error:
                reason += f"; Device cleanup also failed: {cleanup_error}"
            else:
                if cleanup_result is None:
                    return self._block_task(task, reason)
                if cleanup_result.status == "paused":
                    return cleanup_result
                return self._block_task(
                    task,
                    reason + "; " + cleanup_result.output,
                )
        try:
            with task.session_lock:
                if task.session is not None:
                    task.session.close()
                    task.session = None
        except Exception as close_error:
            reason += f"; session close also failed: {close_error}"
        if (
            session is None
            and task.status != "cleaning_up"
            and not task.device_changes_pending
        ):
            try:
                self._release_device(task)
            except Exception as release_error:
                reason += f"; Device release also failed: {release_error}"
        return self._block_task(task, reason)

    def interrupt_task(
        self,
        task_id: str,
        control_order: int | None = None,
    ) -> str:
        """Interrupt an active turn, then wait for its persisted stopping point."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                self._pending_interrupts[task_id] = control_order
                return "Interrupt recorded before task start."
            if control_order is not None:
                previous = task.last_interrupt_order
                if previous is None or control_order > previous:
                    task.last_interrupt_order = control_order
            if task.status == "created":
                task.interrupt_requested.set()
                return "Interrupt recorded before task start."
            if task.status in {"paused", "blocked"}:
                task.interrupt_requested.set()
                return task.summary or "Task is already stopped."
            if task.status not in ACTIVE_STATUSES | {"waiting_for_device"}:
                raise ReverserRequestError(
                    f"cannot interrupt task in status {task.status}"
                )
            task.interrupt_requested.set()
            if task.status == "waiting_for_device":
                task.cancelled_while_waiting = True
        interrupt_active_session(task)
        with task.run_lock:
            if task.status == "waiting_for_device":
                self._devices.cancel_waiter(
                    task.device_id,
                    self.agent_id,
                    task.task_id,
                )
                current = self._devices.get(task.device_id).lease
                if current is not None and (
                    current.agent_id,
                    current.task_id,
                ) == (self.agent_id, task.task_id):
                    task.device_lease = current
                    self._release_device(task)
                return self._pause_task(
                    task,
                    "Interrupted while waiting for the Device.",
                ).output
            if task.status in ACTIVE_STATUSES:
                return self._pause_task(task, "Interrupted by Orchestrator.").output
            return task.summary

    def interrupt_all(self, reason: str) -> str:
        return interrupt_all(self, reason)

    def resume_task(
        self,
        task_id: str,
        notes: str,
        control_order: int | None = None,
    ) -> ReverseTaskResult:
        if not notes.strip():
            raise ValueError("resume notes must not be empty")
        with self._lock:
            if self._stopping:
                raise ReverserRequestError("Reverser is stopping")
            task = self._get_task(task_id)
            if task.status not in {"paused", "blocked"}:
                raise ReverserRequestError(
                    f"cannot resume task in status {task.status}"
                )
            if (
                control_order is not None
                and task.last_interrupt_order is not None
                and control_order < task.last_interrupt_order
            ):
                return ReverseTaskResult(task.task_id, task.status, task.summary)
            task.interrupt_requested.clear()
        instruction = notes
        if task.session_id is None:
            instruction = (
                self._task_prompt(
                    task,
                    self._device_endpoint(task.device_id),
                )
                + "\n\nResume notes: "
                + notes
            )
        if task.device_lease is None:
            waiting = self._acquire_device(task, instruction)
            if waiting is not None:
                return waiting
        if (task.workspace / "analysis" / "behavior-model.md").is_file():
            return self._resume_cleanup(task)
        return self._run_task(
            task,
            instruction,
            {"paused", "blocked"},
        )

    def device_available(self, task_id: str) -> ReverseTaskResult:
        task = self._get_task(task_id)
        with task.run_lock:
            with self._lock:
                status = task.status
                cancelled = task.cancelled_while_waiting
            if status != "waiting_for_device":
                if cancelled:
                    current = self._devices.get(task.device_id).lease
                    if current is not None and (
                        current.agent_id,
                        current.task_id,
                    ) == (self.agent_id, task.task_id):
                        task.device_lease = current
                        self._release_device(task)
                return ReverseTaskResult(task.task_id, task.status, task.summary)
            if cancelled:
                current = self._devices.get(task.device_id).lease
                if current is not None and (
                    current.agent_id,
                    current.task_id,
                ) == (self.agent_id, task.task_id):
                    task.device_lease = current
                    self._release_device(task)
                task.device_lease = None
                return self._pause_task(
                    task,
                    "Interrupted while waiting for the Device.",
                )
            request = self._devices.acquire(
                task.device_id,
                self.agent_id,
                task.task_id,
            )
            if request.status != LeaseStatus.GRANTED or request.lease is None:
                raise ReverserRequestError(
                    f"Device notification did not grant {task.device_id}"
                )
            task.device_lease = request.lease
            cleanup_pending = (
                task.workspace / "analysis" / "behavior-model.md"
            ).is_file()
            with self._lock:
                cancelled = task.cancelled_while_waiting
                if not cancelled and task.status == "waiting_for_device":
                    next_status = "cleaning_up" if cleanup_pending else "running"
                    self._set_status(task, next_status)
                    claimed = True
                else:
                    claimed = False
            if cancelled:
                self._release_device(task)
                return self._pause_task(
                    task,
                    "Interrupted while waiting for the Device.",
                )
            if not claimed:
                return ReverseTaskResult(task.task_id, task.status, task.summary)
            instruction = task.pending_instruction
            if not instruction:
                instruction = self._task_prompt(
                    task,
                    self._device_endpoint(task.device_id),
                )
            if cleanup_pending:
                return self._resume_cleanup(task)
            return self._run_task(
                task,
                instruction,
                {"running"},
            )

    def _resume_cleanup(self, task: ReverseTask) -> ReverseTaskResult:
        with task.run_lock:
            with self._lock:
                if self._stopping:
                    raise ReverserRequestError("Reverser is stopping")
                if task.status not in {"paused", "blocked", "cleaning_up"}:
                    raise ReverserRequestError(
                        f"cannot resume cleanup from status {task.status}"
                    )
                if task.status != "cleaning_up":
                    self._set_status(task, "cleaning_up")
            if task.interrupt_requested.is_set():
                return self._pause_task(task, "Task paused before Device cleanup.")
            try:
                session = session_for(self, task)
                cleanup_result = self._cleanup_device(task, session)
            except ExecutionError as exc:
                return self._execution_failed(task, exc)
            if cleanup_result is not None:
                return cleanup_result
            output = (
                task.workspace / "analysis" / "behavior-model.md"
            ).read_text(encoding="utf-8")
            self._set_status(
                task,
                "ready_to_deliver",
                self._workspace.short_summary(output),
            )
            return ReverseTaskResult(task.task_id, task.status, output)

    def _acquire_device(
        self,
        task: ReverseTask,
        instruction: str | None = None,
    ) -> ReverseTaskResult | None:
        with task.run_lock:
            task.cancelled_while_waiting = False
            request = self._devices.acquire(
                task.device_id,
                self.agent_id,
                task.task_id,
            )
            with self._lock:
                interrupted = task.interrupt_requested.is_set()
                if not interrupted and request.status == LeaseStatus.GRANTED:
                    if request.lease is None:
                        raise RuntimeError("granted Device request has no lease")
                    task.device_lease = request.lease
                    return None
                if not interrupted and request.status == LeaseStatus.WAITING:
                    task.pending_instruction = instruction or self._task_prompt(
                        task,
                        self._device_endpoint(task.device_id),
                    )
                    summary = f"Waiting for Device: {task.device_id}"
                    self._set_status(task, "waiting_for_device", summary)
                    return ReverseTaskResult(task.task_id, task.status, summary)
            if interrupted:
                if request.status == LeaseStatus.WAITING:
                    task.cancelled_while_waiting = True
                    self._devices.cancel_waiter(
                        task.device_id,
                        self.agent_id,
                        task.task_id,
                    )
                    current = self._devices.get(task.device_id).lease
                    if current is not None and (
                        current.agent_id,
                        current.task_id,
                    ) == (self.agent_id, task.task_id):
                        task.device_lease = current
                        self._release_device(task)
                elif (
                    request.status == LeaseStatus.GRANTED
                    and request.lease is not None
                ):
                    task.device_lease = request.lease
                    self._release_device(task)
                return self._pause_task(task, "Interrupted before Device use.")
            reason = f"Device is quarantined: {task.device_id}"
            self._set_status(task, "blocked", reason)
            return ReverseTaskResult(task.task_id, "blocked", reason)

    def _release_device(self, task: ReverseTask) -> None:
        lease = task.device_lease
        if lease is None:
            return
        self._devices.release(lease)
        task.device_lease = None

    def _device_endpoint(self, device_id: str) -> DeviceEndpoint:
        endpoint = self._devices.get(device_id).endpoint
        if endpoint.telnet_port is None:
            raise ReverserRequestError(
                f"Device '{device_id}' requires telnet_port"
            )
        return endpoint

    @staticmethod
    def _task_details(information: Mapping[str, Any]) -> dict[str, str]:
        if not isinstance(information, Mapping):
            raise TypeError("Reverser task information must be an object")
        fields = (
            "target_id",
            "device_id",
            "binary_path",
            "username",
            "password",
            "enable_password",
        )
        details = {}
        for field in fields:
            value = information.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Reverser task requires {field}")
            details[field] = value
        return details

    @staticmethod
    def _task_prompt(task: ReverseTask, endpoint: DeviceEndpoint) -> str:
        connection = {
            "target_id": task.target_id,
            "device_id": task.device_id,
            "device_name": endpoint.name,
            "ip_address": endpoint.ip_address,
            "telnet_port": endpoint.telnet_port,
            "username": task.username,
            "password": task.password,
            "enable_password": task.enable_password,
            "binary_name": Path(task.binary_path).name,
            "binary_path": task.binary_path,
        }
        return (
            f"Objective: {task.objective}\n\n"
            "Task inputs:\n"
            + json.dumps(connection, ensure_ascii=False, indent=2)
            + "\n\n"
            "Use the binary already open in IDA MCP whose path matches "
            "`binary_path`. If it is not open, return `BLOCKED:` with the "
            "reason. Use `/usr/bin/telnet <ip_address> <telnet_port>` in a "
            "PTY, then send the username, login password, `enable`, and the "
            "enable password through repeated terminal input. Keep that "
            "Telnet process for later turns in this task.\n\n"
            "Record every Device command, output, error, and its time in "
            "`evidence/device-operations.md` in execution order. Check at "
            "least one real Device behavior and cite that file in the final "
            "behavior model. Before the first command that changes the Device, "
            "create `device-changes-pending`; leave it until every change has "
            "been undone."
        )

    def query_task(self, task_id: str) -> str:
        task = self._get_task(task_id)
        if task.status not in ACTIVE_STATUSES or task.session_id is None:
            return f"Status: {task.status}\nSummary: {task.summary}"
        with task.session_lock:
            session = task.session
        if session is None:
            return f"Status: {task.status}\nSummary: {task.summary}"
        return session.query(
            "Report this task's current evidence-backed progress and next step."
        )

    def mark_delivered(self, task_id: str) -> None:
        task = self._get_task(task_id)
        if task.status != "ready_to_deliver":
            raise RuntimeError(f"task is not ready to deliver: {task_id}")
        self._set_status(task, "delivered")

    def _get_task(self, task_id: str) -> ReverseTask:
        with self._lock:
            try:
                return self._tasks[task_id]
            except KeyError:
                raise LookupError(f"unknown Reverser task: {task_id}") from None

    def _set_status(
        self,
        task: ReverseTask,
        status: str,
        summary: str | None = None,
    ) -> None:
        set_task_status(self, task, status, summary)

    def receive(self, envelope: Envelope) -> None:
        self._envelope_handler.receive(envelope)

    def stop(self) -> None:
        stop(self)
