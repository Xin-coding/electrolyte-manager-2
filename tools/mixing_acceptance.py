"""Two 0.1 mL bench pulses through the real service; no electrolysis."""
import json
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from em2.runtime import Runtime
from em2.devices.sensors import ports
from em2.models import Settings, SignalSettings
from em2.paths import atomic_json, read_json

root = Path(__file__).resolve().parents[2]
if "--run-hardware" not in sys.argv:
    raise SystemExit("Prepared collection bench required. Pass --run-hardware to run actual injections.")
runtime = Runtime(root)
saved_settings = runtime.engine.settings.payload()
saved_limit = runtime.engine.load.limit_ml
events = []
samples = {"ph": [], "ec": []}
original_event = runtime.event
original_handle = runtime.handle
def capture(name, data):
    original_event(name, data)
    if name in {"decision", "dose_accounted", "dose_pending", "connection", "settings_applied"}:
        events.append({"kind": name, "mono": time.monotonic(), "data": data})
        print(json.dumps(events[-1]), flush=True)
def handle(name, data):
    if name == "sample":
        samples[data["kind"]].append(data)
    original_handle(name, data)
runtime.event = runtime.engine.emit = capture
runtime.handle = handle
def wait(predicate, seconds=90):
    deadline = time.monotonic()+seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        if runtime.snapshot.get("pending_reason"):
            raise RuntimeError(runtime.snapshot["pending_reason"])
        time.sleep(.1)
    raise TimeoutError("Mixing acceptance timed out")

started = time.monotonic()
try:
    for kind, port in (("pump","COM5"),("ph","COM8"),("ec","COM3")):
        endpoint = next(p for p in ports() if p["port"] == port)
        runtime.command("connect", kind=kind, port=port, identity=endpoint["identity"])
    wait(lambda: len(runtime.snapshot.get("samples", {})) >= 1 and all(runtime.snapshot.get("connections",{}).get(k,{}).get("connected") for k in ("ph","ec","pump")), 20)
    ph = runtime.snapshot["samples"]["ph"]["value"]
    settings = Settings(dose_ml=.1, flow_ml_min=1, force_percent=50, mix_s=10,
                        ph=SignalSettings(ph+.5, .05, ph-2, ph+2), metadata={"notes":"Bench sequence verification only; no electrolysis. Temporary settings."})
    runtime.command("apply", settings=settings.payload())
    runtime.command("allowance", value=runtime.snapshot["load"]["used_ml"]+.205)
    runtime.command("start_recording", name="Bench_mixing_acceptance")
    wait(lambda: runtime.snapshot.get("recording"))
    runtime.command("enable", signature=runtime.snapshot["hardware"]["signature"])
    wait(lambda: len([e for e in events if e["kind"] == "dose_accounted"]) == 2)
    runtime.stop("manual_pause")
    doses = [e["data"] for e in events if e["kind"] == "dose_accounted"]
    for d in doses:
        assert not d["result"]["error"] and abs(d["result"]["amount_ml"]-.1) <= .0021
    interval = doses[1]["result"]["evidence"]["started_at"]-doses[0]["result"]["stopped_at"]
    decisions = [e for e in events if e["kind"] == "decision" and e["data"]["demand"]]
    assert interval >= 10
    assert decisions[1]["mono"]-doses[0]["result"]["stopped_at"] >= 10
    # Keep observing enough real EC points to establish its initial target.
    wait(lambda: sum(1 for s in samples["ec"] if s["live"]) >= 3, 240)
    report = {"status":"passed", "configured_mix_s":10, "actual_stop_to_next_start_s":interval,
              "minimum_decision_wait_s":decisions[1]["mono"]-doses[0]["result"]["stopped_at"],
              "doses":doses, "sample_counts":{k:len(v) for k,v in samples.items()},
              "ec_live_times":[s["mono"] for s in samples["ec"] if s["live"]],
              "connection_events":[e for e in events if e["kind"]=="connection"],
              "database":runtime.snapshot["path"], "duration_s":time.monotonic()-started}
    atomic_json(root/"exports/mixing-acceptance.json",report)
    print(json.dumps({k:v for k,v in report.items() if k not in {"doses","connection_events"}},indent=2),flush=True)
finally:
    runtime.stop("manual_pause")
    runtime.command("apply", settings=saved_settings)
    runtime.command("finish")
    time.sleep(2)
    runtime.close()
    runtime.thread.join(15)
    ledger = read_json(root/"data/load.json", {})
    if ledger and not ledger.get("intent"):
        ledger["load"]["limit_ml"] = saved_limit
        atomic_json(root/"data/load.json",ledger)
