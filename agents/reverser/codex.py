import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from openai_codex import ApprovalMode, Codex, InvalidRequestError, Sandbox

from .codex_config import CodexRuntimeConfig
from .execution import (
    ExecutionBackend,
    ExecutionError,
    ExecutionResult,
    ExecutionSession,
    ExecutionStatus,
    UninitializedSessionError,
)
from .prompts import QUERY_SYSTEM_PROMPT, REVIEWER_SYSTEM_PROMPT, WORKER_SYSTEM_PROMPT


class CodexExecutionSession:
    """Adapt one Codex Thread and its active TurnHandle."""

    def __init__(
        self,
        client: Any,
        thread: Any,
        *,
        effort: str | None,
        observer_config: Mapping[str, Any] | None = None,
    ) -> None:
        self._client = client
        self._thread = thread
        self._effort = effort
        self._observer_config = dict(observer_config or {})
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._active_turn: Any = None
        self._interrupt_requested = False
        self._active_queries = 0
        self._query_turns: list[Any] = []
        self._closed = False

    @property
    def session_id(self) -> str:
        return str(self._thread.id)

    def run(
        self,
        prompt: str,
        output_schema: Mapping[str, Any] | None = None,
        on_started: Callable[[], None] | None = None,
    ) -> ExecutionResult:
        with self._lock:
            if self._closed:
                raise ExecutionError("Codex session is closed")
            if self._active_turn is not None:
                raise ExecutionError("Codex session already has an active turn")
            if self._interrupt_requested:
                self._interrupt_requested = False
                return ExecutionResult(ExecutionStatus.INTERRUPTED, "")
            try:
                handle = self._thread.turn(
                    prompt,
                    effort=self._effort,
                    output_schema=dict(output_schema) if output_schema else None,
                )
            except Exception as exc:
                raise ExecutionError(f"unable to start Codex turn: {exc}") from exc
            self._active_turn = handle

        try:
            try:
                if on_started is not None:
                    on_started()
                result = handle.run()
            except Exception as exc:
                if on_started is not None:
                    try:
                        handle.interrupt()
                    except Exception:
                        pass
                raise ExecutionError(f"Codex turn failed: {exc}") from exc

            status = getattr(result.status, "value", str(result.status))
            if status == "interrupted":
                execution_status = ExecutionStatus.INTERRUPTED
            elif status == "completed":
                execution_status = ExecutionStatus.COMPLETED
            else:
                raise ExecutionError(f"Codex turn ended with status: {status}")
            return ExecutionResult(execution_status, result.final_response or "")
        finally:
            with self._lock:
                if self._active_turn is handle:
                    self._active_turn = None

    def query(self, prompt: str) -> str:
        with self._condition:
            if self._closed:
                raise ExecutionError("Codex session is closed")
            self._active_queries += 1
        handle = None
        try:
            observer = self._client.thread_fork(
                self.session_id,
                approval_mode=ApprovalMode.deny_all,
                developer_instructions=QUERY_SYSTEM_PROMPT,
                ephemeral=True,
                sandbox=Sandbox.read_only,
                config=self._observer_config,
            )
            handle = observer.turn(prompt, effort=self._effort)
            with self._condition:
                self._query_turns.append(handle)
                closing = self._closed
            if closing:
                handle.interrupt()
            result = handle.run()
        except Exception as exc:
            raise ExecutionError(f"Codex observer failed: {exc}") from exc
        finally:
            with self._condition:
                if handle is not None:
                    self._query_turns = [
                        item for item in self._query_turns if item is not handle
                    ]
                self._active_queries -= 1
                self._condition.notify_all()
        status = getattr(result.status, "value", str(result.status))
        if status != "completed":
            raise ExecutionError(f"Codex observer ended with status: {status}")
        return result.final_response or ""

    def interrupt(self) -> None:
        with self._lock:
            handle = self._active_turn
            if handle is None:
                self._interrupt_requested = True
                return
        try:
            handle.interrupt()
        except InvalidRequestError:
            # The completion event won the race; run() still consumes it.
            return
        except Exception as exc:
            raise ExecutionError(f"unable to interrupt Codex turn: {exc}") from exc

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            handle = self._active_turn
            query_turns = tuple(self._query_turns)
        failures: list[Exception] = []
        for current in ((handle,) if handle is not None else ()) + query_turns:
            try:
                current.interrupt()
            except InvalidRequestError:
                pass
            except Exception as exc:
                failures.append(exc)
        with self._condition:
            while self._active_queries:
                self._condition.wait()
        if failures:
            raise ExecutionError(f"unable to close Codex session: {failures[0]}")


class CodexExecutionBackend(ExecutionBackend):
    """Create isolated worker and reviewer Threads using the Codex SDK."""

    name = "codex"

    def __init__(
        self,
        config: Mapping[str, Any],
        client: Any | None = None,
    ) -> None:
        self._settings = CodexRuntimeConfig(config)
        client_config = self._settings.client_config()
        self._lock = threading.Lock()
        self._closed = False
        self._client = client or Codex(client_config)

    def open_worker(
        self,
        workspace: Path,
        session_id: str | None,
    ) -> ExecutionSession:
        options = self._settings.thread_options(
            "worker",
            workspace,
            WORKER_SYSTEM_PROMPT,
            Sandbox.workspace_write,
            include_tools=True,
        )
        try:
            if session_id:
                thread = self._client.thread_resume(session_id, **options)
            else:
                thread = self._client.thread_start(ephemeral=False, **options)
        except InvalidRequestError as exc:
            if session_id and "no rollout found for thread id" in str(exc).lower():
                raise UninitializedSessionError(
                    "Codex thread has no resumable turn history"
                ) from exc
            raise ExecutionError(f"unable to resume Codex worker: {exc}") from exc
        except Exception as exc:
            action = "resume" if session_id else "start"
            raise ExecutionError(f"unable to {action} Codex worker: {exc}") from exc
        return CodexExecutionSession(
            self._client,
            thread,
            effort=self._settings.effort("worker"),
            observer_config=self._settings.observer_config(),
        )

    def open_reviewer(self, workspace: Path) -> ExecutionSession:
        options = self._settings.thread_options(
            "main",
            workspace,
            REVIEWER_SYSTEM_PROMPT,
            Sandbox.read_only,
            include_tools=False,
        )
        try:
            thread = self._client.thread_start(ephemeral=True, **options)
        except Exception as exc:
            raise ExecutionError(f"unable to start Codex reviewer: {exc}") from exc
        return CodexExecutionSession(
            self._client,
            thread,
            effort=self._settings.effort("main"),
        )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._client.close()
            self._closed = True
