import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")

from openhands.sdk import ConversationExecutionStatus

from agents.reverser import Reverser, register
from agents.reverser_sdk import OpenHandsRuntime
from system.envelope import (
    ResultEnvelope,
    SystemAction,
    SystemEnvelope,
    TaskEnvelope,
)


class FakeConversation:
    instances = []
    review_answers = []
    block_worker = False
    run_started = threading.Event()
    release_run = threading.Event()

    def __init__(self, agent, *, workspace, **options):
        self.agent = agent
        self.workspace = Path(workspace)
        self.options = options
        self.messages = []
        self.run_count = 0
        self.closed = False
        self.pause_requested = False
        self.state = SimpleNamespace(
            execution_status=ConversationExecutionStatus.IDLE,
            events=[],
        )
        self.instances.append(self)

    def send_message(self, message, sender):
        self.messages.append((sender, message))

    def run(self):
        self.run_count += 1
        evidence = self.workspace / "evidence" / "trace.txt"
        evidence.write_text("0x401000: input reaches parser\n", encoding="utf-8")
        if self.agent == "worker" and self.block_worker:
            self.run_started.set()
            self.release_run.wait(timeout=5)
        self.state.events.append(object())
        self.state.execution_status = (
            ConversationExecutionStatus.PAUSED
            if self.pause_requested
            else ConversationExecutionStatus.FINISHED
        )

    def ask_agent(self, question):
        if self.agent == "main":
            return self.review_answers.pop(0)
        return "Stopped after tracing the parser; next inspect the bounds check."

    def pause(self):
        self.pause_requested = True
        self.release_run.set()

    def close(self):
        self.closed = True


class ReverserTest(unittest.TestCase):
    def setUp(self):
        FakeConversation.instances = []
        FakeConversation.review_answers = ["APPROVE"]
        FakeConversation.block_worker = False
        FakeConversation.run_started = threading.Event()
        FakeConversation.release_run = threading.Event()
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.config = {
            "workspace": self.temporary_directory.name,
            "max_concurrency": 2,
            "main": {"model": "review-model"},
            "worker": {"model": "reverse-model"},
        }
        self.patches = [
            patch("agents.reverser_sdk.Conversation", FakeConversation),
            patch.object(
                OpenHandsRuntime,
                "build_agent",
                autospec=True,
                side_effect=lambda _self, role, tools: role,
            ),
            patch.object(OpenHandsRuntime, "worker_tools", return_value=[]),
            patch(
                "agents.reverser.get_agent_final_response",
                return_value="Entry 0x401000 receives attacker input; see trace.txt.",
            ),
        ]
        for current_patch in self.patches:
            current_patch.start()
            self.addCleanup(current_patch.stop)

    def test_runs_worker_and_persists_reviewed_result(self):
        reverser = Reverser(self.config)

        result = reverser.start_task("task-1", "Map the packet parser")

        self.assertEqual(result.status, "ready_to_deliver")
        task_root = Path(self.temporary_directory.name) / "task-1"
        self.assertTrue((task_root / "analysis").is_dir())
        self.assertTrue((task_root / "task-state.json").is_file())
        behavior_model = task_root / "analysis" / "behavior-model.md"
        self.assertIn("trace.txt", behavior_model.read_text(encoding="utf-8"))
        self.assertIn("ready_to_deliver", (task_root.parent / "index.md").read_text())
        self.assertEqual(FakeConversation.instances[1].run_count, 1)

    def test_rejected_result_continues_same_conversation(self):
        FakeConversation.review_answers = [
            "REVISE\nExplain the bounds check.",
            "APPROVE",
        ]
        reverser = Reverser(self.config)

        result = reverser.start_task("task-2", "Map the request handler")

        worker = FakeConversation.instances[1]
        self.assertEqual(result.status, "ready_to_deliver")
        self.assertEqual(worker.run_count, 2)
        self.assertIn("Explain the bounds check.", worker.messages[-1][1])

    def test_missing_evidence_reference_is_rejected_before_llm_review(self):
        reverser = Reverser(self.config)
        outputs = [
            "A behavior model without a citation.",
            "The input reaches the parser; see trace.txt.",
        ]
        with patch(
            "agents.reverser.get_agent_final_response",
            side_effect=outputs,
        ):
            result = reverser.start_task("task-evidence", "Trace input flow")

        self.assertEqual(result.status, "ready_to_deliver")
        self.assertEqual(FakeConversation.instances[1].run_count, 2)

    def test_worker_can_report_external_blocker(self):
        reverser = Reverser(self.config)
        with patch(
            "agents.reverser.get_agent_final_response",
            return_value="BLOCKED: IDA MCP is unavailable",
        ):
            result = reverser.start_task("task-blocked", "Inspect the dispatcher")

        self.assertEqual(result.status, "blocked")
        self.assertEqual(reverser._tasks["task-blocked"].status, "blocked")
        self.assertIn("IDA MCP is unavailable", reverser._tasks["task-blocked"].summary)

    def test_interrupt_and_restore_reuse_conversation_id(self):
        reverser = Reverser(self.config)
        FakeConversation.block_worker = True
        outcome = {}
        runner = threading.Thread(
            target=lambda: outcome.setdefault(
                "result",
                reverser.start_task("task-3", "Map authentication"),
            )
        )
        runner.start()
        self.assertTrue(FakeConversation.run_started.wait(timeout=2))
        conversation_id = reverser._tasks["task-3"].conversation_id

        summary = reverser.interrupt_task("task-3")
        runner.join(timeout=2)
        reverser.stop()
        restored = Reverser(self.config)

        self.assertFalse(runner.is_alive())
        self.assertEqual(outcome["result"].status, "paused")
        self.assertIn("next inspect", summary)
        self.assertEqual(restored._tasks["task-3"].status, "paused")
        FakeConversation.block_worker = False
        FakeConversation.review_answers = ["APPROVE"]
        result = restored.resume_task("task-3", "Continue from the saved trace")
        self.assertEqual(result.status, "ready_to_deliver")
        self.assertEqual(
            FakeConversation.instances[-1].options["conversation_id"],
            conversation_id,
        )

    def test_rejects_task_id_that_can_escape_workspace(self):
        reverser = Reverser(self.config)

        with self.assertRaises(ValueError):
            reverser.start_task("../outside", "Inspect the parser")

    def test_rejects_symlinked_task_workspace(self):
        reverser = Reverser(self.config)
        with tempfile.TemporaryDirectory() as outside:
            (Path(self.temporary_directory.name) / "linked").symlink_to(
                outside,
                target_is_directory=True,
            )
            with self.assertRaises(ValueError):
                reverser.start_task("linked", "Inspect the parser")

    def test_register_exposes_only_main_lifecycle(self):
        submitted = []
        system = SimpleNamespace(
            submit=submitted.append,
            register_child_agent=lambda *_args: True,
            unregister_child_agent=lambda *_args: None,
            register_agent=lambda *args: setattr(system, "call", args),
        )
        config = {"agents": {"reverser": self.config}}

        reverser = register(system, config)

        name, receiver, accepted, concurrency, stop = system.call
        self.assertEqual(name, "reverser")
        self.assertEqual(accepted, (TaskEnvelope, SystemEnvelope))
        self.assertEqual(concurrency, 2)
        self.assertEqual(receiver, reverser.receive)
        self.assertEqual(stop, reverser.stop)

    def test_mailbox_task_emits_result_and_marks_delivery(self):
        submitted = []
        reverser = Reverser(self.config, submit=submitted.append)
        envelope = TaskEnvelope(
            sender="orchestrator",
            recipient="reverser",
            task_id="task-mailbox",
            content="Map the packet parser",
        )

        reverser.receive(envelope)

        self.assertEqual(len(submitted), 1)
        self.assertIsInstance(submitted[0], ResultEnvelope)
        self.assertEqual(submitted[0].correlation_id, envelope.envelope_id)
        self.assertEqual(submitted[0].status, "ready_to_deliver")
        self.assertEqual(reverser._tasks["task-mailbox"].status, "delivered")

    def test_mailbox_query_returns_correlated_status(self):
        submitted = []
        reverser = Reverser(self.config, submit=submitted.append)
        reverser.start_task("task-query", "Map the parser")
        query = SystemEnvelope(
            sender="orchestrator",
            recipient="reverser",
            task_id="task-query",
            event="query status",
            action=SystemAction.QUERY_TASK,
        )

        reverser.receive(query)

        self.assertEqual(submitted[0].status, "status")
        self.assertEqual(submitted[0].correlation_id, query.envelope_id)

    def test_mailbox_rejects_non_orchestrator_task(self):
        reverser = Reverser(self.config)
        envelope = TaskEnvelope(
            sender="hunter",
            recipient="reverser",
            task_id="unauthorized",
            content="Run this task",
        )

        with self.assertRaises(PermissionError):
            reverser.receive(envelope)


if __name__ == "__main__":
    unittest.main()
