import json
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .models import ReverseTask
from .workspace import ReverserWorkspace


@dataclass
class NestedAgentManifest:
    child_id: str
    parent_task_id: str
    generation: int
    workspace: str
    status: str
    metadata: dict[str, Any]


class DelegationRegistry:
    """Persist nested identities while AgentSystem owns their control routing."""

    def __init__(
        self,
        register_child: Callable[[str, Callable[[Any], None]], bool] | None,
        unregister_child: Callable[[str], None] | None,
    ) -> None:
        self._register_child = register_child
        self._unregister_child = unregister_child
        self._lock = threading.RLock()
        self._manifests: dict[str, NestedAgentManifest] = {}

    def register(
        self,
        task: ReverseTask,
        child_name: str,
        interrupt: Callable[[Any], None],
        metadata: Mapping[str, Any] | None = None,
    ) -> NestedAgentManifest:
        if Path(child_name).name != child_name or not child_name:
            raise ValueError("nested Agent name must be a safe path component")
        if self._register_child is None:
            raise RuntimeError("nested Agent registration is not connected")
        encoded_metadata = json.dumps(dict(metadata or {}), ensure_ascii=False)
        child_root = task.work_dir / "delegated" / child_name
        if child_root.is_symlink() or not child_root.resolve().is_relative_to(
            task.work_dir.resolve()
        ):
            raise RuntimeError("nested workspace must remain under task workspace")
        with self._lock:
            prefix = f"{task.task_id}.{child_name}."
            previous_generations = [
                item.generation
                for item in self._manifests.values()
                if item.child_id.startswith(prefix)
            ]
            generation = max(previous_generations, default=0) + 1
            while True:
                child_id = f"{task.task_id}.{child_name}.{generation}"
                workspace = child_root / str(generation)
                if workspace.exists():
                    generation += 1
                    continue
                if self._register_child(child_id, interrupt):
                    break
                generation += 1
            try:
                workspace.mkdir(parents=True, exist_ok=False)
            except Exception:
                if self._unregister_child is not None:
                    self._unregister_child(child_id)
                raise
            manifest = NestedAgentManifest(
                child_id=child_id,
                parent_task_id=task.task_id,
                generation=generation,
                workspace=str(workspace.relative_to(task.work_dir)),
                status="running",
                metadata=json.loads(encoded_metadata),
            )
            self._manifests[child_id] = manifest
            try:
                self._write(task)
            except Exception:
                self._manifests.pop(child_id, None)
                if self._unregister_child is not None:
                    self._unregister_child(child_id)
                raise
            return manifest

    def finish(self, task: ReverseTask, child_id: str, status: str) -> None:
        with self._lock:
            try:
                manifest = self._manifests[child_id]
            except KeyError:
                raise LookupError(f"unknown nested Agent: {child_id}") from None
            if manifest.parent_task_id != task.task_id:
                raise ValueError("nested Agent does not belong to this task")
            manifest.status = status
            self._write(task)
            if self._unregister_child is not None:
                self._unregister_child(child_id)

    def restore(self, tasks: Iterable[ReverseTask]) -> None:
        """Load manifests for top-level workers to reconstruct on demand."""
        with self._lock:
            for task in tasks:
                for manifest in self.load(task):
                    self._manifests[manifest.child_id] = manifest

    @staticmethod
    def load(task: ReverseTask) -> tuple[NestedAgentManifest, ...]:
        path = task.workspace / "delegated" / "manifest.json"
        if not path.exists():
            return ()
        content = json.loads(path.read_text(encoding="utf-8"))
        return tuple(NestedAgentManifest(**item) for item in content)

    def _write(self, task: ReverseTask) -> None:
        manifests = [
            asdict(item)
            for item in self._manifests.values()
            if item.parent_task_id == task.task_id
        ]
        path = task.workspace / "delegated" / "manifest.json"
        ReverserWorkspace.atomic_write(
            path,
            json.dumps(manifests, ensure_ascii=False, indent=2) + "\n",
        )
