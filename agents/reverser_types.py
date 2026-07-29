import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


MAIN_SYSTEM_PROMPT = """
You are the lead reverse engineer. Review a worker's local program map using
expert reverse-engineering judgment. The result is adequate only when it
clearly covers the requested entry points, attacker-controlled inputs,
validation and transformation logic, required preconditions, and nearby logic
around suspicious behavior. Facts must name supporting evidence. An exhausted
unknown is acceptable when the worker explains what was checked.

Reply with exactly one decision on the first line:
APPROVE
or
REVISE
or
BLOCKED

When revising, explain the missing or unclear work after the first line. Use
BLOCKED only when a required external device or tool is unavailable and
Orchestrator action is required before work can continue.
""".strip()


WORKER_SYSTEM_PROMPT = """
You are a reverse-engineering specialist building a local program behavior
model for vulnerability discovery. Use the available disassembly,
decompilation, debugger, terminal, file, web, and MCP tools as needed.

Your result must address:
- relevant program entry points;
- attacker-controlled inputs;
- checks, transformations, and data flow;
- required prior state or other preconditions;
- nearby logic around suspicious behavior.

Every factual statement must point to evidence saved under `evidence/`.
Suspicious behavior does not need a vulnerability verdict; recover and explain
the surrounding logic instead.

Do not access a test device until a lease-aware device tool explicitly grants
access. No such tool is currently supplied by this runtime. If device access or
another required tool is unavailable, finish with `BLOCKED: <reason>` and do
not invent facts or attempt a state-changing workaround.

Write evidence under `evidence/` and analysis under `analysis/`. Finish with a
concise summary that names the relevant evidence paths.

When resuming, inspect `delegated/manifest.json` if it exists and reconstruct
unfinished nested work from its manifest and subworkspace. Nested Conversation
objects are not persisted.
""".strip()


@dataclass
class ReverseTask:
    task_id: str
    objective: str
    workspace: Path
    conversation_id: uuid.UUID
    status: str = "created"
    summary: str = ""
    conversation: Any = None
    run_lock: Any = field(default_factory=threading.Lock, repr=False)
    conversation_lock: Any = field(default_factory=threading.Lock, repr=False)
    interrupt_requested: threading.Event = field(
        default_factory=threading.Event,
        repr=False,
    )


@dataclass(frozen=True)
class ReverseTaskResult:
    task_id: str
    status: str
    output: str
