import json
import tempfile
import threading
import unittest

from agents.orchestrator import Orchestrator
from agents.reverser import Reverser
from system.agent_system import AgentSystem
from system.config import Config
from system.envelope import (
    ResultEnvelope,
    SystemAction,
    SystemEnvelope,
    TaskEnvelope,
)
from tests.test_reverser import FakeBackend


class AgentSystemDeviceTest(unittest.TestCase):
    def build_system(self, directory, *, recovery_agent_id="recovery"):
        system = AgentSystem(
            Config.from_mapping(
                {
                    "agents": {},
                    "device_state_dir": directory,
                    "recovery_agent_id": recovery_agent_id,
                    "devices": {
                        "router": {
                            "ip_address": "192.0.2.10",
                        }
                    },
                }
            )
        )
        self.addCleanup(system.stop)
        return system

    @staticmethod
    def register(system, *agent_ids):
        for agent_id in agent_ids:
            system.register_agent(
                agent_id,
                lambda _envelope: None,
                (SystemEnvelope,),
                max_concurrency=1,
            )

    def test_only_recovery_agent_can_update_device_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            system = self.build_system(directory)
            self.register(system, "worker", "recovery")

            worker_lease = system.acquire_device(
                "router",
                "worker",
                "worker-task",
            ).lease
            with self.assertRaises(PermissionError):
                system.update_device_baseline(worker_lease, {"mode": "worker"})
            system.release_device(worker_lease)
            recovery_lease = system.acquire_device(
                "router",
                "recovery",
                "recovery-task",
            ).lease

            baseline = system.update_device_baseline(
                recovery_lease,
                {"mode": "baseline"},
            )

        self.assertEqual(baseline.updated_by, "recovery")

    def test_device_preemption_requires_orchestrator(self):
        with tempfile.TemporaryDirectory() as directory:
            system = self.build_system(directory)
            self.register(system, "worker-a", "worker-b")
            system.acquire_device("router", "worker-a", "task-a")

            with self.assertRaises(PermissionError):
                system.preempt_device(
                    "router",
                    "worker-b",
                    "task-b",
                    requested_by="worker-b",
                )

    def test_preemption_delivers_interrupt_before_new_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            system = self.build_system(directory)
            interrupted = []
            system.register_agent(
                "worker-a",
                interrupted.append,
                (SystemEnvelope,),
                max_concurrency=1,
            )
            self.register(system, "worker-b")
            system.acquire_device("router", "worker-a", "task-a")

            lease = system.preempt_device(
                "router",
                "worker-b",
                "task-b",
                requested_by="orchestrator",
            )

        self.assertEqual(interrupted[0].action, SystemAction.DEVICE_PREEMPTED)
        self.assertEqual(interrupted[0].task_id, "task-a")
        self.assertEqual(lease.agent_id, "worker-b")
        self.assertEqual(lease.task_id, "task-b")

    def test_handoff_notification_identifies_waiting_task(self):
        with tempfile.TemporaryDirectory() as directory:
            system = self.build_system(directory)
            available = []
            notified = threading.Event()

            def receive(envelope):
                if envelope.action == SystemAction.DEVICE_AVAILABLE:
                    available.append(envelope)
                    notified.set()

            system.register_agent(
                "worker",
                receive,
                (SystemEnvelope,),
                max_concurrency=1,
            )
            first = system.acquire_device("router", "worker", "task-a").lease
            waiting = system.acquire_device("router", "worker", "task-b")

            system.release_device(first)
            self.assertTrue(notified.wait(timeout=1))

        self.assertEqual(waiting.status.value, "waiting")
        self.assertEqual(available[0].task_id, "task-b")
        self.assertEqual(available[0].arguments["device_id"], "router")

    def test_cancel_device_wait_removes_only_the_waiting_task(self):
        with tempfile.TemporaryDirectory() as directory:
            system = self.build_system(directory)
            self.register(system, "worker-a", "worker-b")
            current = system.acquire_device(
                "router",
                "worker-a",
                "task-a",
            ).lease
            system.acquire_device("router", "worker-b", "task-b")

            cancelled = system.cancel_device_wait(
                "router",
                "worker-b",
                "task-b",
            )
            system.release_device(current)

        self.assertTrue(cancelled)
        self.assertIsNone(system.devices.get("router").lease)

    def test_quarantine_notification_identifies_lease_task(self):
        with tempfile.TemporaryDirectory() as directory:
            system = self.build_system(directory)
            quarantined = []
            notified = threading.Event()

            def receive(envelope):
                if envelope.action == SystemAction.DEVICE_QUARANTINED:
                    quarantined.append(envelope)
                    notified.set()

            system.register_agent(
                "worker",
                receive,
                (SystemEnvelope,),
                max_concurrency=1,
            )
            system.acquire_device("router", "worker", "task-a")

            system.quarantine_device("router", "state unknown")
            self.assertTrue(notified.wait(timeout=1))

        self.assertEqual(quarantined[0].task_id, "task-a")

    def test_unregistering_child_quarantines_held_device(self):
        with tempfile.TemporaryDirectory() as directory:
            system = self.build_system(directory)
            self.assertTrue(
                system.register_child_agent("worker.child", lambda _mail: None)
            )
            system.acquire_device("router", "worker.child", "child-task")

            system.unregister_child_agent("worker.child")
            device = system.devices.get("router")

        self.assertIsNone(device.lease)
        self.assertIsNotNone(device.quarantine_reason)

    def test_orchestrator_to_reverser_releases_device_before_result(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config.from_mapping(
                {
                    "agents": {
                        "reverser": {
                            "workspace": f"{directory}/reverser",
                            "max_concurrency": 2,
                            "main": {"model": "review"},
                            "worker": {"model": "worker"},
                        }
                    },
                    "device_state_dir": f"{directory}/devices",
                    "devices": {
                        "router": {
                            "ip_address": "192.0.2.10",
                            "telnet_port": 23,
                        }
                    },
                }
            )
            system = AgentSystem(config)
            output = []
            result_seen = threading.Event()

            def emit(line):
                output.append(json.loads(line))
                if output[-1].get("type") == "result":
                    result_seen.set()

            orchestrator = Orchestrator(system.submit, system.stop, emit)
            reverser = Reverser(
                config.agent("reverser"),
                system.devices,
                submit=system.submit,
                execution_backend=FakeBackend(),
            )
            system.register_agent(
                "orchestrator",
                orchestrator.receive,
                (TaskEnvelope, ResultEnvelope, SystemEnvelope),
                max_concurrency=1,
            )
            system.register_agent(
                "reverser",
                reverser.receive,
                (TaskEnvelope, SystemEnvelope),
                max_concurrency=reverser.max_concurrency,
                stop=reverser.stop,
            )
            try:
                system.send_to_orchestrator(
                    json.dumps(
                        {
                            "action": "reverse",
                            "target_id": "ios-xe",
                            "device_id": "router",
                            "binary_path": "/opt/ida/httpd",
                            "objective": "Map the request parser",
                            "username": "admin",
                            "password": "login-pass",
                            "enable_password": "enable-pass",
                        }
                    )
                )
                self.assertTrue(result_seen.wait(timeout=2))
            finally:
                system.stop()

        self.assertEqual(output[0]["type"], "accepted")
        self.assertEqual(output[-1]["status"], "ready_to_deliver")
        self.assertEqual(
            output[-1]["artifacts"],
            [f"{output[0]['task_id']}/analysis/behavior-model.md"],
        )
        self.assertEqual(
            reverser._tasks[output[0]["task_id"]].status,
            "delivered",
        )
        self.assertIsNone(system.devices.get("router").lease)


if __name__ == "__main__":
    unittest.main()
