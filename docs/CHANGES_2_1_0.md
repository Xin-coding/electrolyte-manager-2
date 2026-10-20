# 2.1.0 — Historical injection curves and experiment reports

Historical pump injection comparison opens in a separate Data manager dialog. Select up to 12 EM2 experiments and use elapsed or absolute time. Saved formal cumulative volume survives refills; test-only records use their own total. Cropped views retain the volume before their start, and the final plateau extends to experiment end. Unknown values remain gaps and estimated entries are identified. Steps now change at their recorded completion time, rather than at the preceding timestamp.

Ending a recording waits for final injection accounting and an acknowledged database completion transaction, then exports three PNG charts and three full-data CSVs. Files are named `<experiment>_pH`, `<experiment>_cond`, and `<experiment>_pump injection`, in `exports/<database stem>/`. A source marker prevents overwriting a different experiment with the same filename. Images cover the whole experiment regardless of live chart settings. CSV exports always contain all rows. Failed exports show a specific error and can be retried; historical **Export full report** provides the same output.

Normal window closure is rejected while a recording or finalization is active. This rejected close does not issue STOP. Users must end the experiment first. Closure waits for report export; a failed export does not prevent later closure or historical retry. Unexpected termination still cannot guarantee a completed report.

The monitor replaces the pump status card with estimated charge, `Q = Σ I Δt`, based on declared current and monotonic recording time. Changes apply to future intervals immediately, including while pump settings await their next cycle. Pump pauses do not pause charge. Missing current is not treated as zero; partial coverage is marked. Signed and zero currents are supported, invalid drafts retain effective settings, and arithmetic overflow cannot prevent database finalization. Charge is retained in completed metadata.

The receive-status line moves above the charts; technical START/accounting details remain in the pump popup. The bottom raw log box is removed, but events remain stored. Signal plots auto-range from visible signal data, excluding target/tolerance/alarm lines. The time axis remains in minutes rather than automatically becoming kmin.

Existing 2.0.x SQLite data is read without migration or modification. Added duration/charge metadata is optional for historical readers. The compatibility check used an existing completed file with 355 injection records, 35.496 mL accounted total, and two refill events; no raw data is included in this repository or release.

Hardware communication modules, pump execution, and feedback decision logic are unchanged. This release was tested offline without opening serial ports or driving hardware. Earlier uncompleted endurance, gravimetric, and physical fault-injection validation remains uncompleted.
