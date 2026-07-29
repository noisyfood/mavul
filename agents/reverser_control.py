from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agents.reverser import Reverser


def interrupt_all(reverser: "Reverser", reason: str) -> str:
    """Pause every active top-level task before waiting for their runners."""
    active_statuses = {"running", "reviewing"}
    with reverser._lock:
        tasks = [
            task
            for task in reverser._tasks.values()
            if task.status in active_statuses
        ]
        for task in tasks:
            task.interrupt_requested.set()
            if task.conversation is not None:
                task.conversation.pause()

    summaries: list[str] = []
    for task in tasks:
        with task.run_lock:
            if task.status in active_statuses:
                result = reverser._pause_task(task, reason)
                summary = result.output
            else:
                summary = task.summary
            summaries.append(f"{task.task_id}: {summary}")
    return "\n".join(summaries) if summaries else "No active Reverser tasks."


def stop(reverser: "Reverser") -> None:
    """Idempotently quiesce runners and close their SDK resources."""
    active_statuses = {"running", "reviewing"}
    with reverser._lock:
        if reverser._closed:
            return
        reverser._closed = True
        reverser._stopping = True
        tasks = list(reverser._tasks.values())
        for task in tasks:
            if task.status in active_statuses:
                task.interrupt_requested.set()
                if task.conversation is not None:
                    task.conversation.pause()
    for task in tasks:
        with task.run_lock:
            if task.status in active_statuses:
                reverser._pause_task(task, "Reverser stopping.")
            with task.conversation_lock:
                if task.conversation is not None:
                    task.conversation.close()
                    task.conversation = None
    reverser._review_conversation.close()
