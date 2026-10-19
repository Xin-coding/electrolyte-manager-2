"""Explicit 2.0.1 bench regression; two 0.1 mL feedback doses plus STOP test.

Only run on an authorized collection bench. Maximum commanded total 0.4 mL.
Preserves the existing load journal, charges actual delivery, restores settings.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6 import QtCore as C, QtWidgets as W
from em2.devices.pump import PumpTransport
from em2.devices.sensors import ports
from em2.models import Settings, SignalSettings
from em2.paths import atomic_json, read_json
from em2.runtime import Runtime
from em2.ui import Window


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-hardware", action="store_true", required=True)
    for kind in ("pump", "ph", "ec"):
        parser.add_argument(f"--{kind}-port", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    report_path = root/"exports/regression-2.0.1-hardware.json"
    app = W.QApplication([])
    lock = C.QLockFile(str(root/"data/application.lock")); lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        raise RuntimeError("An application is already using this workspace")
    r = None
    w = None
    report = {"status":"in_progress", "maximum_commanded_ml":.4, "events":[], "samples":{"ph":[],"ec":[]}}
    saved_settings = read_json(root/"data/settings.json", {})
    saved_ledger = read_json(root/"data/load.json", {})
    if saved_ledger.get("intent") or saved_ledger.get("load",{}).get("uncertain"):
        raise RuntimeError("Resolve the existing dose before bench testing")
    saved_limit = saved_ledger.get("load",{}).get("limit_ml")
    started = time.monotonic()
    try:
        # Read-only preflight: an already running pump must not be stopped here.
        p = PumpTransport(args.pump_port)
        try:
            p.open(); status=p.status(); hardware=p.hardware()
        finally:
            p.close()
        if status["state"] != 3 or status["error"]:
            raise RuntimeError("Pump is not stopped and fault-free; no test was started")
        report["before"]={"status":status,"hardware":hardware}
        r=Runtime(root); w=Window(r)
        original_event,original_handle=r.event,r.handle
        def capture(name,data):
            original_event(name,data)
            if name in {"decision","dose_accounted","dose_pending","connection","settings_applied"}:
                report["events"].append({"kind":name,"mono":time.monotonic(),"data":data})
                if name in {"dose_accounted","dose_pending"}:
                    print(json.dumps({"stage":name,"data":data}),flush=True)
        def handle(name,data):
            if name == "sample": report["samples"][data["kind"]].append(data)
            original_handle(name,data)
        r.event=r.engine.emit=capture; r.handle=handle
        def wait(predicate, seconds=60, checking=True):
            end=time.monotonic()+seconds
            while time.monotonic()<end:
                app.processEvents()
                if checking and r.snapshot.get("last_failure"):
                    raise RuntimeError(str(r.snapshot["last_failure"]))
                if predicate(): return
                time.sleep(.05)
            raise TimeoutError(str(r.snapshot.get("notices",[])[-3:]))
        inventory=ports()
        for kind in ("pump","ph","ec"):
            endpoint=next(p for p in inventory if p["port"]==getattr(args,f"{kind}_port"))
            if kind == "pump" and saved_ledger.get("load",{}).get("identity") not in (None,"",endpoint["identity"]):
                raise RuntimeError("Load journal belongs to another pump")
            r.command("connect",kind=kind,port=endpoint["port"],identity=endpoint["identity"])
        wait(lambda: all(r.snapshot.get("connections",{}).get(k,{}).get("connected") for k in ("pump","ph","ec")) and "ph" in r.snapshot.get("samples",{}),25)
        ph=r.snapshot["samples"]["ph"]["value"]
        cfg=Settings(dose_ml=.1,flow_ml_min=5,force_percent=hardware["force_percent"],mix_s=10,
                     ph=SignalSettings(ph+.5,.05,ph-2,ph+2),
                     metadata={"notes":"Temporary collection-bench regression, not an electrolysis protocol."})
        w.populate_settings(cfg.payload()); w.fields["force_percent"].setText(f"{hardware['force_percent']}.0")
        profile=root/"exports/regression-2.0.1-profile.json"
        atomic_json(profile,{"format":"em2-settings-1","settings":w.collect_settings()})
        w.populate_settings(Settings.from_dict(read_json(profile,{})["settings"]).payload())
        w.apply_settings()
        wait(lambda:r.snapshot["settings"]["dose_ml"]==.1)
        assert type(r.snapshot["settings"]["force_percent"]) is int
        initial_used=r.snapshot["load"]["used_ml"]
        # A third complete feedback pulse cannot fit even with counter tolerance.
        r.command("allowance",value=initial_used+.205)
        r.command("start_recording",name="Bench_2_0_1_formal_regression")
        wait(lambda:r.snapshot.get("recording"))
        report["database"]=r.snapshot["path"]
        r.command("enable",signature=r.snapshot["hardware"]["signature"])
        def doses(): return [x for x in report["events"] if x["kind"]=="dose_accounted"]
        wait(lambda:len(doses())>=2,90)
        r.stop("manual_pause")
        wait(lambda:not r.snapshot["enabled"] and not r.snapshot["active"])
        formal=[x for x in doses() if x["data"]["dose"]["purpose"]=="experiment"]
        assert len(formal)==2
        for x in formal:
            result=x["data"]["result"]
            assert not result["error"] and result["stopped"] and abs(result["amount_ml"]-.1)<=.0021
            assert result["source"]=="device_counter"
        decisions=[x for x in report["events"] if x["kind"]=="decision" and x["data"]["demand"]]
        report["decision_wait_s"]=decisions[1]["mono"]-formal[0]["data"]["result"]["stopped_at"]
        report["stop_to_next_start_s"]=formal[1]["data"]["result"]["evidence"]["started_at"]-formal[0]["data"]["result"]["stopped_at"]
        assert report["decision_wait_s"]>=10 and report["stop_to_next_start_s"]>=10
        wait(lambda:r.snapshot["mix_remaining"]==0,20)
        r.command("allowance",value=r.snapshot["load"]["used_ml"]+.2)
        wait(lambda:r.snapshot["remaining"]>=.1999999)
        w.test_volume.setText("0.05"); w.test_flow.setText("1")
        w.test_force.setText(f"{hardware['force_percent']}.0"); w.test_cap.setText("0.2")
        r.command("test",signature=r.snapshot["hardware"]["signature"],**w.test_data(True))
        wait(lambda:len(doses())==3,60)
        stopped=doses()[-1]["data"]["result"]
        assert stopped["evidence"]["stop_test_passed"] and not stopped["error"]
        # Acquisition remains independent: obtain at least two real live EC frames.
        wait(lambda:sum(bool(x["live"]) for x in report["samples"]["ec"])>=2,180)
        report.update(status="passed",duration_s=time.monotonic()-started,
                      actual_total_ml=sum(x["data"]["result"]["amount_ml"] for x in doses()),
                      sample_counts={k:len(v) for k,v in report["samples"].items()},
                      after=r.snapshot["pump"],load_after=r.snapshot["load"],
                      limitations="Device-counter evidence only; no external balance, electrolysis or physical fault injection.")
        assert report["actual_total_ml"]<=.4
    except Exception as exc:
        report.update(status="failed",error=str(exc))
        raise
    finally:
        if r:
            r.stop("manual_pause")
            if not r.snapshot.get("active"):
                r.command("apply",settings=saved_settings)
                r.command("finish")
                end=time.monotonic()+5
                while time.monotonic()<end and (r.snapshot.get("recording") or r.snapshot.get("enabled")):
                    app.processEvents();time.sleep(.05)
            r.close();r.thread.join(20)
            if w: w.close();app.processEvents()
            ledger=read_json(root/"data/load.json",{})
            if not r.thread.is_alive() and ledger and not ledger.get("intent") and not ledger["load"]["uncertain"]:
                ledger["load"]["limit_ml"]=saved_limit
                atomic_json(root/"data/load.json",ledger)
                report["restored_allowance"]=True
        atomic_json(report_path,report)
        lock.unlock()
        print(json.dumps({k:v for k,v in report.items() if k not in {"events","samples","before","after","load_after"}},indent=2),flush=True)


if __name__ == "__main__": main()
