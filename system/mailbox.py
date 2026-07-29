import threading
from collections import deque
from collections.abc import Callable

from system.envelope import Envelope, SystemEnvelope


class PriorityMailbox:
    """Unbounded system-first mailbox with FIFO order inside each priority."""

    def __init__(self) -> None:
        self._system: deque[Envelope] = deque()
        self._agent: deque[Envelope] = deque()
        self._condition = threading.Condition()
        self._closed = False

    def put(self, envelope: Envelope) -> None:
        if not isinstance(envelope, Envelope):
            raise TypeError("mailbox accepts Envelope instances")
        with self._condition:
            if self._closed:
                raise RuntimeError("mailbox is closed")
            queue = (
                self._system
                if isinstance(envelope, SystemEnvelope)
                else self._agent
            )
            queue.append(envelope)
            self._condition.notify()

    def get(self) -> Envelope | None:
        with self._condition:
            while not self._closed and not self._system and not self._agent:
                self._condition.wait()
            if self._system:
                return self._system.popleft()
            if self._agent:
                return self._agent.popleft()
            return None

    def close(self, discard: bool = False) -> None:
        with self._condition:
            self._closed = True
            if discard:
                self._system.clear()
                self._agent.clear()
            self._condition.notify_all()

    def remove_agent(self, matches: Callable[[Envelope], bool]) -> list[Envelope]:
        """Remove matching ordinary mail without disturbing FIFO order."""
        removed = []
        with self._condition:
            retained = deque()
            while self._agent:
                envelope = self._agent.popleft()
                if matches(envelope):
                    removed.append(envelope)
                else:
                    retained.append(envelope)
            self._agent = retained
        return removed

    def __len__(self) -> int:
        with self._condition:
            return len(self._system) + len(self._agent)
