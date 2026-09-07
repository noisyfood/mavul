import json
import threading
import uuid
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class LeaseStatus(StrEnum):
    GRANTED = "granted"
    WAITING = "waiting"
    QUARANTINED = "quarantined"


@dataclass(frozen=True)
class DeviceEndpoint:
    device_id: str
    name: str
    ip_address: str
    telnet_port: int | None = None
    ssh_port: int | None = None
    serial_port: str | None = None

    @classmethod
    def from_mapping(
        cls,
        device_id: str,
        config: Mapping[str, Any],
    ) -> "DeviceEndpoint":
        if not device_id or Path(device_id).name != device_id:
            raise ValueError("device ID must be a safe path component")
        ip_address = config.get("ip_address")
        if not isinstance(ip_address, str) or not ip_address:
            raise ValueError(f"device '{device_id}' requires ip_address")
        return cls(
            device_id=device_id,
            name=str(config.get("name", device_id)),
            ip_address=ip_address,
            telnet_port=_optional_port(config.get("telnet_port"), "telnet_port"),
            ssh_port=_optional_port(config.get("ssh_port"), "ssh_port"),
            serial_port=_optional_text(config.get("serial_port"), "serial_port"),
        )


@dataclass(frozen=True)
class DeviceLease:
    lease_id: uuid.UUID
    device_id: str
    agent_id: str
    task_id: str
    generation: int


@dataclass(frozen=True)
class LeaseRequest:
    status: LeaseStatus
    lease: DeviceLease | None = None


@dataclass(frozen=True)
class DeviceBaseline:
    device_id: str
    version: int
    updated_by: str
    configuration: dict[str, Any]


class Device:
    """Manage one exclusive device lease, wait queue, and baseline file."""

    def __init__(self, endpoint: DeviceEndpoint, baseline_root: Path) -> None:
        self.endpoint = endpoint
        self._baseline_root = baseline_root.resolve()
        self._baseline_root.mkdir(parents=True, exist_ok=True)
        self._baseline_path = self._baseline_root / f"{endpoint.device_id}.json"
        self._lock = threading.RLock()
        self._waiters: deque[tuple[str, str]] = deque()
        self._lease: DeviceLease | None = None
        self._generation = 0
        self._quarantine_reason: str | None = None

    @property
    def lease(self) -> DeviceLease | None:
        with self._lock:
            return self._lease

    @property
    def quarantine_reason(self) -> str | None:
        with self._lock:
            return self._quarantine_reason

    def acquire(
        self,
        agent_id: str,
        task_id: str,
        allow_quarantined: bool = False,
    ) -> LeaseRequest:
        if not agent_id:
            raise ValueError("agent_id must not be empty")
        if not task_id:
            raise ValueError("task_id must not be empty")
        owner = (agent_id, task_id)
        with self._lock:
            if self._lease is not None and (
                self._lease.agent_id,
                self._lease.task_id,
            ) == owner:
                return LeaseRequest(LeaseStatus.GRANTED, self._lease)
            if self._quarantine_reason is not None:
                if allow_quarantined and self._lease is None:
                    return LeaseRequest(LeaseStatus.GRANTED, self._grant(*owner))
                return LeaseRequest(LeaseStatus.QUARANTINED)
            if self._lease is None:
                return LeaseRequest(LeaseStatus.GRANTED, self._grant(*owner))
            if owner not in self._waiters:
                self._waiters.append(owner)
            return LeaseRequest(LeaseStatus.WAITING)

    def release(self, lease: DeviceLease) -> DeviceLease | None:
        with self._lock:
            self._validate_lease(lease)
            self._lease = None
            if self._quarantine_reason is not None or not self._waiters:
                return None
            return self._grant(*self._waiters.popleft())

    def cancel_waiter(self, agent_id: str, task_id: str) -> bool:
        """Cancel one waiting task without affecting the current lease."""
        if not agent_id:
            raise ValueError("agent_id must not be empty")
        if not task_id:
            raise ValueError("task_id must not be empty")
        with self._lock:
            return self._remove_waiter(agent_id, task_id)

    def preempt(
        self,
        target_agent: str,
        target_task_id: str,
    ) -> tuple[DeviceLease | None, DeviceLease]:
        if not target_agent:
            raise ValueError("target_agent must not be empty")
        if not target_task_id:
            raise ValueError("target_task_id must not be empty")
        target = (target_agent, target_task_id)
        with self._lock:
            if self._quarantine_reason is not None:
                raise RuntimeError("quarantined device cannot be assigned")
            previous = self._lease
            if previous is not None and (
                previous.agent_id,
                previous.task_id,
            ) == target:
                return previous, self._lease
            if previous is not None:
                self._waiters.appendleft((previous.agent_id, previous.task_id))
            self._remove_waiter(*target)
            self._lease = None
            return previous, self._grant(*target)

    def quarantine(self, reason: str) -> DeviceLease | None:
        if not reason:
            raise ValueError("quarantine reason must not be empty")
        with self._lock:
            previous = self._lease
            self._lease = None
            self._quarantine_reason = reason
            return previous

    def clear_quarantine(self) -> DeviceLease | None:
        with self._lock:
            self._quarantine_reason = None
            if self._lease is None and self._waiters:
                return self._grant(*self._waiters.popleft())
            return None

    def remove_agent(self, agent_id: str) -> bool:
        """Remove a waiter; quarantine if it still owns this device."""
        with self._lock:
            self._waiters = deque(
                owner for owner in self._waiters if owner[0] != agent_id
            )
            if self._lease is None or self._lease.agent_id != agent_id:
                return False
            self._lease = None
            self._quarantine_reason = f"lease owner unregistered: {agent_id}"
            return True

    def read_baseline(self) -> DeviceBaseline | None:
        with self._lock:
            self._check_baseline_path()
            if not self._baseline_path.exists():
                return None
            data = json.loads(self._baseline_path.read_text(encoding="utf-8"))
            return DeviceBaseline(
                device_id=data["device_id"],
                version=data["version"],
                updated_by=data["updated_by"],
                configuration=data["configuration"],
            )

    def update_baseline(
        self,
        lease: DeviceLease,
        configuration: Mapping[str, Any],
    ) -> DeviceBaseline:
        with self._lock:
            self._validate_lease(lease)
            self._check_baseline_path()
            previous = self.read_baseline()
            baseline = DeviceBaseline(
                device_id=self.endpoint.device_id,
                version=1 if previous is None else previous.version + 1,
                updated_by=lease.agent_id,
                configuration=dict(configuration),
            )
            temporary = self._baseline_path.with_name(
                f".{self._baseline_path.name}.{uuid.uuid4().hex}.tmp"
            )
            try:
                with temporary.open("x", encoding="utf-8") as output:
                    json.dump(
                        baseline.__dict__,
                        output,
                        ensure_ascii=False,
                        indent=2,
                    )
                    output.write("\n")
                temporary.replace(self._baseline_path)
            finally:
                temporary.unlink(missing_ok=True)
            return baseline

    def _grant(self, agent_id: str, task_id: str) -> DeviceLease:
        self._generation += 1
        self._lease = DeviceLease(
            uuid.uuid4(),
            self.endpoint.device_id,
            agent_id,
            task_id,
            self._generation,
        )
        return self._lease

    def _validate_lease(self, lease: DeviceLease) -> None:
        if self._lease != lease:
            raise RuntimeError("device lease is not current")

    def _remove_waiter(self, agent_id: str, task_id: str) -> bool:
        try:
            self._waiters.remove((agent_id, task_id))
        except ValueError:
            return False
        return True

    def _check_baseline_path(self) -> None:
        if self._baseline_path.is_symlink() or (
            self._baseline_path.exists()
            and self._baseline_path.resolve().parent != self._baseline_root
        ):
            raise RuntimeError("device baseline must remain under baseline root")


class DeviceManager:
    """Own Device instances; policy decisions remain with Orchestrator."""

    def __init__(
        self,
        configs: Mapping[str, Any],
        baseline_root: str | Path,
    ) -> None:
        root = Path(baseline_root)
        self._devices = {}
        for device_id, config in configs.items():
            if not isinstance(config, Mapping):
                raise TypeError(f"device '{device_id}' configuration must be a table")
            self._devices[device_id] = Device(
                DeviceEndpoint.from_mapping(device_id, config),
                root,
            )

    def get(self, device_id: str) -> Device:
        try:
            return self._devices[device_id]
        except KeyError:
            raise LookupError(f"unknown device: {device_id}") from None

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(self._devices)

    def remove_agent(self, agent_id: str) -> tuple[str, ...]:
        quarantined = [
            device_id
            for device_id, device in self._devices.items()
            if device.remove_agent(agent_id)
        ]
        return tuple(quarantined)


def _optional_port(value: object, name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not 0 < value < 65536:
        raise ValueError(f"{name} must be a valid TCP port")
    return value


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value
