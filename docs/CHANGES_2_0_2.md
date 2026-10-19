# 2.0.2 — inline input protection

Previously, numeric mistakes were mainly reported at submission without identifying the field. Forms now preserve editable drafts and show a red border plus a specific reason below each invalid numeric field on blur or submission. Chinese/English interface switching also switches the reasons; data-manager messages remain English.

## Behavior

- Reject malformed decimals, comma separators, unit/text suffixes, non-finite numbers, underflow and invalid ranges. Trim only outer whitespace; accept standard decimal/scientific notation and integer-valued force such as `50.0` without rounding fractional force.
- Reuse pump protocol validation for finite volume precision, flow/refresh flow and representable duration. Report current syringe capacity, alarm ordering/target-band conflicts, and STOP-test cap/allowance problems at the relevant fields.
- Invalid Apply, Save preset and allowance submissions do not enqueue mutations. Current effective settings, an already pending valid revision, active dose, full mixing deadline and consumption remain intact. Corrected settings still follow the existing versioned activation rules.
- Missing control values do not block data recording or partial presets. Enabling feedback with incomplete effective settings directs the user to Settings. Resuming with valid effective settings does not silently apply an unfinished draft.
- STOP cap errors do not disable an otherwise valid finite test. The service still independently validates every command before physical actions. No form edits send serial commands.
- Low remaining allowance prevents the next complete dose; it does not make otherwise valid settings unsavable. The load display explains this condition. Record-only metadata remains optional and unrestricted.
- Historical time-range errors are inline and never submit invalid queries.
- Extremely large finite pump inputs now produce a parameter-validation error instead of an integer-conversion overflow.

## Validation

126 automated tests pass, including 43 new cases. The new cases cover accepted/rejected formats, field messages and language switching, limits and linked fields, unchanged effective/pending versions during injection/mixing, correction and normal pending activation, preserved mixing deadlines, partial presets, blocked invalid saves/allowance requests, independent test eligibility and invalid history ranges. Real serial ports are prohibited in these fixtures. Existing feedback, storage, accounting and packet regressions also pass.

The final portable executable was inspected with native Windows controls in an isolated workspace and without Python paths. Chinese and English inline messages were legible. Submitting `0，1` left effective version 1 unchanged while recording continued; correcting it to `0.2` and applying created version 2. No devices were connected by the test application. It exited normally with code 0 after approximately 105 seconds; observed peak working set was 241.2 MiB. This short startup/UI check is not an endurance test.

No new injection or physical fault test is claimed for this input-interface release. The 2.0.1 small-dose bench results remain documented separately. External gravimetric verification, physical fault injection, 24 h uninterrupted acceptance and electrolysis chemistry validation remain incomplete. Local data, device identities and diagnostic logs are excluded from the public release.

## Update

Close the old application after stopping feedback and confirming the pump has stopped. Extract the complete portable package and preserve the existing local `data` folder. The existing Desktop shortcut points to `app/ElectrolyteManager.exe`. Opening a new version or loading a preset does not start feedback.
