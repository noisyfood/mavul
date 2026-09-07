from typing import TYPE_CHECKING

from .execution import ExecutionSession
from .models import ReverseTask, ReverseTaskResult

if TYPE_CHECKING:
    from .agent import Reverser


ACTIVE_STATUSES = {"running", "reviewing", "cleaning_up"}


def set_active_session(task: ReverseTask, session: ExecutionSession) -> None:
    with task.active_session_lock:
        if task.active_session is not None:
            raise RuntimeError("task already has an active execution session")
        task.active_session = session
        interrupt_requested = task.interrupt_requested.is_set()
    if interrupt_requested:
        session.interrupt()


def clear_active_session(task: ReverseTask, session: ExecutionSession) -> None:
    with task.active_session_lock:
        if task.active_session is session:
            task.active_session = None


def interrupt_active_session(task: ReverseTask) -> None:
    with task.active_session_lock:
        session = task.active_session
    if session is not None:
        session.interrupt()


def save_progress(reverser: "Reverser", task: ReverseTask, reason: str) -> str:
    """Write a deterministic checkpoint without delaying interruption."""
    progress = task.summary or (
        "No reviewed summary is available; reconstruct from the task work "
        "directory and Codex thread."
    )
    reverser._workspace.save_progress(task, reason, progress)
    return progress


def pause_task(
    reverser: "Reverser",
    task: ReverseTask,
    reason: str,
) -> ReverseTaskResult:
    summary = save_progress(reverser, task, reason)
    reverser._set_status(
        task,
        "paused",
        reverser._workspace.short_summary(summary),
    )
    return ReverseTaskResult(task.task_id, "paused", summary)


def record_failure(
    reverser: "Reverser",
    task: ReverseTask,
    error: Exception,
) -> None:
    """Best-effort checkpoint for failures that must still reach AgentSystem."""
    try:
        with task.session_lock:
            if task.session is not None:
                task.session.close()
                task.session = None
    except Exception as cleanup_error:
        error.add_note(f"session cleanup also failed: {cleanup_error}")
    try:
        reverser._workspace.save_progress(task, "Runtime failure", str(error))
        if task.status in ACTIVE_STATUSES:
            reverser._set_status(
                task,
                "paused",
                reverser._workspace.short_summary(str(error)),
            )
    except Exception as checkpoint_error:
        error.add_note(f"failure checkpoint also failed: {checkpoint_error}")


def interrupt_all(reverser: "Reverser", reason: str) -> str:
    """Interrupt every active turn, then wait for persisted stopping points."""
    with reverser._lock:
        tasks = [
            task
            for task in reverser._tasks.values()
            if task.status in ACTIVE_STATUSES
        ]
        for task in tasks:
            task.interrupt_requested.set()
    failures: list[str] = []
    for task in tasks:
        try:
            interrupt_active_session(task)
        except Exception as exc:
            failures.append(f"{task.task_id}: interrupt failed: {exc}")

    summaries: list[str] = []
    for task in tasks:
        with task.run_lock:
            if task.status in ACTIVE_STATUSES:
                summary = pause_task(reverser, task, reason).output
            else:
                summary = task.summary
            summaries.append(f"{task.task_id}: {summary}")
    summaries.extend(failures)
    return "\n".join(summaries) if summaries else "No active Reverser tasks."


def stop(reverser: "Reverser") -> None:
    """Idempotently quiesce turns before closing the shared Codex client."""
    with reverser._stop_lock:
        with reverser._lock:
            if reverser._closed:
                return
            reverser._stopping = True
            tasks = list(reverser._tasks.values())
            for task in tasks:
                if task.status in ACTIVE_STATUSES:
                    task.interrupt_requested.set()

        failures: list[Exception] = []
        for task in tasks:
            try:
                interrupt_active_session(task)
            except Exception as exc:
                failures.append(exc)
        for task in tasks:
            try:
                with task.run_lock:
                    if task.status in ACTIVE_STATUSES:
                        pause_task(reverser, task, "Reverser stopping.")
                    with task.session_lock:
                        if task.session is not None:
                            task.session.close()
                            task.session = None
            except Exception as exc:
                failures.append(exc)
        try:
            reverser._execution.close()
        except Exception as exc:
            failures.append(exc)

        if failures:
            raise RuntimeError(
                f"Reverser cleanup failed in {len(failures)} operation(s)"
            ) from failures[0]
        with reverser._lock:
            reverser._closed = True
