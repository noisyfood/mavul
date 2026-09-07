import json
import uuid
from collections.abc import Mapping
from pathlib import Path

from .models import ReverseTask


class ReverserWorkspace:
    """Persist task metadata and human-readable reverse-engineering artifacts."""

    SCHEMA_VERSION = 4

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def create_task(
        self,
        task_id: str,
        objective: str,
        *,
        target_id: str = "",
        device_id: str = "",
        binary_path: str = "",
        username: str = "",
        password: str = "",
        enable_password: str = "",
    ) -> ReverseTask:
        workspace = self.task_path(task_id)
        workspace.mkdir(parents=True, exist_ok=True)
        self._prepare_layout(workspace)
        return ReverseTask(
            task_id,
            objective,
            workspace,
            target_id=target_id,
            device_id=device_id,
            binary_path=binary_path,
            username=username,
            password=password,
            enable_password=enable_password,
        )

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
                if state_path.is_symlink():
                    raise ValueError("task state must not be a symlink")
                state = json.loads(state_path.read_text(encoding="utf-8"))
                self._validate_task_state(state)
                workspace = self.task_path(state_path.parent.name)
                self._prepare_layout(workspace, migrate_legacy=True)
                (
                    session_id,
                    session_initialized,
                    legacy_session_id,
                    migrated,
                ) = self._session_state(state)
                task = ReverseTask(
                    task_id=state["task_id"],
                    objective=state["objective"],
                    workspace=workspace,
                    target_id=state.get("target_id", ""),
                    device_id=state.get("device_id", ""),
                    binary_path=state.get("binary_path", ""),
                    username=state.get("username", ""),
                    password=state.get("password", ""),
                    enable_password=state.get("enable_password", ""),
                    status=state["status"],
                    summary=state.get("summary", ""),
                    execution_backend="codex",
                    session_id=session_id,
                    session_initialized=session_initialized,
                    legacy_session_id=legacy_session_id,
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"invalid Reverser state: {state_path}") from exc
            if task.task_id != state_path.parent.name:
                raise RuntimeError(f"task ID does not match workspace: {state_path}")
            if task.status in {
                "waiting_for_device",
                "running",
                "reviewing",
                "cleaning_up",
            }:
                task.status = "paused"
                migrated = True
            if migrated:
                self.persist_task(task)
            tasks[task.task_id] = task
        self.write_index(tasks)
        return tasks

    def persist_task(self, task: ReverseTask) -> None:
        state = {
            "schema_version": self.SCHEMA_VERSION,
            "task_id": task.task_id,
            "objective": task.objective,
            "target_id": task.target_id,
            "device_id": task.device_id,
            "binary_path": task.binary_path,
            "username": task.username,
            "password": task.password,
            "enable_password": task.enable_password,
            "execution_backend": task.execution_backend,
            "session_id": task.session_id,
            "session_initialized": task.session_initialized,
            "status": task.status,
            "summary": task.summary,
        }
        if task.legacy_session_id:
            state["legacy_session_id"] = task.legacy_session_id
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

    @classmethod
    def _session_state(
        cls,
        state: Mapping[str, object],
    ) -> tuple[str | None, bool, str | None, bool]:
        version = state.get("schema_version", 1)
        if not isinstance(version, int) or version > cls.SCHEMA_VERSION:
            raise ValueError("unsupported Reverser state schema")
        if "conversation_id" in state:
            legacy = state["conversation_id"]
            if not isinstance(legacy, str) or not legacy:
                raise ValueError("invalid legacy Conversation ID")
            return None, False, legacy, True
        backend = state.get("execution_backend", "codex")
        if backend != "codex":
            raise ValueError(f"unsupported execution backend: {backend}")
        session_id = state.get("session_id")
        initialized = state.get("session_initialized")
        legacy = state.get("legacy_session_id")
        for value in (session_id, legacy):
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError("invalid execution session ID")
        if initialized is None and version < 3:
            initialized = session_id is not None
        if not isinstance(initialized, bool):
            raise TypeError("session_initialized must be a boolean")
        if initialized and session_id is None:
            raise ValueError("initialized session requires a session ID")
        return session_id, initialized, legacy, version != cls.SCHEMA_VERSION

    @staticmethod
    def _prepare_layout(workspace: Path, migrate_legacy: bool = False) -> None:
        work = workspace / "work"
        if work.is_symlink():
            raise RuntimeError("task work directory must not be a symlink")
        work.mkdir(exist_ok=True)
        if migrate_legacy:
            source = workspace / "evidence"
            target = work / "evidence"
            if source.exists():
                if source.is_symlink() or not source.is_dir():
                    raise RuntimeError("legacy evidence must be a directory")
                if target.exists():
                    if target.is_symlink() or not target.is_dir():
                        raise RuntimeError("Codex evidence must be a directory")
                    if any(source.iterdir()):
                        raise RuntimeError(
                            "both legacy and Codex evidence directories exist"
                        )
                    source.rmdir()
                else:
                    source.replace(target)
        for directory in (
            "evidence",
            "analysis",
            "delegated",
        ):
            path = work / directory
            if path.is_symlink():
                raise RuntimeError(f"task {directory} must not be a symlink")
            path.mkdir(mode=0o700, exist_ok=True)
        delegated = workspace / "delegated"
        if delegated.is_symlink():
            raise RuntimeError("delegation metadata must not be a symlink")
        delegated.mkdir(exist_ok=True)
        if migrate_legacy:
            for entry in tuple(delegated.iterdir()):
                if entry.name == "manifest.json":
                    continue
                target = work / "delegated" / entry.name
                if target.exists():
                    raise RuntimeError(f"duplicate delegated workspace: {entry.name}")
                entry.replace(target)
        analysis = workspace / "analysis"
        if analysis.is_symlink():
            raise RuntimeError("task result directory must not be a symlink")
        analysis.mkdir(exist_ok=True)

    @staticmethod
    def _validate_task_state(state: object) -> None:
        if not isinstance(state, Mapping):
            raise TypeError("task state must be an object")
        for name in ("task_id", "objective", "status"):
            if not isinstance(state.get(name), str):
                raise TypeError(f"task state '{name}' must be a string")
        for name in (
            "target_id",
            "device_id",
            "binary_path",
            "username",
            "password",
            "enable_password",
        ):
            if not isinstance(state.get(name, ""), str):
                raise TypeError(f"task state '{name}' must be a string")
        if not isinstance(state.get("summary", ""), str):
            raise TypeError("task state 'summary' must be a string")
        statuses = {
            "created",
            "waiting_for_device",
            "running",
            "reviewing",
            "cleaning_up",
            "paused",
            "blocked",
            "ready_to_deliver",
            "delivered",
        }
        if state["status"] not in statuses:
            raise ValueError(f"invalid task status: {state['status']}")

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
