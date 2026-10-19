# Validation status — 2.0.3

This document distinguishes executable software checks, instrument evidence and checks that require more elapsed time or external measurement.

Version 2.0.3 has **158 passing automated tests**, including the revised monitoring-only policy for flow, duration and counter deviations; STOP priority and stale-queue invalidation; delayed START and pre-START connection failure; result-journal replay and exactly-once accounting; and test/formal mixing isolation. Storage failure now prevents the next injection without interrupting an already-started finite dose. Existing signal-alarm protection remains active during that wait. These boundary checks use memory transports and explicitly prohibit real serial ports. No new physical injection or fault-injection validation is claimed. See [2.0.3 changes](CHANGES_2_0_3.md).

The 2.0.3 native Windows Qt interface was checked in Chinese and English, including monitoring-only notes, estimated-total markers and invalid-input highlighting. The rebuilt portable executable launched with Python paths removed, used 199.4 MiB at startup and closed with exit code 0. These checks used separate local data folders and did not connect to devices. Production settings and load-ledger file hashes were unchanged.

Version 2.0.2 adds 43 input-protection regressions, bringing the suite to 126 passing tests. Tests prohibit physical serial access and verify malformed/range/precision errors, bilingual inline messages, cross-field alarm and STOP limits, partial presets, and unchanged effective/pending settings, consumption and full mixing deadline after rejected drafts. Existing pump packet and feedback tests remain included. No additional physical injections were required for this form change; the 2.0.1 bench evidence remains historical evidence, not a new hardware test. See [2.0.2 changes](CHANGES_2_0_2.md).

Version 2.0.0 was approved for general release on 2026-10-19. Release status does not change the validation results below.

Version 2.0.1 adds 32 automated regressions (83 passing tests in total) and a fresh local small-dose bench run. The 2.0.0 test suite had not exercised formal form input through actual Modbus serialization, allowing a floating-point force bug to escape. This gap and the fixes are documented in [2.0.1 changes](CHANGES_2_0_1.md). Earlier acceptance below is retained as historical evidence; the outstanding physical and endurance limitations still apply.

## Completed

- 51 automated tests: directional pH/EC target bands, raw alarm counts and recovery, new-sample deduplication, full monotonic mixing, sensor outages, settings revisions, allowance/refill/test accounting, unique results, delayed counters, preparation longer than six seconds, missed short running states, repeated flow configuration, START reply loss, STOP during preparation, bounded storage, failed writes/recovery, crash marking, package export/import, language switching, history display and PNG export.
- Real Metrohm 781 communication and real Apera PC8500 identity/data decoding. PC8500 does not reliably answer a repeated identity handshake during PC-Link; the production worker uses the validated handshake once per connection, then regular data queries.
- Two consecutive 0.100 mL finite pump tests at 1 mL/min, 50% force: pump counters reported 0.100 mL for each; second start succeeded.
- Real software STOP test with 0.050 mL software threshold and 0.200 mL finite hardware cap: final pump counter delta 0.056 mL; running immediately before STOP, acknowledgement and confirmed stop were captured before the hardware cap. Polling/mechanical latency makes this distinct from precise finite dosing.
- 300 h equivalent fixture: 540,000 pH samples, 18,000 conductivity samples, 18,000 representative pump events and 3,600 injection results. Database 67.6 MiB, generation/query process peak 28.1 MiB, each plotted series at most 5,000 points; SQLite integrity check `ok`. This is a scale fixture, not 300 hours of real acquisition.
- Five pages rendered with the Windows Qt platform and inspected; Unicode text rendered correctly. Offscreen platform fonts are not used to judge Windows appearance.
- The portable executable opened with Python paths removed, using its bundled Python/Qt libraries, and exited normally with code 0. Startup memory was 181.4 MiB. Build analysis uses an isolated PATH to avoid collecting incompatible ICU DLLs from unrelated installed tools.
- Two real feedback-sequence 0.100 mL pulses with a 10 s full mixing interval: next decision occurred 11.843 s after confirmed stop; the next physical START occurred 15.359 s after stop (including parameter preparation). Both counter deltas were 0.100 mL.
- A 230.9 s real acquisition segment collected 115 pH points and four conductivity records. The three live conductivity records arrived 60.000 s and 60.016 s apart, with no reconnection events during that segment.

## Not completed / not claimed

- The continuous acquisition run ended after approximately 1 h 12 min, preserving 2,144 pH samples and 71 conductivity records. Follow-up monitoring was disabled at the user's request. The 24 h acceptance is **not completed**; the partial run is not counted as a 24 h pass.
- External gravimetric measurement of actual liquid volume/flow has not been performed. Counter and command timing evidence is not a substitute for a balance and known-density fluid.
- Physical unplug/replug during active injection, real force-limit activation, disk-full on the real Windows volume and deliberate host power loss are not claimed as physical tests. Relevant failure paths have offline coverage, but hardware fault-injection acceptance remains to be completed.
- The sensors are immersed in separate buffers; no electrolysis was running. No claim is made that chemical feedback performance has been validated.

## Regression discovered during development

Sending STOP exactly `requested_volume / requested_flow` seconds after START prematurely stopped an initial 0.100 mL test at a stable counter delta of 0.095 mL. The completed implementation separates finite pump metering, bounded software supervision, and counter-triggered STOP testing. Stable actual delivery is recorded even if it differs from the command; it is not converted to zero or an unknown dose solely by a timer discrepancy. The first test and its subsequent stable-counter reconciliation remain local acceptance records.

Local reports contain instrument identities and measured data. They are not committed to this public repository. This public document reports aggregate results only.
