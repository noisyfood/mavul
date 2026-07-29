import threading
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace

from system.envelope import Envelope, SystemAction, SystemEnvelope
from system.mailbox import PriorityMailbox


ErrorHandler = Callable[[str, Envelope, BaseException], None]


class AgentRuntime:
    """Dispatch one main Agent mailbox with a bounded worker pool."""

    def __init__(
        self,
        name: str,
        receiver: Callable[[Envelope], None],
        accepted_envelopes: tuple[type[Envelope], ...],
        max_concurrency: int,
        stop: Callable[[], None] | None,
        on_error: ErrorHandler,
    ) -> None:
        self.name = name
        self.receiver = receiver
        self.accepted_envelopes = accepted_envelopes
        self.max_concurrency = max_concurrency
        self.stop_callback = stop
        self._on_error = on_error
        self._mailbox = PriorityMailbox()
        self._executor = ThreadPoolExecutor(
            max_workers=max_concurrency,
            thread_name_prefix=f"{name}-task",
        )
        self._lock = threading.RLock()
        self._futures: set[Future[None]] = set()
        self._pending: deque[Envelope] = deque()
        self._running = 0
        self._seen: set[object] = set()
        self._closed = False
        self._dispatcher = threading.Thread(
            target=self._dispatch,
            name=f"{name}-mailbox",
            daemon=True,
        )
        self._dispatcher.start()

    def submit(self, envelope: Envelope) -> None:
        if not isinstance(envelope, self.accepted_envelopes):
            expected = ", ".join(item.__name__ for item in self.accepted_envelopes)
            raise TypeError(f"agent '{self.name}' accepts: {expected}")
        with self._lock:
            if self._closed:
                raise RuntimeError(f"agent runtime is closed: {self.name}")
            if envelope.envelope_id in self._seen:
                return
            self._seen.add(envelope.envelope_id)
        self._mailbox.put(envelope)

    def deliver_system_now(self, envelope: SystemEnvelope) -> None:
        if not isinstance(envelope, self.accepted_envelopes):
            raise TypeError(f"agent '{self.name}' does not accept SystemEnvelope")
        self._invoke(envelope)

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._pending.clear()
        self._mailbox.close(discard=True)
        if threading.current_thread() is not self._dispatcher:
            self._dispatcher.join()
        try:
            if self.stop_callback is not None:
                self.stop_callback()
        finally:
            self._executor.shutdown(wait=True, cancel_futures=True)

    @property
    def pending(self) -> int:
        with self._lock:
            scheduled = self._running + len(self._pending)
        return len(self._mailbox) + scheduled

    def _dispatch(self) -> None:
        while True:
            envelope = self._mailbox.get()
            if envelope is None:
                return
            if isinstance(envelope, SystemEnvelope):
                envelope = self._cancel_pending(envelope)
                self._invoke(envelope)
                continue
            with self._lock:
                if self._running < self.max_concurrency:
                    self._start(envelope)
                else:
                    self._pending.append(envelope)

    def _invoke(self, envelope: Envelope) -> None:
        try:
            self.receiver(envelope)
        except BaseException as exc:
            self._on_error(self.name, envelope, exc)

    def _forget(self, future: Future[None]) -> None:
        with self._lock:
            self._futures.discard(future)
            self._running -= 1
            if not self._closed and self._pending:
                self._start(self._pending.popleft())

    def _start(self, envelope: Envelope) -> None:
        self._running += 1
        future = self._executor.submit(self._invoke, envelope)
        self._futures.add(future)
        future.add_done_callback(self._forget)

    def _cancel_pending(self, envelope: SystemEnvelope) -> SystemEnvelope:
        cancelled: list[str] = []
        task_id = envelope.task_id
        if envelope.action == SystemAction.INTERRUPT_TASK and task_id:
            removed = self._mailbox.remove_agent(
                lambda item: item.task_id == task_id
            )
            cancelled.extend(
                item.task_id for item in removed if item.task_id is not None
            )
        elif envelope.action == SystemAction.INTERRUPT_AGENT:
            removed = self._mailbox.remove_agent(lambda _item: True)
            cancelled.extend(
                item.task_id for item in removed if item.task_id is not None
            )
        with self._lock:
            if envelope.action == SystemAction.INTERRUPT_TASK and task_id:
                retained = deque()
                while self._pending:
                    pending = self._pending.popleft()
                    if pending.task_id == task_id:
                        cancelled.append(task_id)
                    else:
                        retained.append(pending)
                self._pending = retained
            elif envelope.action == SystemAction.INTERRUPT_AGENT:
                cancelled = [
                    item.task_id
                    for item in self._pending
                    if item.task_id is not None
                ]
                self._pending.clear()
        if not cancelled:
            return envelope
        arguments = dict(envelope.arguments)
        arguments["cancelled_task_ids"] = cancelled
        return replace(envelope, arguments=arguments)
