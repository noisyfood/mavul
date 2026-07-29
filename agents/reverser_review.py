from typing import Any

from agents.reverser_types import ReverseTask
from agents.reverser_workspace import ReverserWorkspace


class ResultReviewer:
    """Validate evidence deterministically before expert LLM review."""

    def __init__(
        self,
        conversation: Any,
        workspace: ReverserWorkspace,
    ) -> None:
        self._conversation = conversation
        self._workspace = workspace

    def review(self, task: ReverseTask, output: str) -> str:
        if not output.strip():
            return "REVISE\nThe worker did not provide a behavior model."
        if output.lstrip().upper().startswith("BLOCKED:"):
            return f"BLOCKED\n{output.partition(':')[2].strip()}"

        evidence = task.workspace / "evidence"
        manifest, paths = self._workspace.evidence_manifest(evidence)
        if not paths:
            return "REVISE\nNo evidence artifacts exist in the task workspace."
        if not any(path in output for path in paths):
            return "REVISE\nThe behavior model does not cite an evidence artifact."
        return self._conversation.ask_agent(
            "Review this reverse-engineering result.\n\n"
            f"Task ID: {task.task_id}\n"
            f"Objective: {task.objective}\n"
            "Evidence manifest and bounded excerpts:\n"
            f"{manifest}\n\n"
            f"Worker result:\n{output}"
        )
