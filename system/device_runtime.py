from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from system.device import (
    Device,
    DeviceBaseline,
    DeviceLease,
    DeviceManager,
    LeaseRequest,
)
from system.envelope import Envelope, SystemAction, SystemEnvelope


class DeviceRuntime:
    """Execute device lease operations without making recovery decisions."""

    def __init__(
        self,
        configs: Mapping[str, Any],
        baseline_root: str | Path,
        recovery_agent_id: str,
        submit: Callable[[Envelope], None],
        deliver_control: Callable[[SystemEnvelope], None],
        require_agent: Callable[[str], None],
    ) -> None:
        self._manager = DeviceManager(configs, baseline_root)
        self._recovery_agent_id = recovery_agent_id
        self._submit = submit
        self._deliver_control = deliver_control
        self._require_agent = require_agent

    def get(self, device_id: str) -> Device:
        return self._manager.get(device_id)

    @property
    def ids(self) -> tuple[str, ...]:
        return self._manager.ids

    def acquire(self, device_id: str, agent_id: str) -> LeaseRequest:
        self._require_agent(agent_id)
        return self.get(device_id).acquire(
            agent_id,
            allow_quarantined=agent_id == self._recovery_agent_id,
        )

    def release(self, lease: DeviceLease) -> None:
        next_lease = self.get(lease.device_id).release(lease)
        if next_lease is not None:
            self._notify_available(next_lease)

    def preempt(
        self,
        device_id: str,
        target_agent: str,
        requested_by: str,
    ) -> DeviceLease:
        if requested_by != "orchestrator":
            raise PermissionError("only Orchestrator may preempt a device")
        self._require_agent(target_agent)
        device = self.get(device_id)
        previous = device.lease.agent_id if device.lease else None
        if previous is not None and previous != target_agent:
            self._deliver_control(
                SystemEnvelope(
                    sender="agent-system",
                    recipient=previous,
                    event="device lease preempted",
                    action=SystemAction.DEVICE_PREEMPTED,
                    arguments={"device_id": device_id},
                )
            )
        _, lease = device.preempt(target_agent)
        self._notify_available(lease)
        return lease

    def quarantine(self, device_id: str, reason: str) -> None:
        previous = self.get(device_id).quarantine(reason)
        if previous is not None:
            self._submit(
                SystemEnvelope(
                    sender="agent-system",
                    recipient=previous,
                    event="device quarantined",
                    action=SystemAction.DEVICE_QUARANTINED,
                    details={"reason": reason},
                    arguments={"device_id": device_id},
                )
            )

    def clear_quarantine(self, device_id: str, requested_by: str) -> None:
        if requested_by != "orchestrator":
            raise PermissionError("only Orchestrator may clear device quarantine")
        lease = self.get(device_id).clear_quarantine()
        if lease is not None:
            self._notify_available(lease)

    def read_baseline(self, device_id: str) -> DeviceBaseline | None:
        return self.get(device_id).read_baseline()

    def update_baseline(
        self,
        lease: DeviceLease,
        configuration: dict[str, Any],
    ) -> DeviceBaseline:
        if lease.agent_id != self._recovery_agent_id:
            raise PermissionError("only Recovery Agent may update device baseline")
        return self.get(lease.device_id).update_baseline(lease, configuration)

    def remove_agent(self, agent_id: str) -> tuple[str, ...]:
        return self._manager.remove_agent(agent_id)

    def _notify_available(self, lease: DeviceLease) -> None:
        self._submit(
            SystemEnvelope(
                sender="agent-system",
                recipient=lease.agent_id,
                event="device available",
                action=SystemAction.DEVICE_AVAILABLE,
                arguments={
                    "device_id": lease.device_id,
                    "lease_id": str(lease.lease_id),
                    "generation": lease.generation,
                },
            )
        )
