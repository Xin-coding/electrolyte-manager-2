# Validation status — 3.2.0


## Version 3.2.0 acceptance (2026-11-05)

- **298 automated tests passed**, including the complete previous suite and 23 additional range/input regressions. Physical serial access remains prohibited in automated tests. The 58 pyqtgraph deprecation warnings remain unchanged.
- Actual Qt mouse/key events verify countdown increment/decrement buttons, limits, disabled controls, invalid drafts and strict ASCII integer validation. Invalid text survives refresh and cannot schedule feedback. Zero is allowed in either field, but not as the total duration.
- Plot controls return to the compact native context menu. Tests cover focus while typing, cursor preservation during repeated refresh, invalid bounds, absolute timestamp precision, restore-auto, linked X axes, independent Y axes, old-interval queries, outdated query results and current-view export. Menu/action lifetimes are retained explicitly for the packaged Qt binding.
- Bilingual countdown fixtures cover 100%, 125% and 150% scale, standard and narrower windows, pending, failed and invalid-input states. Plot fixtures confirm the extra bottom range toolbar is absent.
- CQ Windows 3.1.0 inspection confirms live Metrohm pH acquisition on its dedicated adapter. PC8500 access is blocked by a separate Uni Bern EC Suite process that is currently recording. The pump adapter denies access without an identifiable user-process handle; Windows reports a reboot is required after a targeted adapter restart request. The ongoing external recording was not stopped. Neither a physical countdown activation nor small-dose injection is claimed until these conditions are resolved. Detailed local evidence is kept outside the public package.
- Pump protocol, dosing rules, countdown service, filtering, temperature compensation, charge calculation and SQLite format are unchanged. Final portable-build, local-update and public-download results are recorded in the delivery report.

## Version 3.1.0 automated acceptance (2026-11-05)

- **275 automated tests passed**, including all 216 previous tests and 59 countdown tests. The 58 warnings are existing pyqtgraph default-parameter deprecations. Physical serial access is blocked in the test suite.
- Simulated monotonic clocks verify a six-hour delay, maximum duration, one-shot activation, wall-clock changes, deadline grace (5 seconds), suspension during activation, and no restore after restart. Cancellation is tested before, during and immediately after activation, including cancellation from a second thread and stale command generations.
- Simulated sensor/pump state verifies pump disconnection/fault/stale status, invalid and buffered readings, exact freshness boundaries for M80/PC8500/pH, allowance and storage failures, parameter changes, refill, identity changes, ordinary notes/current changes, retained automatic targets and the requirement for a new post-activation reading.
- The existing syringe dialog is forced at scheduling even if the syringe was previously confirmed. Cancelling it creates no reservation. The deadline path has no UI callback or second dialog. Recording, charge accumulation and database event evidence are covered while feedback remains off.
- **24 interface views** cover Chinese/English, 100%/125%/150% scale, 1420×930 and 1100×760 logical windows, pending and persistent-failure states. Countdown controls and status were inspected in the generated screenshots.
- This release performs **no physical pump dosing or new device hardware acceptance**. M80 read-only hardware evidence below belongs to 3.0. The Windows portable build and public-download verification are documented in the release delivery report.

## Version 3.0.0 acceptance (2026-11-05)

- **216 automated tests passed** locally and on GitHub Windows runners. Coverage includes M80 frame validation and units, fixed one-second scheduling, independent Metrohm/PC8500/M80 recording, both pH/conductivity combinations, port conflicts and stale worker events, manual chart ranges and current-view PNG/SVG export. Physical serial access is blocked in all unit tests. The 58 warnings are pyqtgraph parameter-default deprecations.
- The final Windows executable was built from clean commit `0e81777af3f7cf25c83b038f1c63597cd2da852a`. Its M80-only hardware acceptance used an isolated workspace, 38400/8N1, Modbus address 1, and a manually selected adapter. It stored **330 valid conductivity/temperature samples**, including continued recording after a software disconnect and reconnect. No pump or pH device was connected or commanded.
- The first **300 consecutive samples** spanned **298.984 seconds**. Mean arrival interval was **0.999946 seconds**, minimum **0.953**, maximum **1.047**, and 95th percentile **1.032 seconds**. The deliberate reconnect gap is excluded from these continuous-run statistics.
- Communication evidence contains **418 requests and 418 responses**, all Modbus function 03 reads, with valid CRCs. There were no instrument-setting, calibration, or other-device commands. Instrument identity, conductivity mode, units and sensor status were checked by the driver. This is communication acceptance, not calibration or chemical-performance validation.
- The packaged GUI completed recording, loaded the saved history, exported full CSV/JSON and complete report PNG/CSV, and exported the manual viewport as PNG/SVG. All **330 CSV measurement rows matched the database**. Mouse zoom entered manual mode; refresh kept the range; restore-auto and linked X axes worked. An SVG closed-path incompatibility found during the preliminary run was fixed, regression-tested, rebuilt, and retested in the final executable.
- Chinese and English interface fixtures were inspected at 100%, 125% and 150% scales, with standard and narrower windows. Charge cards use the same 23 pt style. Native packaged Windows windows and exported images were also inspected during M80 acceptance.
- Original production data/configuration remained separate from acceptance data. Pump protocol/execution, feedback decisions, storage format, charge calculations and units modules are unchanged from the preserved 2.1.1 baseline (`64e7d54`). The release archive contains no experiment databases, personal configuration, bindings or diagnostic logs.
- Other devices received **software regression only** in this release. Earlier physical results below remain historical; this release does not complete outstanding 24-hour endurance, external gravimetric or physical fault-injection acceptance.

## Version 2.1.0 acceptance

- **173 automated tests passed** locally, including historical pump alignment and exact step geometry, unknown/estimated volume handling, full-data exports, final-result-before-report ordering, failed finalization and export retry, normal-close rejection, input protection, and piecewise/zero/signed/missing/overflow current handling. Forty warnings are existing pyqtgraph default-parameter deprecations, not test failures.
- Native Windows Qt rendering passed in Chinese and English. Reference lines do not affect signal y-axis bounds; history opens in a separate pump dialog. PNG files were visually inspected, including axis fonts and the exact step timing.
- A pre-existing EM2 schema-1 database passed SQLite integrity checking. All **355** exported injection rows matched saved timestamps, per-dose amounts and cumulative values; the final total was **35.496 mL**, with two refill events. The original database SHA-256 was unchanged. No private database, identity or raw report is included here.
- The rebuilt Windows portable application launched with Python paths removed, reported version **2.1.0**, used approximately **162.4 MiB** at startup and closed with exit code **0**. Its temporary data directory was isolated from production settings and syringe allowance state.
- Hardware protocol modules, pump execution code and feedback decision code were unchanged. No physical device was connected or driven for this release. These tests do not complete the outstanding physical or endurance acceptance listed below.


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
