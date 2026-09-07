import tempfile
import unittest
from pathlib import Path

from agents.reverser.delegation import DelegationRegistry
from agents.reverser.models import ReverseTask


class DelegationRegistryTest(unittest.TestCase):
    def test_registers_unique_nested_id_and_persists_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            (workspace / "work" / "delegated").mkdir(parents=True)
            (workspace / "delegated").mkdir()
            task = ReverseTask(
                task_id="task-1",
                objective="Map parser",
                workspace=workspace,
                session_id="thread-opaque-1",
            )
            registered = []
            unregistered = []

            def register(agent_id, _interrupt):
                registered.append(agent_id)
                return not agent_id.endswith(".1")

            registry = DelegationRegistry(register, unregistered.append)
            manifest = registry.register(
                task,
                "callee",
                lambda _envelope: None,
                {"model": "reverse-model"},
            )
            registry.finish(task, manifest.child_id, "complete")
            loaded = registry.load(task)

        self.assertEqual(registered, ["task-1.callee.1", "task-1.callee.2"])
        self.assertEqual(manifest.child_id, "task-1.callee.2")
        self.assertEqual(manifest.workspace, "delegated/callee/2")
        self.assertEqual(loaded[0].status, "complete")
        self.assertEqual(unregistered, [manifest.child_id])


if __name__ == "__main__":
    unittest.main()
