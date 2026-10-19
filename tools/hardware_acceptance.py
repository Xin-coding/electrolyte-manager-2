"""Explicit small-dose bench acceptance, never run by normal app startup."""
import json
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from em2.runtime import Runtime
from em2.devices.sensors import ports
from em2.paths import atomic_json

root = Path(__file__).resolve().parents[2]
if "--run-hardware" not in sys.argv:
    raise SystemExit("Prepared collection bench required. Pass --run-hardware to run actual injections.")
runtime = Runtime(root)
events = []
original = runtime.event
def capture(name, data):
    original(name, data)
    if name in {"dose_accounted", "dose_pending", "pump_phase"}:
        events.append({"kind": name, "data": data})
        print(json.dumps(events[-1]), flush=True)
runtime.event = capture
runtime.engine.emit = capture

def wait(predicate, seconds=60):
    deadline = time.monotonic()+seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        if runtime.snapshot.get("pending_reason"):
            raise RuntimeError(runtime.snapshot["pending_reason"])
        time.sleep(.1)
    raise TimeoutError("Acceptance stage timed out: "+str(runtime.snapshot.get("notices", [])[-1:]))

try:
    endpoint = next(p for p in ports() if p["port"] == "COM5")
    runtime.command("connect", kind="pump", port=endpoint["port"], identity=endpoint["identity"])
    wait(lambda: runtime.snapshot.get("connections", {}).get("pump", {}).get("connected"), 15)
    signature = runtime.snapshot["hardware"]["signature"]
    initial_counter = runtime.snapshot["pump"]["counter_ml"]
    cases = ((.05,"stop_test",.2),) if "--stop-only" in sys.argv else ((.1,"test",None),(.1,"test",None),(.05,"stop_test",.2))
    for i, (volume, purpose, cap) in enumerate(cases):
        runtime.command("test", signature=signature, volume_ml=volume, flow_ml_min=1., force_percent=50, purpose=purpose, hardware_cap_ml=cap)
        wait(lambda: len([e for e in events if e["kind"] == "dose_accounted"]) > i, 60)
        result = [e for e in events if e["kind"] == "dose_accounted"][-1]["data"]["result"]
        if result["error"] or result["amount_ml"] is None:
            raise RuntimeError("Unverified test: "+str(result))
        if purpose == "stop_test" and not result["evidence"].get("stop_test_passed"):
            raise RuntimeError("Software STOP evidence did not pass")
        time.sleep(.5)
    report = {"status": "passed", "initial_counter_ml": initial_counter,
              "final_counter_ml": runtime.snapshot["pump"]["counter_ml"], "events": events,
              "test_database": runtime.snapshot["test_path"],
              "limits": "Instrument counter and timing evidence only; no external gravimetric flow measurement."}
    atomic_json(root/("exports/hardware-stop-counter.json" if "--stop-only" in sys.argv else "exports/hardware-acceptance.json"), report)
    print("HARDWARE ACCEPTANCE PASSED", flush=True)
except Exception as exc:
    atomic_json(root/"exports/hardware-acceptance.json", {"status": "failed", "error": str(exc), "events": events})
    raise
finally:
    runtime.close()
    runtime.thread.join(15)
