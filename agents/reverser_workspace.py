import hashlib
import json
import uuid
from pathlib import Path

from agents.reverser_types import ReverseTask


class ReverserWorkspace:
    """Persist task metadata and human-readable reverse-engineering artifacts."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def create_task(self, task_id: str, objective: str) -> ReverseTask:
        workspace = self.task_path(task_id)
        workspace.mkdir(parents=True, exist_ok=True)
        for directory in ("evidence", "analysis", "delegated"):
            (workspace / directory).mkdir(exist_ok=True)
        conversation_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"mavul/reverser/{task_id}",
        )
        return ReverseTask(task_id, objective, workspace, conversation_id)

    def task_path(self, task_id: str) -> Path:
        if not task_id or Path(task_id).name != task_id or task_id in {".", ".."}:
            raise ValueError("task_id must be a single safe path component")
        workspace = self.root / task_id
        if workspace.is_symlink() or (
            workspace.exists() and workspace.resolve().parent != self.root.resolve()
        ):
            raise ValueError("task workspace must remain under Reverser workspace")
        return workspace

    def load_tasks(self) -> dict[str, ReverseTask]:
        tasks: dict[str, ReverseTask] = {}
        for state_path in self.root.glob("*/task-state.json"):
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                task = ReverseTask(
                    task_id=state["task_id"],
                    objective=state["objective"],
                    workspace=self.task_path(state_path.parent.name),
                    conversation_id=uuid.UUID(state["conversation_id"]),
                    status=state["status"],
                    summary=state.get("summary", ""),
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"invalid Reverser state: {state_path}") from exc
            if task.task_id != state_path.parent.name:
                raise RuntimeError(f"task ID does not match workspace: {state_path}")
            if task.status in {"running", "reviewing"}:
                task.status = "paused"
                self.persist_task(task)
            tasks[task.task_id] = task
        self.write_index(tasks)
        return tasks

    def persist_task(self, task: ReverseTask) -> None:
        state = {
            "task_id": task.task_id,
            "objective": task.objective,
            "conversation_id": str(task.conversation_id),
            "status": task.status,
            "summary": task.summary,
        }
        self.atomic_write(
            self._task_artifact(task, "task-state.json"),
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        )

    def write_index(self, tasks: dict[str, ReverseTask]) -> None:
        lines = [
            "# Reverser Workspace Index",
            "",
            "| Task ID | Objective | Status | Conclusion |",
            "| --- | --- | --- | --- |",
        ]
        for task in sorted(tasks.values(), key=lambda item: item.task_id):
            lines.append(
                "| "
                f"{self._table_text(task.task_id)} | "
                f"{self._table_text(task.objective)} | "
                f"{self._table_text(task.status)} | "
                f"{self._table_text(task.summary)} |"
            )
        self.atomic_write(self.root / "index.md", "\n".join(lines) + "\n")

    def save_progress(self, task: ReverseTask, reason: str, progress: str) -> None:
        document = (
            "# Reverse Task Progress\n\n"
            f"- Task ID: {task.task_id}\n"
            f"- Reason: {reason}\n\n"
            "## Progress\n\n"
            f"{progress}\n"
        )
        self.atomic_write(self._task_artifact(task, "progress.md"), document)

    def save_result(self, task: ReverseTask, output: str) -> None:
        path = self._task_artifact(task, "analysis/behavior-model.md")
        self.atomic_write(path, output)

    @staticmethod
    def short_summary(output: str, limit: int = 240) -> str:
        summary = " ".join(output.split())
        if len(summary) <= limit:
            return summary
        return summary[: limit - 1].rstrip() + "…"

    @staticmethod
    def evidence_manifest(
        evidence: Path,
        excerpt_budget: int = 12_000,
    ) -> tuple[str, list[str]]:
        if evidence.is_symlink():
            raise RuntimeError("evidence directory must not be a symlink")
        evidence_root = evidence.resolve()
        artifacts = sorted(path for path in evidence.rglob("*") if path.is_file())
        sections: list[str] = []
        relative_paths: list[str] = []
        remaining = excerpt_budget
        for path in artifacts:
            if path.is_symlink() or not path.resolve().is_relative_to(evidence_root):
                raise RuntimeError(f"evidence escapes task workspace: {path}")
            relative_path = path.relative_to(evidence).as_posix()
            relative_paths.append(relative_path)
            digest = hashlib.sha256()
            preview = bytearray()
            with path.open("rb") as artifact:
                for chunk in iter(lambda: artifact.read(64 * 1024), b""):
                    digest.update(chunk)
                    if len(preview) < remaining:
                        preview.extend(chunk[: remaining - len(preview)])
            header = (
                f"### {relative_path}\n"
                f"- Size: {path.stat().st_size} bytes\n"
                f"- SHA-256: {digest.hexdigest()}"
            )
            try:
                text = preview.decode("utf-8")
            except UnicodeDecodeError:
                sections.append(f"{header}\n- Preview: binary artifact")
                continue
            sections.append(f"{header}\n\n```text\n{text}\n```")
            remaining -= len(preview)
        return "\n\n".join(sections), relative_paths

    @staticmethod
    def atomic_write(path: Path, content: str) -> None:
        if path.is_symlink():
            raise RuntimeError(f"refusing to replace symlink: {path}")
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as output:
                output.write(content)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _table_text(value: str) -> str:
        return value.replace("|", "\\|").replace("\n", " ").strip()

    @staticmethod
    def _task_artifact(task: ReverseTask, relative: str) -> Path:
        path = task.workspace / relative
        root = task.workspace.resolve()
        if (
            path.is_symlink()
            or path.parent.is_symlink()
            or not path.parent.resolve().is_relative_to(root)
        ):
            raise RuntimeError(f"task artifact escapes workspace: {path}")
        return path
