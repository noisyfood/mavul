import threading
from collections.abc import Callable, Mapping
from typing import Any

from openhands.sdk import ConversationExecutionStatus
from openhands.sdk.conversation import get_agent_final_response

from agents.reverser_control import interrupt_all, stop
from agents.reverser_delegation import DelegationRegistry
from agents.reverser_mailbox import ReverserMailbox
from agents.reverser_registration import register
from agents.reverser_review import ResultReviewer
from agents.reverser_sdk import OpenHandsRuntime
from agents.reverser_types import ReverseTask, ReverseTaskResult
from agents.reverser_workspace import ReverserWorkspace


_TRANSITIONS = {
    "created": {"running"},
    "running": {"reviewing", "paused", "blocked"},
    "reviewing": {"running", "ready_to_deliver", "paused", "blocked"},
    "paused": {"running"},
    "blocked": {"running"},
    "ready_to_deliver": {"delivered"},
    "delivered": set(),
}
_ACTIVE_STATUSES = {"running", "reviewing"}


class Reverser:
    """Coordinate persistent reverse-engineering workers and review results."""

    def __init__(
        self,
        config: Mapping[str, Any],
        submit: Callable[[Any], None] | None = None,
        register_child: Callable[..., bool] | None = None,
        unregister_child: Callable[[str], None] | None = None,
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

        self.max_concurrency = max_concurrency
        self._lock = threading.RLock()
        self._stopping = False
        self._closed = False
        self._workspace = ReverserWorkspace(workspace)
        self.workspace = self._workspace.root
        self._tasks = self._workspace.load_tasks()
        self._runtime = OpenHandsRuntime(self.config)
        self._review_conversation = self._runtime.create_reviewer(self.workspace)
        self._reviewer = ResultReviewer(
            self._review_conversation,
            self._workspace,
        )
        self._mailbox = ReverserMailbox(self, submit)
        self.delegation = DelegationRegistry(register_child, unregister_child)
        self.delegation.restore(self._tasks.values())

    def _conversation_for(self, task: ReverseTask) -> Any:
        with task.conversation_lock:
            if task.conversation is None:
                task.conversation = self._runtime.create_worker(task)
            return task.conversation

    def start_task(self, task_id: str, instruction: str) -> ReverseTaskResult:
        """Start one top-level reverse-engineering Conversation."""
        if not instruction.strip():
            raise ValueError("instruction must not be empty")
        with self._lock:
            if self._stopping:
                raise RuntimeError("Reverser is stopping")
            if task_id in self._tasks:
                raise ValueError(f"Reverser task already exists: {task_id}")
            task = self._workspace.create_task(task_id, instruction)
            self._tasks[task_id] = task
            self._set_status(task, "running")
        return self._run_task(
            task,
            instruction,
            sender="orchestrator",
            allowed_statuses={"running"},
        )

    def _run_task(
        self,
        task: ReverseTask,
        instruction: str,
        sender: str,
        allowed_statuses: set[str],
        clear_interrupt: bool = False,
    ) -> ReverseTaskResult:
        with task.run_lock:
            with self._lock:
                if task.status not in allowed_statuses:
                    raise RuntimeError(
                        f"cannot run task {task.task_id} from status {task.status}"
                    )
                if clear_interrupt:
                    task.interrupt_requested.clear()
                if task.status != "running":
                    self._set_status(task, "running")
            if task.interrupt_requested.is_set():
                return self._pause_task(task, "Task interrupted before running.")
            return self._run_locked(task, instruction, sender)

    def _run_locked(
        self,
        task: ReverseTask,
        instruction: str,
        sender: str,
    ) -> ReverseTaskResult:
        conversation = self._conversation_for(task)
        conversation.send_message(instruction, sender=sender)
        while True:
            try:
                if task.status != "running":
                    self._set_status(task, "running")
                conversation.run()
            except Exception as exc:
                return self._block_task(task, f"Conversation failed: {exc}")

            execution_status = conversation.state.execution_status
            if (
                task.interrupt_requested.is_set()
                or execution_status == ConversationExecutionStatus.PAUSED
            ):
                return self._pause_task(task, "Task paused.")
            if execution_status != ConversationExecutionStatus.FINISHED:
                return self._block_task(
                    task,
                    f"Conversation stopped with status: {execution_status.value}",
                )

            output = get_agent_final_response(conversation.state.events)
            self._set_status(
                task,
                "reviewing",
                self._workspace.short_summary(output),
            )
            try:
                decision = self._reviewer.review(task, output)
            except Exception as exc:
                return self._block_task(task, f"Review failed: {exc}")
            if task.interrupt_requested.is_set():
                return self._pause_task(task, "Task paused during review.")

            decision_type, _, details = decision.partition("\n")
            if decision_type.strip().upper() == "APPROVE":
                self._workspace.save_result(task, output)
                self._set_status(
                    task,
                    "ready_to_deliver",
                    self._workspace.short_summary(output),
                )
                return ReverseTaskResult(task.task_id, task.status, output)
            if decision_type.strip().upper() == "BLOCKED":
                return self._block_task(task, details.strip() or decision.strip())

            feedback = details.strip() or decision.strip()
            conversation.send_message(
                "The lead Reverser rejected the result. Continue the same "
                f"Conversation and address this review:\n\n{feedback}",
                sender="reverser",
            )

    def _pause_task(self, task: ReverseTask, reason: str) -> ReverseTaskResult:
        summary = self._save_progress(task, reason)
        self._set_status(task, "paused", self._workspace.short_summary(summary))
        return ReverseTaskResult(task.task_id, "paused", summary)

    def _block_task(self, task: ReverseTask, reason: str) -> ReverseTaskResult:
        if task.conversation is not None:
            task.conversation.pause()
        summary = self._save_progress(task, reason)
        self._set_status(task, "blocked", self._workspace.short_summary(reason))
        return ReverseTaskResult(task.task_id, "blocked", summary)

    def interrupt_task(self, task_id: str) -> str:
        """Request a pause and wait until the task has saved its stopping point."""
        task = self._get_task(task_id)
        with self._lock:
            if task.status not in _ACTIVE_STATUSES:
                raise RuntimeError(f"cannot interrupt task in status {task.status}")
            task.interrupt_requested.set()
        with task.conversation_lock:
            conversation = task.conversation
        if conversation is not None:
            conversation.pause()
        with task.run_lock:
            if task.status in _ACTIVE_STATUSES:
                result = self._pause_task(task, "Interrupted by Orchestrator.")
                return result.output
            return task.summary

    def interrupt_all(self, reason: str) -> str:
        """Pause and persist all active top-level tasks."""
        return interrupt_all(self, reason)

    def resume_task(self, task_id: str, notes: str) -> ReverseTaskResult:
        """Resume the same persisted Conversation with Orchestrator notes."""
        if not notes.strip():
            raise ValueError("resume notes must not be empty")
        return self._run_task(
            self._get_task(task_id),
            notes,
            sender="orchestrator",
            allowed_statuses={"paused", "blocked"},
            clear_interrupt=True,
        )

    def query_task(self, task_id: str) -> str:
        """Return a /btw-style overview for one global task ID."""
        task = self._get_task(task_id)
        if task.status not in _ACTIVE_STATUSES:
            return f"Status: {task.status}\nSummary: {task.summary}"
        return self._conversation_for(task).ask_agent(
            "Briefly report established facts, unresolved questions, blockers, "
            "and the next intended step."
        )

    def mark_delivered(self, task_id: str) -> None:
        task = self._get_task(task_id)
        if task.status != "ready_to_deliver":
            raise RuntimeError(f"task is not ready to deliver: {task_id}")
        self._set_status(task, "delivered")

    def _save_progress(self, task: ReverseTask, reason: str) -> str:
        try:
            progress = (
                task.conversation.ask_agent(
                    "Summarize the stopping point, facts with evidence, incomplete "
                    "work, blockers, and the exact next step."
                )
                if task.conversation is not None
                else task.summary
            )
        except Exception as exc:
            progress = f"Unable to summarize Conversation: {exc}"
        self._workspace.save_progress(task, reason, progress)
        return progress

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
        with self._lock:
            if status != task.status and status not in _TRANSITIONS[task.status]:
                transition = f"{task.status} -> {status}"
                raise RuntimeError(f"invalid task transition: {transition}")
            task.status = status
            if summary is not None:
                task.summary = summary
            self._workspace.persist_task(task)
            self._workspace.write_index(self._tasks)

    def receive(self, envelope: Any) -> None:
        """Deliver one routed Envelope to the Reverser main Agent mailbox."""
        self._mailbox.receive(envelope)

    def stop(self) -> None:
        """Quiesce task runners, save progress, and close SDK resources."""
        stop(self)
