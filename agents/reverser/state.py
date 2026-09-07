from typing import TYPE_CHECKING

from .models import ReverseTask

if TYPE_CHECKING:
    from .agent import Reverser


TRANSITIONS = {
    "created": {"waiting_for_device", "running", "paused", "blocked"},
    "waiting_for_device": {"running", "cleaning_up", "paused", "blocked"},
    "running": {"reviewing", "cleaning_up", "paused", "blocked"},
    "reviewing": {"running", "cleaning_up", "paused", "blocked"},
    "cleaning_up": {"ready_to_deliver", "paused", "blocked"},
    "paused": {"waiting_for_device", "running", "cleaning_up"},
    "blocked": {"waiting_for_device", "running", "cleaning_up"},
    "ready_to_deliver": {"delivered"},
    "delivered": set(),
}


def set_task_status(
    reverser: "Reverser",
    task: ReverseTask,
    status: str,
    summary: str | None,
) -> None:
    """Commit task state and roll memory/disk back together on failure."""
    with reverser._lock:
        if status != task.status and status not in TRANSITIONS[task.status]:
            raise RuntimeError(f"invalid task transition: {task.status} -> {status}")
        old_status, old_summary = task.status, task.summary
        task.status = status
        if summary is not None:
            task.summary = summary
        try:
            reverser._workspace.persist_task(task)
            reverser._workspace.write_index(reverser._tasks)
        except Exception as exc:
            task.status, task.summary = old_status, old_summary
            try:
                reverser._workspace.persist_task(task)
                reverser._workspace.write_index(reverser._tasks)
            except Exception as rollback_error:
                exc.add_note(f"task state rollback also failed: {rollback_error}")
            raise
