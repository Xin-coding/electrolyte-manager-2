# Electrolyte Manager 2.0

Current version: **2.1.0**. Historical pump curves can be compared and exported. Ending a recording generates three PNG charts and three full-data CSVs. The monitor adds charge estimated from declared current, expands plots, and scales signal axes independently of reference lines. Recording must be ended before closing. Existing pump protocols and execution logic are unchanged. See [2.1 changes](docs/CHANGES_2_1_0.md).

Independent Windows acquisition and syringe-pump feedback software for Metrohm 781, Apera PC8500 and Longer ASP. This is a fresh Python / PySide6 application; the previous application is not its code baseline. Hardware protocols and observed firmware behavior were used as references.

## Run the portable application

Extract the entire ZIP to a writable folder and open `app/ElectrolyteManager.exe`. Keep `_internal` beside the executable. No Python installation is needed. The application creates `data` and `exports` beside `app` and keeps device bindings, experiments and diagnostic logs locally.

The first release provides five pages: Connections, Pump tests, Settings, Monitor, Data manager. The main interface switches between Chinese and English; plots and data analysis use English.

1. Detect devices, check the displayed COM ports and identities, or select each port manually. Save a binding profile if needed.
2. Set PC8500 automatic upload to **1 minute on the instrument**. Its initial buffered frame is recorded but cannot trigger feedback.
3. Optional: run independent finite-volume or software STOP tests. These use separate test parameters. Test delivery consumes the current syringe load, not the experiment total.
4. Configure dose, flow, force, load allowance, complete mixing time, control direction, tolerance and fixed alarm boundaries. No dosing values or safety boundaries are guessed.
5. Start recording. Optional experiment metadata does not block recording. Review the syringe information, then enable feedback separately.
6. End the experiment to finish its SQLite record. History reading and exports never issue device commands.

## Control behavior

A new control sample can cause at most one finite injection. The software confirms the stopped state, waits the full configured mixing time on a monotonic clock, then requires a genuinely new post-mixing sample. Target-band arrival during a pulse does not abort it. Alarm, manual STOP and actual faults can interrupt it. Remaining load must support a complete pulse.

Reported flow, dose deviation, counter resets/jumps/instability and elapsed time beyond the requested-time estimate do not themselves send STOP or latch a feedback pause. Ordinary dosing uses the verified finite pump limit, with software supervision of confirmed volume-limit readings. An explicit software STOP test is a separate user-requested interruption. If final counters are unusable, the programmed finite cap is charged once as an explicitly labeled estimate; the current load displays an approximation mark. No extra needle makes up a reported short delivery. Test results cannot shorten an experiment's mixing interval.

pH and conductivity use the same directional target-band logic. EMA smoothing uses alpha 0.5. Alarms use raw valid data: pH needs three consecutive out-of-range readings and three in-range readings to recover; conductivity needs one each. Only the selected sensor interlocks dosing. Sensor loss does not end recording. A finite active pulse finishes, then dosing waits for reconnection/new data.

Settings are always editable. Apply creates a version; an active injection and its mixing interval retain their original settings. The next decision applies the pending version. Applying settings never resumes a manual stop. Changing a syringe does not rescale doses or rewrite past records.

The red STOP button requests a priority serial STOP. It is **a software control**, not a substitute for the instrument's physical stop when the cable or computer cannot communicate. Pump force protection remains an instrument function. A force stop is not reported as a successful dose.

## Records and plots

Each experiment has its own SQLite database with original/processed samples, instrument and reception times, settings revisions, decisions, alarms, connection events, injection intents/results and refill events. WAL and short background transactions keep acquisition separate from presentation. Intent is persisted before START. Final evidence is locally journaled, then committed to SQLite before clearing the intent and updating the allowance. Retries and startup journal recovery cannot account for a result twice or resume old feedback. Stable plausible pump counters take precedence; estimated values retain their provenance and warnings.

Use Data manager to compare up to 12 EM2 experiments, aligned by elapsed or absolute time. Export sample/injection CSV, settings JSON, streaming event JSONL, PNG/SVG plots or an `.em2zip` experiment package. Older application databases are deliberately unsupported.

Plots retain at most 5,000 representative points per curve; original records are never discarded to meet the plot limit. Raw diagnostics are off by default and rotate within 200 MiB. Persistent storage failure for 30 seconds blocks subsequent injections while the already-started finite dose completes; signal alarms and user STOP remain active. A final-result commit failure retains the result journal and prevents the next injection until saved; feedback needs a manual resume after storage recovery.

## Estimated hydroxide

`Estimated OH- [mmol/L] = 1000 * 10 ** (processed_pH - pKw(T))`.

`pKw(T)` is linearly interpolated from a bundled 0–99 °C, 1 °C table generated with IAPWS water density and ionization equations at 0.101325 MPa. The corresponding pH measurement temperature is used. Missing/out-of-table temperatures produce no OH estimate. This is a dilute-water activity-based estimate, not a measurement of hydroxide concentration in concentrated electrolyte, and never changes feedback.

Reference: [IAPWS release on the ionization constant of water](https://iapws.org/technical-guidance/release/Ionization). Build-only table generation uses `iapws==1.5.5`; the runtime does not require that package.

## Development

Python 3.12 on Windows:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m em2.main
powershell -ExecutionPolicy Bypass -File tools\build_windows.ps1
```

`em2/engine.py` is deterministic and has no device/GUI imports. `devices` owns serial I/O. `runtime.py` coordinates the actors. `storage.py` owns persistence/streaming readers. `ui.py` submits requests rather than issuing serial commands.

Offline fixtures are tests only; there is no simulation mode in the product. Physical acceptance scripts require an explicit `--run-hardware` flag and a prepared collection bench. Read [validation status](docs/VALIDATION.md) before using this first release. Hardware tests without an external balance do not establish gravimetric flow accuracy.

The application has no automatic updater. Download a later release manually and retain the local `data` folder.
