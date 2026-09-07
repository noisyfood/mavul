import threading
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from queue import Empty, Queue
from typing import Any

from system.agent_runtime import AgentRuntime
from system.config import Config
from system.device import DeviceBaseline, DeviceLease, LeaseRequest
from system.device_runtime import DeviceRuntime
from system.envelope import (
    Envelope,
    SystemAction,
    SystemEnvelope,
    TaskEnvelope,
)


Registrar = Callable[["AgentSystem", Config], None]


class StopCompletion:
    """Read-only view of one AgentSystem stop attempt."""

    __slots__ = ("__future",)

    def __init__(self, future: Future[None]) -> None:
        self.__future = future

    def done(self) -> bool:
        return self.__future.done()

    def cancelled(self) -> bool:
        return self.__future.cancelled()

    def result(self, timeout: float | None = None) -> None:
        return self.__future.result(timeout)

    def exception(self, timeout: float | None = None) -> BaseException | None:
        return self.__future.exception(timeout)

    def add_done_callback(
        self,
        callback: Callable[["StopCompletion"], object],
    ) -> None:
        self.__future.add_done_callback(lambda _future: callback(self))


@dataclass
class _StopAttempt:
    future: Future[None] = field(default_factory=Future)
    completion: StopCompletion = field(init=False)
    thread: threading.Thread | None = None

    def __post_init__(self) -> None:
        self.completion = StopCompletion(self.future)


class AgentSystem:
    """Route Envelopes and own threads, mailboxes, IDs, and device leases."""

    def __init__(
        self,
        config: Config,
        registrars: Iterable[Registrar] = (),
    ) -> None:
        if not isinstance(config, Config):
            raise TypeError("AgentSystem requires a parsed Config instance")
        self.config = config
        self._lock = threading.RLock()
        self._stop_lock = threading.Lock()
        self._agents: dict[str, AgentRuntime] = {}
        self._children: dict[str, Callable[[SystemEnvelope], None]] = {}
        self._child_seen: set[object] = set()
        self._errors: list[tuple[str, Envelope, BaseException]] = []
        self._control_threads: set[threading.Thread] = set()
        self._stop_attempt: _StopAttempt | None = None
        self._running = False
        self._stopping = False
        self._stopped = False
        control_workers = config.get("control_workers", 4)
        if (
            not isinstance(control_workers, int)
            or isinstance(control_workers, bool)
            or control_workers < 1
        ):
            raise ValueError("control_workers must be a positive integer")
        self._control = ThreadPoolExecutor(
            max_workers=control_workers,
            thread_name_prefix="agent-control",
            initializer=self._register_control_thread,
        )
        baseline_root = config.get("device_state_dir", "memory/devices")
        self.devices = DeviceRuntime(
            config.devices,
            baseline_root,
            str(config.get("recovery_agent_id", "recovery")),
            self.submit,
            self._deliver_control_now,
            self._require_agent_id,
        )
        self.initialize(registrars)

    def initialize(self, registrars: Iterable[Registrar]) -> None:
        for register in registrars:
            if not callable(register):
                raise TypeError("agent registrar must be callable")
            register(self, self.config)

    def register_agent(
        self,
        name: str,
        receiver: Callable[[Envelope], None],
        accepted_envelopes: tuple[type[Envelope], ...],
        max_concurrency: int,
        stop: Callable[[], None] | None = None,
    ) -> None:
        """Register one main Agent mailbox and its bounded task workers."""
        if not name:
            raise ValueError("agent name must not be empty")
        if not callable(receiver):
            raise TypeError("agent receiver must be callable")
        if not accepted_envelopes or not all(
            isinstance(item, type) and issubclass(item, Envelope)
            for item in accepted_envelopes
        ):
            raise TypeError("accepted_envelopes must contain Envelope classes")
        if (
            not isinstance(max_concurrency, int)
            or isinstance(max_concurrency, bool)
            or max_concurrency < 1
        ):
            raise ValueError("max_concurrency must be a positive integer")
        if stop is not None and not callable(stop):
            raise TypeError("agent stop callback must be callable")
        with self._lock:
            if self._stopping or self._stopped:
                raise RuntimeError("cannot register an agent while stopping")
            if name in self._agents or name in self._children:
                raise ValueError(f"agent already registered: {name}")
            self._agents[name] = AgentRuntime(
                name,
                receiver,
                accepted_envelopes,
                max_concurrency,
                stop,
                self._handle_receiver_error,
            )

    def register_child_agent(
        self,
        agent_id: str,
        interrupt: Callable[[SystemEnvelope], None],
    ) -> bool:
        """Register a nested Agent control endpoint without a mailbox."""
        if not agent_id or not callable(interrupt):
            raise ValueError("child agent requires an ID and interrupt callback")
        with self._lock:
            if self._stopping or self._stopped:
                raise RuntimeError("cannot register an agent while stopping")
            if agent_id in self._agents or agent_id in self._children:
                return False
            self._children[agent_id] = interrupt
            return True

    def unregister_child_agent(self, agent_id: str) -> None:
        with self._lock:
            self._children.pop(agent_id, None)
        self.devices.remove_agent(agent_id)

    @property
    def registered_agents(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._agents)

    def submit(self, envelope: Envelope) -> None:
        """Accept an Envelope from an Agent and route solely by recipient."""
        if not isinstance(envelope, Envelope):
            raise TypeError("AgentSystem accepts Envelope instances")
        with self._lock:
            if self._stopping or self._stopped:
                raise RuntimeError("agent system is stopping")
            runtime = self._agents.get(envelope.recipient)
            child = self._children.get(envelope.recipient)
            if child is not None:
                if not isinstance(envelope, SystemEnvelope):
                    raise TypeError("nested Agents accept only SystemEnvelope")
                if envelope.envelope_id in self._child_seen:
                    return
                self._child_seen.add(envelope.envelope_id)
        if runtime is not None:
            runtime.submit(envelope)
            return
        if child is not None:
            self._control.submit(
                self._invoke_child,
                envelope.recipient,
                child,
                envelope,
            )
            return
        raise LookupError(f"agent is not registered: {envelope.recipient}")

    def deliver(self, recipient: str, envelope: Envelope) -> None:
        """Compatibility wrapper that verifies Envelope routing."""
        if envelope.recipient != recipient:
            raise ValueError("recipient argument does not match Envelope")
        self.submit(envelope)

    def send_to_orchestrator(self, message: str | Envelope) -> None:
        envelope = (
            message
            if isinstance(message, Envelope)
            else TaskEnvelope(
                sender="user",
                recipient="orchestrator",
                content=message,
            )
        )
        self.submit(envelope)

    def acquire_device(
        self,
        device_id: str,
        agent_id: str,
        task_id: str,
    ) -> LeaseRequest:
        return self.devices.acquire(device_id, agent_id, task_id)

    def cancel_device_wait(
        self,
        device_id: str,
        agent_id: str,
        task_id: str,
    ) -> bool:
        return self.devices.cancel_waiter(device_id, agent_id, task_id)

    def release_device(self, lease: DeviceLease) -> None:
        self.devices.release(lease)

    def preempt_device(
        self,
        device_id: str,
        target_agent: str,
        target_task_id: str,
        requested_by: str,
    ) -> DeviceLease:
        return self.devices.preempt(
            device_id,
            target_agent,
            target_task_id,
            requested_by,
        )

    def quarantine_device(self, device_id: str, reason: str) -> None:
        self.devices.quarantine(device_id, reason)

    def clear_device_quarantine(self, device_id: str, requested_by: str) -> None:
        self.devices.clear_quarantine(device_id, requested_by)

    def read_device_baseline(self, device_id: str) -> DeviceBaseline | None:
        return self.devices.read_baseline(device_id)

    def update_device_baseline(
        self,
        lease: DeviceLease,
        configuration: dict[str, Any],
    ) -> DeviceBaseline:
        return self.devices.update_baseline(lease, configuration)

    def serve(self) -> None:
        if "orchestrator" not in self.registered_agents:
            raise RuntimeError("orchestrator is not registered")
        self._running = True
        lines: Queue[str | None] = Queue()

        def read_input() -> None:
            while self._running:
                try:
                    lines.put(input())
                except EOFError:
                    lines.put(None)
                    return

        threading.Thread(
            target=read_input,
            name="agent-system-input",
            daemon=True,
        ).start()
        while self._running:
            try:
                line = lines.get(timeout=0.1)
            except Empty:
                continue
            if line is None:
                break
            self.send_to_orchestrator(line)
        self._running = False

    def stop(self) -> StopCompletion:
        """Request final stop and return its observable completion."""
        with self._stop_lock:
            with self._lock:
                runtimes = tuple(self._agents.values())
                current = threading.current_thread()
                managed = current in self._control_threads or any(
                    runtime.owns_thread(current) for runtime in runtimes
                )
                attempt = self._stop_attempt
                coordinator_callback = (
                    attempt is not None and current is attempt.thread
                )
                if coordinator_callback and not attempt.future.done():
                    return attempt.completion
                if self._stopped:
                    if attempt is None:
                        raise RuntimeError("stopped system has no stop completion")
                elif attempt is None or attempt.future.done():
                    self._stopping = True
                    self._running = False
                    attempt = _StopAttempt()
                    attempt.thread = threading.Thread(
                        target=self._run_stop_attempt,
                        args=(attempt,),
                        name="agent-system-stop",
                    )
                    self._stop_attempt = attempt
                    try:
                        attempt.thread.start()
                    except BaseException as exc:
                        attempt.future.set_exception(exc)
                        self._stop_attempt = None
                        raise

        if managed or coordinator_callback:
            return attempt.completion
        try:
            attempt.future.result()
        finally:
            if attempt.thread is not None:
                attempt.thread.join()
        return attempt.completion

    @property
    def errors(self) -> tuple[tuple[str, Envelope, BaseException], ...]:
        with self._lock:
            return tuple(self._errors)

    def _require_agent_id(self, agent_id: str) -> None:
        with self._lock:
            if agent_id not in self._agents and agent_id not in self._children:
                raise LookupError(f"agent is not registered: {agent_id}")

    def _register_control_thread(self) -> None:
        with self._lock:
            self._control_threads.add(threading.current_thread())

    def _run_stop_attempt(self, attempt: _StopAttempt) -> None:
        try:
            self._perform_stop()
        except BaseException as exc:
            attempt.future.set_exception(exc)
        else:
            attempt.future.set_result(None)

    def _perform_stop(self) -> None:
        with self._lock:
            if self._stopped:
                return
            runtimes = tuple(self._agents.values())

        failures: list[tuple[str, BaseException]] = []
        for runtime in runtimes:
            try:
                runtime.shutdown()
            except BaseException as exc:
                failures.append((runtime.name, exc))
        if failures:
            names = ", ".join(name for name, _error in failures)
            error = RuntimeError(f"failed to stop registered agents: {names}")
            raise error from failures[0][1]

        self._control.shutdown(wait=True, cancel_futures=True)
        with self._lock:
            self._stopping = False
            self._stopped = True

    def _deliver_control_now(self, envelope: SystemEnvelope) -> None:
        with self._lock:
            runtime = self._agents.get(envelope.recipient)
            child = self._children.get(envelope.recipient)
        if runtime is not None:
            runtime.deliver_system_now(envelope)
            return
        if child is not None:
            self._invoke_child(envelope.recipient, child, envelope)
            return
        raise LookupError(f"agent is not registered: {envelope.recipient}")

    def _invoke_child(
        self,
        agent_id: str,
        callback: Callable[[SystemEnvelope], None],
        envelope: SystemEnvelope,
    ) -> None:
        with self._lock:
            if self._children.get(agent_id) is not callback:
                return
        try:
            callback(envelope)
        except BaseException as exc:
            self._handle_receiver_error(agent_id, envelope, exc)

    def _handle_receiver_error(
        self,
        name: str,
        envelope: Envelope,
        error: BaseException,
    ) -> None:
        with self._lock:
            self._errors.append((name, envelope, error))
            orchestrator_exists = "orchestrator" in self._agents
        if orchestrator_exists and name != "orchestrator":
            self.submit(
                SystemEnvelope(
                    sender="agent-system",
                    recipient="orchestrator",
                    task_id=envelope.task_id,
                    correlation_id=envelope.envelope_id,
                    event="agent receiver failed",
                    action=SystemAction.REPORT_FAILURE,
                    details={"agent_id": name, "error": str(error)},
                )
            )
