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
        self._lock = threading.RLock()
        self._shutdown_lock = threading.Lock()
        self._task_threads: set[threading.Thread] = set()
        self._query_threads: set[threading.Thread] = set()
        self._executor = ThreadPoolExecutor(
            max_workers=max_concurrency,
            thread_name_prefix=f"{name}-task",
            initializer=self._register_task_thread,
        )
        self._queries = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"{name}-query",
            initializer=self._register_query_thread,
        )
        self._futures: set[Future[None]] = set()
        self._pending: deque[Envelope] = deque()
        self._running = 0
        self._control_order = 0
        self._seen: set[object] = set()
        self._stopping = False
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
            if self._stopping:
                raise RuntimeError(f"agent runtime is stopping: {self.name}")
            if envelope.envelope_id in self._seen:
                return
            self._seen.add(envelope.envelope_id)
            self._mailbox.put(envelope)

    def deliver_system_now(self, envelope: SystemEnvelope) -> None:
        if not isinstance(envelope, self.accepted_envelopes):
            raise TypeError(f"agent '{self.name}' does not accept SystemEnvelope")
        self._invoke(envelope)

    def shutdown(self) -> None:
        if self.owns_thread(threading.current_thread()):
            raise RuntimeError(
                f"agent runtime cannot stop from its own thread: {self.name}"
            )
        with self._shutdown_lock:
            with self._lock:
                if self._closed:
                    return
                first_attempt = not self._stopping
                self._stopping = True
                self._pending.clear()
            if first_attempt:
                self._mailbox.close(discard=True)
            try:
                if self.stop_callback is not None:
                    self.stop_callback()
            except BaseException:
                self._executor.shutdown(wait=False, cancel_futures=True)
                self._queries.shutdown(wait=False, cancel_futures=True)
                raise
            self._dispatcher.join()
            self._executor.shutdown(wait=True, cancel_futures=True)
            self._queries.shutdown(wait=True, cancel_futures=True)
            with self._lock:
                self._closed = True

    def owns_thread(self, thread: threading.Thread) -> bool:
        """Return whether *thread* executes work owned by this runtime."""
        with self._lock:
            return (
                thread is self._dispatcher
                or thread in self._task_threads
                or thread in self._query_threads
            )

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
            with self._lock:
                if self._stopping:
                    continue
            if isinstance(envelope, SystemEnvelope) and envelope.action in {
                SystemAction.INTERRUPT_TASK,
                SystemAction.RESUME_TASK,
            }:
                envelope = self._with_control_order(envelope)
            if (
                isinstance(envelope, SystemEnvelope)
                and envelope.action == SystemAction.QUERY_TASK
            ):
                self._queries.submit(self._invoke, envelope)
                continue
            if isinstance(envelope, SystemEnvelope) and envelope.action not in {
                SystemAction.RESUME_TASK,
                SystemAction.DEVICE_AVAILABLE,
            }:
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
            if not self._stopping and not self._closed and self._pending:
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
                lambda item: (
                    item.task_id == task_id and self._can_cancel(item)
                )
            )
            cancelled.extend(
                item.task_id for item in removed if item.task_id is not None
            )
        elif envelope.action == SystemAction.INTERRUPT_AGENT:
            removed = self._mailbox.remove_agent(self._can_cancel)
            cancelled.extend(
                item.task_id for item in removed if item.task_id is not None
            )
        with self._lock:
            if envelope.action == SystemAction.INTERRUPT_TASK and task_id:
                retained = deque()
                while self._pending:
                    pending = self._pending.popleft()
                    if pending.task_id == task_id and self._can_cancel(pending):
                        cancelled.append(task_id)
                    else:
                        retained.append(pending)
                self._pending = retained
            elif envelope.action == SystemAction.INTERRUPT_AGENT:
                cancelled.extend(
                    item.task_id
                    for item in self._pending
                    if item.task_id is not None and self._can_cancel(item)
                )
                self._pending = deque(
                    item for item in self._pending if not self._can_cancel(item)
                )
        if not cancelled:
            return envelope
        arguments = dict(envelope.arguments)
        arguments["cancelled_task_ids"] = cancelled
        return replace(envelope, arguments=arguments)

    @staticmethod
    def _can_cancel(envelope: Envelope) -> bool:
        return not (
            isinstance(envelope, SystemEnvelope)
            and envelope.action == SystemAction.DEVICE_AVAILABLE
        )

    def _with_control_order(self, envelope: SystemEnvelope) -> SystemEnvelope:
        with self._lock:
            self._control_order += 1
            order = self._control_order
        arguments = dict(envelope.arguments)
        arguments["runtime_control_order"] = order
        return replace(envelope, arguments=arguments)

    def _register_task_thread(self) -> None:
        with self._lock:
            self._task_threads.add(threading.current_thread())

    def _register_query_thread(self) -> None:
        with self._lock:
            self._query_threads.add(threading.current_thread())
