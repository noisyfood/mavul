# Let Reverser Modify Its Device

Reverser may modify the Device leased to its task when runtime analysis requires
it. The first implementation uses the command-line Telnet client with a PTY.
It records the operations, undoes
its changes, and only then releases the Device. A paused running task keeps its
lease. A blocked task keeps it while changes or cleanup are still pending. Each
task's Worker owns its Device
Session and may reuse it across turns; other Workers do not share that process.
After review approval, the same Worker undoes its changes and closes the Device
Session before MAVUL releases the lease and delivers the result. A system-wide
stop is the exception: MAVUL closes the Device Session and exits without undoing
the Device changes.
