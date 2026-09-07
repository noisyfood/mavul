# MAVUL Domain

MAVUL researches software targets for vulnerabilities and records results that can
be checked against the user's threat model.

## Language

**Target**:
The subject of one vulnerability-research effort. A Target owns one or more Assets
and is not the same thing as any single Device.

**Asset**:
A concrete item used to analyze or test a Target, such as a Device or a source tree.

**Device**:
A running physical or virtual system that belongs to a Target. One Target may have
multiple Devices.
_Avoid_: Target

**Threat Model**:
The attacker abilities and conditions supplied by the user that a Finding must
satisfy.

**Finding**:
A vulnerability result that has sufficient evidence, can be reproduced, and
matches the user's Threat Model.
_Avoid_: Suspicion, unverified candidate

**Reverse Task**:
Work assigned to the Reverser to analyze supplied firmware or binaries and check
their runtime behavior on exactly one Device. A Reverse Task does not currently
require a Threat Model.

**Behavior Model**:
The Reverser's evidence-backed description of program inputs, checks, data flow,
required state, and relevant logic. A Behavior Model is not itself a Finding.

**Device Lease**:
The temporary, exclusive assignment of one Device to one task. Reading and
modifying the Device both require the lease. A paused running task keeps its
lease. A blocked task keeps it when changes or cleanup are still pending.

**Device Operation Log**:
The commands, Device output, and errors recorded in order during a task. It is
runtime evidence and must be cited by the Behavior Model.

**Worker**:
The persistent Codex thread owned by one Reverse Task. Its workspace, shell, and
running processes are not shared with other Workers.

**Device Session**:
The Telnet process a Worker uses to interact with its leased Device. It may stay
open across Worker turns and is closed before the Device Lease is released.
