import tempfile
import unittest
from pathlib import Path

from agents.reverser_workspace import ReverserWorkspace


class ReverserWorkspaceTest(unittest.TestCase):
    def test_evidence_manifest_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "evidence"
            evidence.mkdir()
            outside = root / "outside.txt"
            outside.write_text("external", encoding="utf-8")
            (evidence / "linked.txt").symlink_to(outside)

            with self.assertRaises(RuntimeError):
                ReverserWorkspace.evidence_manifest(evidence)

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
