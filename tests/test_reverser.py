import json
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

from agents.reverser import Reverser
from agents.reverser.execution import (
    ExecutionError,
    ExecutionResult,
    ExecutionStatus,
)
from system.agent_runtime import AgentRuntime
from system.device import DeviceEndpoint, DeviceLease, LeaseRequest, LeaseStatus
from system.envelope import (
    ResultEnvelope,
    SystemAction,
    SystemEnvelope,
    TaskEnvelope,
)


class FakeSession:
    def __init__(self, backend, session_id, workspace, *, reviewer=False):
        self.backend = backend
        self._session_id = session_id
        self.workspace = Path(workspace)
        self.reviewer = reviewer
        self.prompts = []
        self.schemas = []
        self.run_count = 0
        self.interrupted = False
        self.closed = False

    @property
    def session_id(self):
        return self._session_id

    def run(self, prompt, output_schema=None, on_started=None):
        self.prompts.append(prompt)
        self.schemas.append(output_schema)
        self.run_count += 1
        if on_started is not None:
            on_started()
        if self.reviewer:
            output = self.backend.review_outputs.pop(0)
            return ExecutionResult(ExecutionStatus.COMPLETED, output)
        if "CLEANUP_COMPLETE" in prompt:
            if self.backend.block_cleanup:
                self.backend.cleanup_started.set()
                self.backend.release_cleanup.wait(timeout=5)
                if self.interrupted:
                    self.interrupted = False
                    return ExecutionResult(ExecutionStatus.INTERRUPTED, "")
            output = self.backend.cleanup_outputs.pop(0)
            return ExecutionResult(ExecutionStatus.COMPLETED, output)
        if self.backend.mark_dirty:
            (self.workspace / "device-changes-pending").write_text(
                "pending\n",
                encoding="utf-8",
            )
        if self.backend.worker_error is not None:
            raise self.backend.worker_error
        evidence = self.workspace / "evidence" / "trace.txt"
        evidence.write_text("0x401000: input reaches parser\n", encoding="utf-8")
        (self.workspace / "evidence" / "device-operations.md").write_text(
            "telnet 192.0.2.10 23\nshow version\nIOS XE output\n",
            encoding="utf-8",
        )
        if self.backend.block_worker:
            self.backend.run_started.set()
            self.backend.release_run.wait(timeout=5)
            if self.interrupted:
                return ExecutionResult(ExecutionStatus.INTERRUPTED, "")
        return ExecutionResult(
            ExecutionStatus.COMPLETED,
            self.backend.worker_outputs.pop(0),
        )

    def query(self, _prompt):
        return self.backend.progress

    def interrupt(self):
        self.interrupted = True
        self.backend.release_run.set()
        self.backend.release_cleanup.set()

    def close(self):
        self.closed = True


class FakeBackend:
    name = "codex"

    def __init__(
        self,
        worker_outputs=None,
        review_outputs=None,
        cleanup_outputs=None,
    ):
        self.worker_outputs = list(
            worker_outputs
            or [
                "Entry 0x401000 receives attacker input; see "
                "evidence/trace.txt and evidence/device-operations.md."
            ]
        )
        self.review_outputs = list(
            review_outputs
            or [json.dumps({"verdict": "approve", "feedback": ""})]
        )
        self.cleanup_outputs = list(cleanup_outputs or ["CLEANUP_COMPLETE"])
        self.worker_error = None
        self.mark_dirty = False
        self.block_worker = False
        self.block_cleanup = False
        self.progress = "Stopped after tracing the parser; next inspect bounds."
        self.run_started = threading.Event()
        self.release_run = threading.Event()
        self.cleanup_started = threading.Event()
        self.release_cleanup = threading.Event()
        self.worker_sessions = []
        self.reviewer_sessions = []
        self.open_calls = []
        self.close_count = 0

    def open_worker(self, workspace, session_id):
        self.open_calls.append((Path(workspace), session_id))
        session_id = session_id or f"thread-{len(self.worker_sessions) + 1}"
        session = FakeSession(self, session_id, workspace)
        self.worker_sessions.append(session)
        return session

    def open_reviewer(self, workspace):
        session = FakeSession(
            self,
            f"review-{len(self.reviewer_sessions) + 1}",
            workspace,
            reviewer=True,
        )
        self.reviewer_sessions.append(session)
        return session

    def close(self):
        self.close_count += 1


class FakeDevice:
    def __init__(self):
        self.endpoint = DeviceEndpoint(
            device_id="router",
            name="Lab router",
            ip_address="192.0.2.10",
            telnet_port=23,
        )
        self.lease = None


class FakeDevices:
    def __init__(self, *, waiting=False):
        self.device = FakeDevice()
        self.waiting = waiting
        self.acquired = []
        self.released = []
        self.cancelled = []

    def get(self, device_id):
        if device_id != "router":
            raise LookupError(f"unknown device: {device_id}")
        return self.device

    def acquire(self, device_id, agent_id, task_id):
        self.get(device_id)
        self.acquired.append((agent_id, task_id))
        if self.waiting:
            return LeaseRequest(LeaseStatus.WAITING)
        if self.device.lease is None:
            self.device.lease = DeviceLease(
                uuid.uuid4(),
                device_id,
                agent_id,
                task_id,
                1,
            )
        return LeaseRequest(LeaseStatus.GRANTED, self.device.lease)

    def release(self, lease):
        if self.device.lease != lease:
            raise RuntimeError("device lease is not current")
        self.released.append(lease)
        self.device.lease = None

    def cancel_waiter(self, device_id, agent_id, task_id):
        self.get(device_id)
        self.cancelled.append((agent_id, task_id))
        return self.waiting


def task_information():
    return {
        "target_id": "ios-xe",
        "device_id": "router",
        "binary_path": "/opt/ida/httpd",
        "username": "admin",
        "password": "login-pass",
        "enable_password": "enable-pass",
    }


class ReverserTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.config = {
            "workspace": self.temporary_directory.name,
            "max_concurrency": 2,
            "main": {"model": "review-model"},
            "worker": {"model": "reverse-model"},
        }
        self.devices = FakeDevices()

    def build_reverser(self, backend, **kwargs):
        return Reverser(
            self.config,
            self.devices,
            execution_backend=backend,
            **kwargs,
        )

    def test_runs_worker_and_persists_reviewed_result(self):
        backend = FakeBackend()
        reverser = self.build_reverser(backend)

        result = reverser.start_task(
            "task-1",
            "Map the packet parser",
            task_information(),
        )

        self.assertEqual(result.status, "ready_to_deliver")
        task_root = Path(self.temporary_directory.name) / "task-1"
        state = json.loads((task_root / "task-state.json").read_text())
        self.assertEqual(state["session_id"], "thread-1")
        self.assertTrue(state["session_initialized"])
        self.assertEqual(state["target_id"], "ios-xe")
        self.assertEqual(state["device_id"], "router")
        self.assertEqual(state["binary_path"], "/opt/ida/httpd")
        self.assertNotIn("hash", json.dumps(state).lower())
        self.assertTrue((task_root / "work" / "evidence" / "trace.txt").is_file())
        behavior_model = task_root / "analysis" / "behavior-model.md"
        self.assertIn("trace.txt", behavior_model.read_text(encoding="utf-8"))
        self.assertEqual(backend.worker_sessions[0].run_count, 2)
        self.assertEqual(backend.reviewer_sessions[0].run_count, 1)
        self.assertEqual(len(self.devices.released), 1)
        prompt = backend.worker_sessions[0].prompts[0]
        self.assertIn("/usr/bin/telnet", prompt)
        self.assertIn("login-pass", prompt)
        self.assertIn("/opt/ida/httpd", prompt)

    def test_waiting_for_device_does_not_open_worker(self):
        backend = FakeBackend()
        self.devices = FakeDevices(waiting=True)
        reverser = self.build_reverser(backend)

        waiting = reverser.start_task(
            "task-waiting",
            "Map parser",
            task_information(),
        )

        self.assertEqual(waiting.status, "waiting_for_device")
        self.assertEqual(backend.worker_sessions, [])

        self.devices.waiting = False
        completed = reverser.device_available("task-waiting")

        self.assertEqual(completed.status, "ready_to_deliver")
        self.assertEqual(len(backend.worker_sessions), 1)

    def test_interrupt_before_start_pauses_without_acquiring_device(self):
        backend = FakeBackend()
        reverser = self.build_reverser(backend)

        reverser.interrupt_task("task-not-started")
        result = reverser.start_task(
            "task-not-started",
            "Map parser",
            task_information(),
        )

        self.assertEqual(result.status, "paused")
        self.assertEqual(self.devices.acquired, [])
        self.assertIsNone(self.devices.device.lease)
        self.assertEqual(backend.worker_sessions, [])

    def test_interrupt_during_device_handoff_releases_granted_lease(self):
        class BlockingGrantDevices(FakeDevices):
            def __init__(self):
                super().__init__(waiting=True)
                self.grant_started = threading.Event()
                self.release_grant = threading.Event()

            def acquire(self, device_id, agent_id, task_id):
                request = super().acquire(device_id, agent_id, task_id)
                if request.status == LeaseStatus.GRANTED:
                    self.grant_started.set()
                    self.release_grant.wait(timeout=2)
                return request

        backend = FakeBackend()
        self.devices = BlockingGrantDevices()
        reverser = self.build_reverser(backend)
        reverser.start_task(
            "task-handoff",
            "Map parser",
            task_information(),
        )
        self.devices.waiting = False
        results = {}
        available = threading.Thread(
            target=lambda: results.setdefault(
                "available",
                reverser.device_available("task-handoff"),
            )
        )
        interrupted = threading.Thread(
            target=lambda: results.setdefault(
                "interrupt",
                reverser.interrupt_task("task-handoff"),
            )
        )

        available.start()
        self.assertTrue(self.devices.grant_started.wait(timeout=1))
        interrupted.start()
        self.assertTrue(
            reverser._tasks["task-handoff"].interrupt_requested.wait(timeout=1)
        )
        self.devices.release_grant.set()
        available.join(timeout=2)
        interrupted.join(timeout=2)

        self.assertFalse(available.is_alive())
        self.assertFalse(interrupted.is_alive())
        self.assertIn("interrupt", results)
        self.assertEqual(results["available"].status, "paused")
        self.assertEqual(reverser._tasks["task-handoff"].status, "paused")
        self.assertIsNone(reverser._tasks["task-handoff"].device_lease)
        self.assertIsNone(self.devices.device.lease)
        self.assertEqual(len(self.devices.released), 1)
        self.assertEqual(backend.worker_sessions, [])

    def test_interrupt_during_initial_wait_handles_late_device_notification(self):
        class BlockingWaitDevices(FakeDevices):
            def __init__(self):
                super().__init__(waiting=True)
                self.wait_started = threading.Event()
                self.release_wait = threading.Event()

            def acquire(self, device_id, agent_id, task_id):
                request = super().acquire(device_id, agent_id, task_id)
                self.wait_started.set()
                self.release_wait.wait(timeout=2)
                return request

        backend = FakeBackend()
        self.devices = BlockingWaitDevices()
        reverser = self.build_reverser(backend)
        results = {}
        starter = threading.Thread(
            target=lambda: results.setdefault(
                "start",
                reverser.start_task(
                    "task-initial-wait",
                    "Map parser",
                    task_information(),
                ),
            )
        )

        starter.start()
        self.assertTrue(self.devices.wait_started.wait(timeout=1))
        reverser.interrupt_task("task-initial-wait")
        self.devices.release_wait.set()
        starter.join(timeout=2)

        self.assertFalse(starter.is_alive())
        self.assertEqual(results["start"].status, "paused")
        self.assertTrue(
            reverser._tasks["task-initial-wait"].cancelled_while_waiting
        )
        lease = DeviceLease(
            uuid.uuid4(),
            "router",
            "reverser",
            "task-initial-wait",
            2,
        )
        self.devices.device.lease = lease
        late = reverser.device_available("task-initial-wait")

        self.assertEqual(late.status, "paused")
        self.assertEqual(self.devices.released, [lease])
        self.assertIsNone(reverser._tasks["task-initial-wait"].device_lease)
        self.assertEqual(backend.worker_sessions, [])

    def test_concurrent_interrupt_is_not_cleared_by_resume(self):
        class BlockingAcquireDevices(FakeDevices):
            def __init__(self):
                super().__init__()
                self.acquire_started = threading.Event()
                self.release_acquire = threading.Event()

            def acquire(self, device_id, agent_id, task_id):
                self.acquire_started.set()
                self.release_acquire.wait(timeout=2)
                return super().acquire(device_id, agent_id, task_id)

        backend = FakeBackend()
        self.devices = BlockingAcquireDevices()
        reverser = self.build_reverser(backend)
        reverser.interrupt_task("task-resume-race")
        reverser.start_task(
            "task-resume-race",
            "Map parser",
            task_information(),
        )
        lookup_started = threading.Event()
        release_lookup = threading.Event()
        original_get_task = reverser._get_task

        def gated_get_task(task_id):
            task = original_get_task(task_id)
            lookup_started.set()
            release_lookup.wait(timeout=2)
            return task

        reverser._get_task = gated_get_task
        results = {}
        resume = threading.Thread(
            target=lambda: results.setdefault(
                "resume",
                reverser.resume_task("task-resume-race", "Continue"),
            )
        )
        interrupt_done = threading.Event()

        def interrupt():
            results["interrupt"] = reverser.interrupt_task("task-resume-race")
            interrupt_done.set()

        interrupted = threading.Thread(target=interrupt)
        resume.start()
        self.assertTrue(lookup_started.wait(timeout=1))
        interrupted.start()
        release_lookup.set()
        self.assertTrue(self.devices.acquire_started.wait(timeout=1))
        self.assertTrue(interrupt_done.wait(timeout=1))
        self.devices.release_acquire.set()
        resume.join(timeout=2)
        interrupted.join(timeout=2)

        self.assertFalse(resume.is_alive())
        self.assertFalse(interrupted.is_alive())
        self.assertEqual(results["resume"].status, "paused")
        self.assertEqual(backend.worker_sessions, [])

    def test_queued_resume_cannot_override_later_runtime_interrupt(self):
        backend = FakeBackend()
        submitted = []
        reverser = Reverser(
            self.config,
            self.devices,
            submit=submitted.append,
            execution_backend=backend,
        )
        reverser.interrupt_task("task-runtime-order")
        reverser.start_task(
            "task-runtime-order",
            "Map parser",
            task_information(),
        )
        resume_started = threading.Event()
        release_resume = threading.Event()
        resume_finished = threading.Event()
        interrupt_finished = threading.Event()
        errors = []

        def receive(envelope):
            if envelope.action == SystemAction.RESUME_TASK:
                resume_started.set()
                release_resume.wait(timeout=2)
            reverser.receive(envelope)
            if envelope.action == SystemAction.RESUME_TASK:
                resume_finished.set()
            elif envelope.action == SystemAction.INTERRUPT_TASK:
                interrupt_finished.set()

        runtime = AgentRuntime(
            "reverser",
            receive,
            (SystemEnvelope,),
            1,
            reverser.stop,
            lambda *_args: errors.append(_args),
        )
        self.addCleanup(runtime.shutdown)
        self.addCleanup(release_resume.set)
        runtime.submit(
            SystemEnvelope(
                sender="orchestrator",
                recipient="reverser",
                task_id="task-runtime-order",
                event="resume",
                action=SystemAction.RESUME_TASK,
                arguments={"notes": "Continue"},
            )
        )
        self.assertTrue(resume_started.wait(timeout=1))
        runtime.submit(
            SystemEnvelope(
                sender="orchestrator",
                recipient="reverser",
                task_id="task-runtime-order",
                event="interrupt",
                action=SystemAction.INTERRUPT_TASK,
            )
        )

        self.assertTrue(interrupt_finished.wait(timeout=1))
        release_resume.set()
        self.assertTrue(resume_finished.wait(timeout=1))

        self.assertEqual(reverser._tasks["task-runtime-order"].status, "paused")
        self.assertEqual(backend.worker_sessions, [])
        self.assertEqual(errors, [])

    def test_cleanup_failure_blocks_and_keeps_device_lease(self):
        backend = FakeBackend(cleanup_outputs=["unable to undo test route"])
        reverser = self.build_reverser(backend)

        result = reverser.start_task(
            "task-cleanup-failed",
            "Map parser",
            task_information(),
        )

        self.assertEqual(result.status, "blocked")
        self.assertIsNotNone(self.devices.device.lease)
        self.assertEqual(self.devices.released, [])

    def test_interrupt_cleanup_keeps_lease_and_resume_only_retries_cleanup(self):
        backend = FakeBackend()
        backend.block_cleanup = True
        reverser = self.build_reverser(backend)
        outcome = {}
        runner = threading.Thread(
            target=lambda: outcome.setdefault(
                "run",
                reverser.start_task(
                    "task-cleanup-interrupt",
                    "Map parser",
                    task_information(),
                ),
            )
        )
        runner.start()
        self.assertTrue(backend.cleanup_started.wait(timeout=2))

        reverser.interrupt_task("task-cleanup-interrupt")
        runner.join(timeout=2)

        self.assertEqual(outcome["run"].status, "paused")
        self.assertIsNotNone(self.devices.device.lease)
        self.assertEqual(len(backend.reviewer_sessions), 1)

        backend.block_cleanup = False
        resumed = reverser.resume_task(
            "task-cleanup-interrupt",
            "Finish cleanup",
        )

        self.assertEqual(resumed.status, "ready_to_deliver")
        self.assertEqual(len(backend.worker_sessions), 1)
        self.assertEqual(len(backend.reviewer_sessions), 1)
        self.assertEqual(self.devices.device.lease, None)

    def test_cleanup_resume_after_device_wait_does_not_repeat_analysis(self):
        first_backend = FakeBackend(cleanup_outputs=["cleanup failed"])
        reverser = self.build_reverser(first_backend)
        blocked = reverser.start_task(
            "task-cleanup-wait",
            "Map parser",
            task_information(),
        )
        self.assertEqual(blocked.status, "blocked")
        behavior_model = (
            reverser._tasks["task-cleanup-wait"].workspace
            / "analysis"
            / "behavior-model.md"
        )
        saved_model = behavior_model.read_text(encoding="utf-8")
        reverser.stop()

        restored_backend = FakeBackend()
        self.devices = FakeDevices(waiting=True)
        restored = self.build_reverser(restored_backend)
        waiting = restored.resume_task("task-cleanup-wait", "Retry cleanup")

        self.assertEqual(waiting.status, "waiting_for_device")
        self.assertEqual(restored_backend.worker_sessions, [])
        self.devices.waiting = False
        completed = restored.device_available("task-cleanup-wait")

        self.assertEqual(completed.status, "ready_to_deliver")
        self.assertEqual(len(restored_backend.worker_sessions), 1)
        self.assertEqual(restored_backend.worker_sessions[0].run_count, 1)
        self.assertIn(
            "CLEANUP_COMPLETE",
            restored_backend.worker_sessions[0].prompts[0],
        )
        self.assertEqual(restored_backend.reviewer_sessions, [])
        self.assertEqual(behavior_model.read_text(encoding="utf-8"), saved_model)
        self.assertIsNone(self.devices.device.lease)

    def test_late_device_notification_releases_cancelled_wait(self):
        backend = FakeBackend()
        self.devices = FakeDevices(waiting=True)
        reverser = self.build_reverser(backend)
        reverser.start_task(
            "task-cancelled-wait",
            "Map parser",
            task_information(),
        )
        reverser.interrupt_task("task-cancelled-wait")
        lease = DeviceLease(
            uuid.uuid4(),
            "router",
            "reverser",
            "task-cancelled-wait",
            2,
        )
        self.devices.device.lease = lease

        result = reverser.device_available("task-cancelled-wait")

        self.assertEqual(result.status, "paused")
        self.assertEqual(self.devices.released, [lease])
        self.assertIsNone(reverser._tasks["task-cancelled-wait"].device_lease)
        self.assertEqual(backend.worker_sessions, [])

    def test_rejected_result_continues_same_session(self):
        backend = FakeBackend(
            worker_outputs=[
                "Initial model cites evidence/trace.txt.",
                "Revised bounds-check model cites evidence/trace.txt.",
            ],
            review_outputs=[
                json.dumps({"verdict": "revise", "feedback": "Explain bounds."}),
                json.dumps({"verdict": "approve", "feedback": ""}),
            ],
        )
        reverser = self.build_reverser(backend)

        result = reverser.start_task(
            "task-2",
            "Map the request handler",
            task_information(),
        )

        self.assertEqual(result.status, "ready_to_deliver")
        self.assertEqual(len(backend.worker_sessions), 1)
        self.assertEqual(backend.worker_sessions[0].run_count, 3)
        self.assertIn("Explain bounds.", backend.worker_sessions[0].prompts[1])

    def test_missing_evidence_reference_is_rejected_before_model_review(self):
        backend = FakeBackend(
            worker_outputs=[
                "A behavior model without a citation.",
                "The input reaches the parser; see evidence/trace.txt.",
            ]
        )
        reverser = self.build_reverser(backend)

        result = reverser.start_task(
            "task-evidence",
            "Trace input flow",
            task_information(),
        )

        self.assertEqual(result.status, "ready_to_deliver")
        self.assertEqual(backend.worker_sessions[0].run_count, 3)
        self.assertEqual(sum(item.run_count for item in backend.reviewer_sessions), 1)

    def test_worker_can_report_external_blocker(self):
        backend = FakeBackend(
            worker_outputs=[
                "BLOCKED: IDA MCP is unavailable; see evidence/trace.txt."
            ],
            review_outputs=[
                json.dumps(
                    {
                        "verdict": "blocked",
                        "feedback": "IDA MCP is unavailable",
                    }
                )
            ],
        )
        reverser = self.build_reverser(backend)

        result = reverser.start_task(
            "task-blocked",
            "Inspect dispatcher",
            task_information(),
        )

        self.assertEqual(result.status, "blocked")
        self.assertIn("IDA MCP is unavailable", reverser._tasks[result.task_id].summary)
        self.assertEqual(backend.reviewer_sessions[0].run_count, 1)

    def test_interrupt_and_restore_reuse_opaque_session_id(self):
        backend = FakeBackend()
        backend.block_worker = True
        reverser = self.build_reverser(backend)
        outcome = {}
        runner = threading.Thread(
            target=lambda: outcome.setdefault(
                "result",
                reverser.start_task(
                    "task-3",
                    "Map authentication",
                    task_information(),
                ),
            )
        )
        runner.start()
        self.assertTrue(backend.run_started.wait(timeout=2))

        summary = reverser.interrupt_task("task-3")
        runner.join(timeout=2)
        reverser.stop()

        restored_backend = FakeBackend()
        restored = Reverser(
            self.config,
            FakeDevices(),
            execution_backend=restored_backend,
        )
        result = restored.resume_task("task-3", "Continue from saved trace")

        self.assertFalse(runner.is_alive())
        self.assertEqual(outcome["result"].status, "paused")
        self.assertIn("reconstruct from", summary)
        self.assertEqual(result.status, "ready_to_deliver")
        self.assertEqual(restored_backend.open_calls[0][1], "thread-1")

    def test_runtime_failure_is_persisted_and_propagated(self):
        backend = FakeBackend()
        backend.worker_error = ExecutionError("app-server disconnected")
        reverser = self.build_reverser(backend)

        result = reverser.start_task(
            "task-runtime",
            "Map parser",
            task_information(),
        )

        task = reverser._tasks["task-runtime"]
        self.assertEqual(result.status, "blocked")
        self.assertEqual(task.status, "blocked")
        self.assertIn("disconnected", (task.workspace / "progress.md").read_text())
        self.assertEqual(len(self.devices.released), 1)

    def test_runtime_failure_after_device_change_keeps_lease(self):
        backend = FakeBackend()
        backend.mark_dirty = True
        backend.worker_error = ExecutionError("app-server disconnected")
        reverser = self.build_reverser(backend)

        result = reverser.start_task(
            "task-dirty-runtime",
            "Map parser",
            task_information(),
        )

        self.assertEqual(result.status, "blocked")
        self.assertTrue(
            reverser._tasks[result.task_id].device_changes_pending
        )
        self.assertIsNotNone(self.devices.device.lease)
        self.assertEqual(self.devices.released, [])

    def test_rejects_unsafe_task_workspaces(self):
        reverser = self.build_reverser(FakeBackend())
        with self.assertRaises(ValueError):
            reverser.start_task(
                "../outside",
                "Inspect parser",
                task_information(),
            )
        with tempfile.TemporaryDirectory() as outside:
            (Path(self.temporary_directory.name) / "linked").symlink_to(
                outside, target_is_directory=True
            )
            with self.assertRaises(ValueError):
                reverser.start_task(
                    "linked",
                    "Inspect parser",
                    task_information(),
                )

    def test_handler_emits_result_and_marks_delivery(self):
        submitted = []
        reverser = Reverser(
            self.config,
            self.devices,
            submit=submitted.append,
            execution_backend=FakeBackend(),
        )
        envelope = TaskEnvelope(
            sender="orchestrator",
            recipient="reverser",
            task_id="task-mailbox",
            content="Map the packet parser",
            information=task_information(),
        )

        reverser.receive(envelope)

        self.assertIsInstance(submitted[0], ResultEnvelope)
        self.assertEqual(submitted[0].correlation_id, envelope.envelope_id)
        self.assertEqual(reverser._tasks["task-mailbox"].status, "delivered")

    def test_handler_query_returns_correlated_status(self):
        submitted = []
        reverser = Reverser(
            self.config,
            self.devices,
            submit=submitted.append,
            execution_backend=FakeBackend(),
        )
        reverser.start_task(
            "task-query",
            "Map parser",
            task_information(),
        )
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

    def test_handler_query_reports_accepted_task_still_starting(self):
        submitted = []
        reverser = Reverser(
            self.config,
            self.devices,
            submit=submitted.append,
            execution_backend=FakeBackend(),
        )
        query = SystemEnvelope(
            sender="orchestrator",
            recipient="reverser",
            task_id="task-starting",
            event="query status",
            action=SystemAction.QUERY_TASK,
            arguments={"accepted_task": True},
        )

        reverser.receive(query)

        self.assertEqual(submitted[0].status, "starting")
        self.assertNotIn("unknown", submitted[0].summary)
        self.assertEqual(submitted[0].correlation_id, query.envelope_id)
