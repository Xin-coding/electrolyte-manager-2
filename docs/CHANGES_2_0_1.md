# 2.0.1 — pump preparation and diagnostic fixes

The formal Settings page previously produced floating-point force values such as `50.0`. They passed numeric validation but failed integer Modbus serialization before START. Independent pump tests used a different conversion path and did not expose this defect.

## Changes

- Formal settings, presets, startup loading and dose/test commands share force normalization. Integer-valued numbers become integers; fractional, boolean, non-finite and out-of-range force values are rejected without truncation. Existing presets containing `50.0` remain compatible.
- Parameter encoding is checked before hardware writes, including exact finite volume, flow, the firmware refresh flow and representable duration. Unsupported precision is explained when settings are applied. Hardware capacity and readback checks remain in force. An old preset with unsupported pump precision can still open for editing and recording, but cannot enable feedback.
- Every real counter change, including a single resolution tick, restarts the final-counter stability window. The separate final-dose resolution tolerance remains unchanged.
- Results carry failure categories. Parameter/preparation/software errors no longer all appear as hardware pump faults. The monitor retains the full last failure and distinguishes attempted START, confirmed stop and accounting provenance. Formal and independent test results use separate labels and displays.
- Test buttons and readiness labels reflect connection, ownership, hardware readiness, unresolved accounting, entered parameters, syringe capacity and available allowance. The service rechecks eligibility before accepting a test.

## Verification

- 83 automated tests pass. Added coverage exercises real Qt form parsing, preset roundtrips, pending setting activation, pH and conductivity decisions, and actual Modbus packet generation/readback through an in-memory serial endpoint. Real serial access is prohibited in these regression tests.
- Added cases cover invalid numeric types, unrepresentable parameters before any writes, continuous single-tick counter changes, categorized error visibility, bilingual formal-result labels, disconnected/faulted readiness and legacy preset loading.
- Authorized local bench: two formal-feedback injections requested at 0.100 mL and 5 mL/min each were accounted as 0.100 mL by the pump counter. The repeated start succeeded: the next decision was 10.485 s after confirmed stop, and the next physical START was 14.032 s after stop, meeting the configured 10 s mixing delay.
- A software STOP test requested a 0.050 mL cutoff with a 0.200 mL finite hardware cap. The final counter delta was 0.056 mL; running state, STOP acknowledgement and stopped state were observed before the hardware cap. Combined measured counter consumption was 0.256 mL.
- Three-device collection lasted approximately 158 s and received 79 pH samples plus three conductivity records. The experiment database retained 78 pH samples (one arrived before recording began), all three conductivity records and both formal dose results; SQLite integrity was `ok`. Temporary experimental settings and the original allowance were restored, while actual test consumption remained charged to the existing load.

These are instrument-counter and command-timing checks. External weighing, physical fault injection, 24 h uninterrupted acceptance and electrolysis chemistry validation remain incomplete. Raw experiment records, identities and diagnostic logs remain local; this document contains aggregate conclusions only.

## Updating

Close the old application after stopping feedback and confirming the pump is stopped. Extract the new portable package and retain the existing local `data` directory. There is no automatic updater. Loading a preset or opening the new version does not start the pump.
