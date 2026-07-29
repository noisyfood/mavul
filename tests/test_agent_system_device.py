import tempfile
import unittest

from system.agent_system import AgentSystem
from system.config import Config
from system.envelope import SystemAction, SystemEnvelope


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

            worker_lease = system.acquire_device("router", "worker").lease
            with self.assertRaises(PermissionError):
                system.update_device_baseline(worker_lease, {"mode": "worker"})
            system.release_device(worker_lease)
            recovery_lease = system.acquire_device("router", "recovery").lease

            baseline = system.update_device_baseline(
                recovery_lease,
                {"mode": "baseline"},
            )

        self.assertEqual(baseline.updated_by, "recovery")

    def test_device_preemption_requires_orchestrator(self):
        with tempfile.TemporaryDirectory() as directory:
            system = self.build_system(directory)
            self.register(system, "worker-a", "worker-b")
            system.acquire_device("router", "worker-a")

            with self.assertRaises(PermissionError):
                system.preempt_device(
                    "router",
                    "worker-b",
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
            system.acquire_device("router", "worker-a")

            lease = system.preempt_device(
                "router",
                "worker-b",
                requested_by="orchestrator",
            )

        self.assertEqual(interrupted[0].action, SystemAction.DEVICE_PREEMPTED)
        self.assertEqual(lease.agent_id, "worker-b")

    def test_unregistering_child_quarantines_held_device(self):
        with tempfile.TemporaryDirectory() as directory:
            system = self.build_system(directory)
            self.assertTrue(
                system.register_child_agent("worker.child", lambda _mail: None)
            )
            system.acquire_device("router", "worker.child")

            system.unregister_child_agent("worker.child")
            device = system.devices.get("router")

        self.assertIsNone(device.lease)
        self.assertIsNotNone(device.quarantine_reason)


if __name__ == "__main__":
    unittest.main()
