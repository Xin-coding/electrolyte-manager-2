# 2.0.3 — Pump stop boundaries and non-blocking measurement diagnostics

This release implements the reviewed pump boundary fixes and the user's revised control policy: measurement accuracy is monitored and recorded, not used to stop feedback dosing.

## Operating policy

- Flow readback differences, derived-duration differences, reported flow deviations, dose deviations, counter jumps/resets, coarse resolution, unstable counts and delayed publication produce monitoring notes. They do not issue STOP or require manual feedback recovery.
- Exceeding the requested-time estimate is a monitoring note, not a timed shutdown of ordinary feedback. The verified finite volume limit in the pump remains active. Two plausible successive volume-limit readings can trigger software volume supervision. Malformed flow/counter units do not mask a valid motion state or turn a numeric issue into a serial disconnection.
- A stable plausible final counter is used when available. Otherwise, the **programmed finite cap** is charged once as a conservative estimate. The record retains raw evidence, warning text and estimate source; load totals display `≈`. This can use up the configured allowance sooner than the actual liquid consumption. The explicit load allowance still governs whether another full dose is permitted.
- Manual STOP/pause/end/exit, connection failure, pump protection and the previously requested active-signal alarm remain interruption reasons. An explicit software STOP test also deliberately stops before its larger pump-side cap. Invalid finite-limit or force configuration prevents START. An unexpected control/communication failure during an active command still takes the stop path; it is not classified as a measurement deviation.
- Persistent storage failure blocks the **next** injection; a finite injection already sent is allowed to finish. The active control signal alarm and immediate user STOP remain effective during this storage pause. The injection intent must still be committed before any START. Storage recovery requires an explicit feedback resume.

## Boundary corrections

1. STOP is sent before status queries when stop is requested or START execution is uncertain. A failed query cannot skip the STOP attempt. START is never automatically retransmitted.
2. Stop generations invalidate previously queued injections in both the service and device queues, even after the stop flag is consumed. A later, explicitly submitted request can be accepted normally.
3. START acknowledgement is separated from motion observation. Early stopped replies do not immediately close the dose. Short needles can complete without observing the running state. Motion beginning during final observation returns to observing the same command.
4. The syringe signature is captured before preparation, so a connection failure before START can close as not-started after identity, configuration and stopped-state reconciliation. Healthy final counter anomalies use estimates instead of stranding the worker in manual accounting review.
5. A recovered connection error remains in evidence but does not permanently disable feedback when configuration and stopped state are confirmed. Estimates are permitted by the revised measurement policy. Manual/force/alarm states retain their own recovery rules.
6. Final results are durably journaled before committing the SQLite injection row. Only confirmed SQLite persistence and an atomic allowance update clear the intent. A failed acknowledgement, failed ledger replacement or process restart can retry one immutable result without counting twice or restarting the pump.
7. Pump-test mixing and formal-experiment mixing use separate deadlines. Completing a test never shortens an existing formal mixing interval.

## Validation scope

Automated fault injection covers the stop/query ordering, stale queues, lost START replies, delayed motion, pre-START disconnection, changed syringe, flow and counter anomalies, storage commit failures, replay after restart and mixing isolation. Tests use memory transports and prohibit real serial ports. Earlier input-protection, feedback, protocol, history/export and UI tests remain included.

This release does not claim new physical fault-injection or gravimetric validation. Prior successful bench injections are historical evidence only. The 24-hour endurance run, external weighing and physical unplug/force-limit tests remain incomplete; see [VALIDATION.md](VALIDATION.md).
