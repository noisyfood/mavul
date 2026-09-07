import threading
import unittest
from unittest.mock import patch

from system.agent_system import AgentSystem
from system.config import Config
from system.envelope import SystemAction, SystemEnvelope, TaskEnvelope


class AgentSystemTest(unittest.TestCase):
    def setUp(self):
        self.system = AgentSystem(Config.from_mapping({"agents": {}}))
        self.addCleanup(self.system.stop)

    def test_enforces_task_concurrency_while_system_mail_bypasses_queue(self):
        gate = threading.Event()
        two_started = threading.Event()
        all_done = threading.Event()
        system_seen = threading.Event()
        lock = threading.Lock()
        started = []
        running = 0
        maximum = 0

        def receive(envelope):
            nonlocal running, maximum
            if isinstance(envelope, SystemEnvelope):
                system_seen.set()
                return
            with lock:
                started.append(envelope.content)
                running += 1
                maximum = max(maximum, running)
                if len(started) == 2:
                    two_started.set()
            gate.wait(timeout=3)
            with lock:
                running -= 1
                if len(started) == 3 and running == 0:
                    all_done.set()

        self.system.register_agent(
            "worker",
            receive,
            (TaskEnvelope, SystemEnvelope),
            max_concurrency=2,
        )
        for number in range(3):
            self.system.submit(
                TaskEnvelope(
                    sender="orchestrator",
                    recipient="worker",
                    content=str(number),
                )
            )
        self.assertTrue(two_started.wait(timeout=2))
        self.system.submit(
            SystemEnvelope(
                sender="orchestrator",
                recipient="worker",
                event="query",
                action=SystemAction.QUERY_TASK,
            )
        )

        self.assertTrue(system_seen.wait(timeout=1))
        self.assertEqual(maximum, 2)
        self.assertNotIn("2", started)
        gate.set()
        self.assertTrue(all_done.wait(timeout=2))
        self.assertEqual(started[-1], "2")

    def test_duplicate_envelope_runs_once(self):
        received = []
        called = threading.Event()

        def receive(envelope):
            received.append(envelope)
            called.set()

        self.system.register_agent(
            "worker",
            receive,
            (TaskEnvelope,),
            max_concurrency=1,
        )
        envelope = TaskEnvelope(
            sender="orchestrator",
            recipient="worker",
            content="once",
        )

        self.system.submit(envelope)
        self.system.submit(envelope)

        self.assertTrue(called.wait(timeout=1))
        self.assertEqual(received, [envelope])

    def test_stop_callback_quiesces_running_and_cancels_queued_tasks(self):
        system = AgentSystem(Config.from_mapping({"agents": {}}))
        started = []
        running = threading.Event()
        release = threading.Event()

        def receive(envelope):
            started.append(envelope.content)
            running.set()
            release.wait(timeout=2)

        system.register_agent(
            "worker",
            receive,
            (TaskEnvelope,),
            max_concurrency=1,
            stop=release.set,
        )
        for content in ("running", "queued"):
            system.submit(
                TaskEnvelope(
                    sender="orchestrator",
                    recipient="worker",
                    content=content,
                )
            )
        self.assertTrue(running.wait(timeout=1))

        system.stop()

        self.assertEqual(started, ["running"])

    def test_nested_agent_receives_direct_system_interrupt(self):
        received = []
        called = threading.Event()

        def interrupt(envelope):
            received.append(envelope)
            called.set()

        self.assertTrue(self.system.register_child_agent("task.child.1", interrupt))
        self.assertFalse(self.system.register_child_agent("task.child.1", interrupt))
        envelope = SystemEnvelope(
            sender="agent-system",
            recipient="task.child.1",
            event="device preempted",
            action=SystemAction.DEVICE_PREEMPTED,
        )

        self.system.submit(envelope)

        self.assertTrue(called.wait(timeout=1))
        self.assertEqual(received, [envelope])

    def test_interrupt_cancels_task_while_it_is_still_queued(self):
        gate = threading.Event()
        first_started = threading.Event()
        control_seen = threading.Event()
        started = []
        cancelled = []

        def receive(envelope):
            if isinstance(envelope, SystemEnvelope):
                cancelled.extend(envelope.arguments.get("cancelled_task_ids", []))
                control_seen.set()
                return
            started.append(envelope.task_id)
            first_started.set()
            gate.wait(timeout=2)

        self.system.register_agent(
            "worker",
            receive,
            (TaskEnvelope, SystemEnvelope),
            max_concurrency=1,
            stop=gate.set,
        )
        for task_id in ("running", "queued"):
            self.system.submit(
                TaskEnvelope(
                    sender="orchestrator",
                    recipient="worker",
                    task_id=task_id,
                    content=task_id,
                )
            )
        self.assertTrue(first_started.wait(timeout=1))
        self.system.submit(
            SystemEnvelope(
                sender="orchestrator",
                recipient="worker",
                task_id="queued",
                event="interrupt queued task",
                action=SystemAction.INTERRUPT_TASK,
            )
        )

        self.assertTrue(control_seen.wait(timeout=1))
        gate.set()
        self.assertEqual(cancelled, ["queued"])
        self.assertEqual(started, ["running"])

    def test_resume_uses_task_quota_without_blocking_control_dispatch(self):
        running = threading.Event()
        release = threading.Event()
        resumed = threading.Event()
        queried = threading.Event()

        def receive(envelope):
            if isinstance(envelope, TaskEnvelope):
                running.set()
                release.wait(timeout=2)
            elif envelope.action == SystemAction.RESUME_TASK:
                resumed.set()
            elif envelope.action == SystemAction.QUERY_TASK:
                queried.set()

        self.system.register_agent(
            "worker",
            receive,
            (TaskEnvelope, SystemEnvelope),
            max_concurrency=1,
            stop=release.set,
        )
        self.system.submit(
            TaskEnvelope(
                sender="orchestrator",
                recipient="worker",
                task_id="active",
                content="run",
            )
        )
        self.assertTrue(running.wait(timeout=1))
        self.system.submit(
            SystemEnvelope(
                sender="orchestrator",
                recipient="worker",
                task_id="paused",
                event="resume",
                action=SystemAction.RESUME_TASK,
            )
        )
        self.system.submit(
            SystemEnvelope(
                sender="orchestrator",
                recipient="worker",
                task_id="active",
                event="query",
                action=SystemAction.QUERY_TASK,
            )
        )

        self.assertTrue(queried.wait(timeout=1))
        self.assertFalse(resumed.is_set())
        release.set()
        self.assertTrue(resumed.wait(timeout=1))

    def test_device_available_uses_task_quota(self):
        running = threading.Event()
        release = threading.Event()
        device_started = threading.Event()
        queried = threading.Event()

        def receive(envelope):
            if isinstance(envelope, TaskEnvelope):
                running.set()
                release.wait(timeout=2)
            elif envelope.action == SystemAction.DEVICE_AVAILABLE:
                device_started.set()
            elif envelope.action == SystemAction.QUERY_TASK:
                queried.set()

        self.system.register_agent(
            "worker",
            receive,
            (TaskEnvelope, SystemEnvelope),
            max_concurrency=1,
            stop=release.set,
        )
        self.system.submit(
            TaskEnvelope(
                sender="orchestrator",
                recipient="worker",
                task_id="active",
                content="run",
            )
        )
        self.assertTrue(running.wait(timeout=1))
        self.system.submit(
            SystemEnvelope(
                sender="agent-system",
                recipient="worker",
                task_id="waiting",
                event="device available",
                action=SystemAction.DEVICE_AVAILABLE,
            )
        )
        self.system.submit(
            SystemEnvelope(
                sender="orchestrator",
                recipient="worker",
                task_id="active",
                event="query",
                action=SystemAction.QUERY_TASK,
            )
        )

        self.assertTrue(queried.wait(timeout=1))
        self.assertFalse(device_started.is_set())
        release.set()
        self.assertTrue(device_started.wait(timeout=1))

    def test_interrupt_does_not_discard_device_available_notification(self):
        running = threading.Event()
        release = threading.Event()
        device_seen = threading.Event()
        interrupt_seen = threading.Event()
        cancelled = []

        def receive(envelope):
            if isinstance(envelope, TaskEnvelope):
                running.set()
                release.wait(timeout=2)
            elif envelope.action == SystemAction.DEVICE_AVAILABLE:
                device_seen.set()
            elif envelope.action == SystemAction.INTERRUPT_TASK:
                cancelled.extend(envelope.arguments.get("cancelled_task_ids", []))
                interrupt_seen.set()

        self.system.register_agent(
            "worker",
            receive,
            (TaskEnvelope, SystemEnvelope),
            max_concurrency=1,
            stop=release.set,
        )
        self.system.submit(
            TaskEnvelope(
                sender="orchestrator",
                recipient="worker",
                task_id="active",
                content="run",
            )
        )
        self.assertTrue(running.wait(timeout=1))
        self.system.submit(
            SystemEnvelope(
                sender="agent-system",
                recipient="worker",
                task_id="waiting",
                event="device available",
                action=SystemAction.DEVICE_AVAILABLE,
            )
        )
        self.system.submit(
            SystemEnvelope(
                sender="orchestrator",
                recipient="worker",
                task_id="waiting",
                event="interrupt",
                action=SystemAction.INTERRUPT_TASK,
            )
        )

        self.assertTrue(interrupt_seen.wait(timeout=1))
        self.assertEqual(cancelled, [])
        release.set()
        self.assertTrue(device_seen.wait(timeout=1))

    def test_slow_query_does_not_block_interrupt_dispatch(self):
        query_started = threading.Event()
        release_query = threading.Event()
        interrupt_seen = threading.Event()

        def receive(envelope):
            if envelope.action == SystemAction.QUERY_TASK:
                query_started.set()
                release_query.wait(timeout=2)
            elif envelope.action == SystemAction.INTERRUPT_TASK:
                interrupt_seen.set()

        self.system.register_agent(
            "worker",
            receive,
            (SystemEnvelope,),
            max_concurrency=1,
            stop=release_query.set,
        )
        for action in (SystemAction.QUERY_TASK, SystemAction.INTERRUPT_TASK):
            self.system.submit(
                SystemEnvelope(
                    sender="orchestrator",
                    recipient="worker",
                    task_id="task-1",
                    event=action.value,
                    action=action,
                )
            )

        self.assertTrue(query_started.wait(timeout=1))
        self.assertTrue(interrupt_seen.wait(timeout=1))
        release_query.set()

    def test_serve_exits_when_orchestrator_stops_system(self):
        input_blocked = threading.Event()
        release_input = threading.Event()
        calls = 0

        def receive(_envelope):
            input_blocked.wait(timeout=1)
            self.system.stop()

        def fake_input():
            nonlocal calls
            calls += 1
            if calls == 1:
                return '{"action":"stop"}'
            input_blocked.set()
            release_input.wait(timeout=2)
            raise EOFError

        self.system.register_agent(
            "orchestrator",
            receive,
            (TaskEnvelope,),
            max_concurrency=1,
        )
        try:
            with patch("builtins.input", side_effect=fake_input):
                self.system.serve()
        finally:
            release_input.set()

        self.assertTrue(input_blocked.is_set())

if __name__ == "__main__":
    unittest.main()
