import tempfile
import unittest
from pathlib import Path

from system.device import DeviceEndpoint, DeviceManager, LeaseStatus


class DeviceTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.manager = DeviceManager(
            {
                "router": {
                    "name": "Lab router",
                    "ip_address": "192.0.2.10",
                    "ssh_port": 22,
                }
            },
            self.temporary_directory.name,
        )
        self.device = self.manager.get("router")

    def test_exclusive_lease_and_fifo_handoff(self):
        first = self.device.acquire("agent-a")
        second = self.device.acquire("agent-b")

        self.assertEqual(first.status, LeaseStatus.GRANTED)
        self.assertEqual(second.status, LeaseStatus.WAITING)
        next_lease = self.device.release(first.lease)
        self.assertEqual(next_lease.agent_id, "agent-b")

        with self.assertRaises(RuntimeError):
            self.device.release(first.lease)

    def test_preempt_puts_previous_owner_first_in_wait_queue(self):
        first = self.device.acquire("agent-a").lease

        previous, replacement = self.device.preempt("agent-b")
        returned = self.device.release(replacement)

        self.assertEqual(previous, "agent-a")
        self.assertEqual(returned.agent_id, "agent-a")
        self.assertGreater(returned.generation, first.generation)

    def test_quarantine_blocks_assignment(self):
        self.device.acquire("agent-a")
        previous = self.device.quarantine("state unknown")

        request = self.device.acquire("agent-b")

        self.assertEqual(previous, "agent-a")
        self.assertEqual(request.status, LeaseStatus.QUARANTINED)
        restored = self.device.clear_quarantine()
        self.assertIsNone(restored)

    def test_recovery_can_lease_quarantined_device(self):
        self.device.acquire("agent-a")
        self.device.quarantine("state unknown")

        recovery = self.device.acquire("recovery", allow_quarantined=True)

        self.assertEqual(recovery.status, LeaseStatus.GRANTED)
        self.assertEqual(recovery.lease.agent_id, "recovery")
        self.device.release(recovery.lease)
        self.assertIsNone(self.device.clear_quarantine())

    def test_baseline_requires_current_lease_and_versions_updates(self):
        lease = self.device.acquire("recovery").lease

        first = self.device.update_baseline(lease, {"hostname": "router"})
        second = self.device.update_baseline(lease, {"hostname": "restored"})

        self.assertEqual(first.version, 1)
        self.assertEqual(second.version, 2)
        self.assertEqual(self.device.read_baseline(), second)

    def test_endpoint_references_credentials_by_environment_name(self):
        endpoint = DeviceEndpoint.from_mapping(
            "router",
            {
                "ip_address": "192.0.2.10",
                "credential_env": {"username": "MISSING_TEST_USERNAME"},
            },
        )

        with self.assertRaises(RuntimeError):
            endpoint.credentials()


if __name__ == "__main__":
    unittest.main()
