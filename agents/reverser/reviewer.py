import json

from .execution import (
    ExecutionError,
    ExecutionSession,
    ExecutionStatus,
    ReviewDecision,
    ReviewVerdict,
)
from .models import ReverseTask
from .prompts import REVIEW_OUTPUT_SCHEMA
from .workspace import ReverserWorkspace


class ResultReviewer:
    """Validate evidence deterministically before expert LLM review."""

    def __init__(
        self,
        workspace: ReverserWorkspace,
    ) -> None:
        self._workspace = workspace

    def review(
        self,
        task: ReverseTask,
        output: str,
        session: ExecutionSession,
    ) -> ReviewDecision:
        if not output.strip():
            return ReviewDecision(
                ReviewVerdict.REVISE,
                "The worker did not provide a behavior model.",
            )
        evidence = task.work_dir / "evidence"
        paths = sorted(
            path.relative_to(task.work_dir).as_posix()
            for path in evidence.rglob("*")
            if path.is_file()
        )
        if not paths:
            return ReviewDecision(
                ReviewVerdict.REVISE,
                "No evidence artifacts exist in the task workspace.",
            )
        if not any(path in output for path in paths):
            return ReviewDecision(
                ReviewVerdict.REVISE,
                "The behavior model does not cite an evidence artifact.",
            )
        result = session.run(
            "Review this reverse-engineering result.\n\n"
            f"Task ID: {task.task_id}\n"
            f"Objective: {task.objective}\n"
            "Approve only if the result cites evidence/device-operations.md "
            "and describes at least one behavior checked on the real Device.\n"
            "Open and inspect these evidence files in the read-only workspace:\n"
            + "\n".join(f"- {path}" for path in paths)
            + "\n\n"
            f"Worker result:\n{output}",
            REVIEW_OUTPUT_SCHEMA,
        )
        if result.status == ExecutionStatus.INTERRUPTED:
            return ReviewDecision(ReviewVerdict.REVISE, "Review was interrupted.")
        try:
            payload = json.loads(result.output)
            verdict = ReviewVerdict(payload["verdict"])
            feedback = payload["feedback"]
            if not isinstance(feedback, str):
                raise TypeError("feedback must be a string")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ExecutionError("Codex reviewer returned an invalid decision") from exc
        return ReviewDecision(verdict, feedback.strip())
