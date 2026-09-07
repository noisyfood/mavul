from typing import TYPE_CHECKING

from .control import clear_active_session, set_active_session
from .execution import (
    ExecutionError,
    ExecutionResult,
    ExecutionSession,
    UninitializedSessionError,
)
from .models import ReverseTask

if TYPE_CHECKING:
    from .agent import Reverser


def session_for(reverser: "Reverser", task: ReverseTask) -> ExecutionSession:
    """Open or safely resume the task's persistent worker session."""
    with task.session_lock:
        if task.session is not None:
            return task.session
        previous_session_id = task.session_id
        try:
            session = reverser._execution.open_worker(
                task.work_dir,
                previous_session_id,
            )
        except UninitializedSessionError as exc:
            if task.session_initialized:
                raise ExecutionError(
                    "initialized Codex thread history is unavailable"
                ) from exc
            session = reverser._execution.open_worker(task.work_dir, None)
            task.session_initialized = False
            task.session_id = None
        if task.session_id and session.session_id != task.session_id:
            session.close()
            raise ExecutionError("Codex resumed an unexpected thread")
        if task.session_id is None:
            task.session_id = session.session_id
            try:
                reverser._workspace.persist_task(task)
            except Exception:
                task.session_id = previous_session_id
                session.close()
                raise
        task.session = session
        return session


def run_session(
    reverser: "Reverser",
    task: ReverseTask,
    session: ExecutionSession,
    prompt: str,
) -> ExecutionResult:
    """Run one turn while publishing its interrupt handle and persistence point."""
    set_active_session(task, session)
    try:
        return session.run(
            prompt,
            on_started=lambda: mark_session_initialized(reverser, task),
        )
    finally:
        clear_active_session(task, session)


def mark_session_initialized(reverser: "Reverser", task: ReverseTask) -> None:
    """Persist that Codex has created resumable rollout history."""
    with task.session_lock:
        if task.session_initialized:
            return
        task.session_initialized = True
        try:
            reverser._workspace.persist_task(task)
        except Exception:
            task.session_initialized = False
            raise
