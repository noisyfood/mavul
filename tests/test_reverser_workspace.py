import json
import tempfile
import unittest
from pathlib import Path

from agents.reverser.workspace import ReverserWorkspace


class ReverserWorkspaceTest(unittest.TestCase):
    def test_create_task_separates_model_work_from_host_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = ReverserWorkspace(directory)

            task = workspace.create_task("task-1", "Map parser")

            self.assertEqual(task.work_dir, task.workspace / "work")
            self.assertTrue((task.work_dir / "evidence").is_dir())
            self.assertTrue((task.work_dir / "analysis").is_dir())
            self.assertTrue((task.work_dir / "delegated").is_dir())
            self.assertTrue((task.workspace / "delegated").is_dir())
            self.assertTrue((task.workspace / "analysis").is_dir())
            self.assertFalse((task.workspace / "task-state.json").exists())

    def test_persists_and_loads_opaque_session_id(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = ReverserWorkspace(directory)
            task = workspace.create_task("task-opaque", "Map parser")
            task.session_id = "thread:not-a-uuid/opaque"
            task.status = "paused"
            workspace.persist_task(task)

            restored = ReverserWorkspace(directory).load_tasks()["task-opaque"]

            self.assertEqual(restored.session_id, "thread:not-a-uuid/opaque")
            self.assertFalse(restored.session_initialized)
            self.assertIsNone(restored.legacy_session_id)
            self.assertEqual(restored.execution_backend, "codex")

    def test_migrates_legacy_conversation_state_and_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_root = root / "legacy-task"
            (task_root / "evidence").mkdir(parents=True)
            (task_root / "delegated").mkdir()
            (task_root / "evidence" / "trace.txt").write_text(
                "legacy evidence", encoding="utf-8"
            )
            state_path = task_root / "task-state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "task_id": "legacy-task",
                        "objective": "Map legacy parser",
                        "conversation_id": "2b278455-legacy-openhands-id",
                        "status": "running",
                        "summary": "parser entry found",
                    }
                ),
                encoding="utf-8",
            )

            task = ReverserWorkspace(root).load_tasks()["legacy-task"]
            migrated_state = json.loads(state_path.read_text(encoding="utf-8"))

            self.assertIsNone(task.session_id)
            self.assertEqual(task.legacy_session_id, "2b278455-legacy-openhands-id")
            self.assertEqual(task.status, "paused")
            self.assertTrue((task.work_dir / "evidence" / "trace.txt").is_file())
            self.assertFalse((task_root / "evidence").exists())
            self.assertEqual(migrated_state["schema_version"], 4)
            self.assertEqual(migrated_state["execution_backend"], "codex")
            self.assertNotIn("conversation_id", migrated_state)

    def test_rejects_ambiguous_legacy_and_codex_layouts(self):
        with tempfile.TemporaryDirectory() as directory:
            task_root = Path(directory) / "conflict"
            (task_root / "evidence").mkdir(parents=True)
            (task_root / "evidence" / "old.txt").write_text("old")
            (task_root / "work" / "evidence").mkdir(parents=True)
            (task_root / "task-state.json").write_text(
                json.dumps(
                    {
                        "task_id": "conflict",
                        "objective": "Map parser",
                        "conversation_id": "legacy-id",
                        "status": "paused",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "both legacy and Codex"):
                ReverserWorkspace(directory).load_tasks()

    def test_atomic_write_rejects_symlink_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside.txt"
            outside.write_text("unchanged", encoding="utf-8")
            linked = root / "linked.txt"
            linked.symlink_to(outside)

            with self.assertRaises(RuntimeError):
                ReverserWorkspace.atomic_write(linked, "changed")

            self.assertEqual(outside.read_text(encoding="utf-8"), "unchanged")


if __name__ == "__main__":
    unittest.main()
