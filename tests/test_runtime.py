import time
from dataclasses import asdict
from em2.runtime import Runtime
from em2.models import Sample, Dose, DoseResult, Binding
from em2.storage import summary, readonly


def wait(fn, seconds=5):
    end = time.monotonic()+seconds
    while time.monotonic() < end:
        if fn():
            return
        time.sleep(.02)
    assert fn()


def test_record_without_devices_then_export_close(tmp_path):
    r = Runtime(tmp_path)
    try:
        r.command("start_recording", name="No devices required")
        wait(lambda:r.snapshot.get("recording"))
        path = r.snapshot["path"]
        r.emit("sample", {"kind":"ph","value":7.,"mono":time.monotonic(),"identity":"test","temperature":25.})
        wait(lambda:r.snapshot.get("samples",{}).get("ph"))
        r.command("finish")
        wait(lambda:not r.snapshot.get("recording"))
        assert summary(path)["status"] == "completed"
        with readonly(path) as db:
            assert db.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 1
    finally:
        r.close()
        r.thread.join(10)


def test_persistent_storage_failure_stops_without_ending_record(tmp_path):
    r = Runtime(tmp_path)
    class Pump:
        def __init__(self): self.stopped=[]
        def stop(self,reason): self.stopped.append(reason)
        def close(self): pass
        thread = type("Thread",(),{"join":lambda *args,**kw:None})()
    pump = Pump()
    try:
        r.command("start_recording",name="Failure")
        wait(lambda:r.snapshot.get("recording"))
        r.workers["pump"] = pump
        r.engine.enabled = True
        r.store.failure_since = time.monotonic()-31
        wait(lambda:bool(pump.stopped))
        wait(lambda:not r.snapshot["enabled"])
        assert r.snapshot["recording"]
        assert "storage_paused" in pump.stopped
    finally:
        r.close()
        r.thread.join(10)


def test_start_intent_is_durable_before_authorization(tmp_path):
    r = Runtime(tmp_path)
    try:
        r.command("start_recording",name="Intent")
        wait(lambda:r.snapshot.get("recording"))
        d = Dose(.1,1,50)
        r.engine.active = d
        r.engine.enabled = True
        r.engine.connected["ph"] = True
        r.confirmed_signature = "s"
        assert r.authorize(d,{"hardware":{"signature":"s"}})
        with readonly(r.store.path) as db:
            assert db.execute("SELECT COUNT(*) FROM events WHERE kind='injection_intent'").fetchone()[0] == 1
        assert r.intent["dose"]["id"] == d.id
        r.engine.active = None
    finally:
        r.close()
        r.thread.join(10)


def test_refill_cancels_pending_allowance_from_previous_load(tmp_path):
    r = Runtime(tmp_path)
    try:
        r.engine.connected["pump"] = True
        r.pump_status = {"state":3,"error":0}
        r.bindings["pump"] = Binding("pump","unused","test-pump")
        r.engine.ready_at = time.monotonic()+20
        r.command("allowance",value=10.)
        wait(lambda:r.snapshot.get("pending_allowance")==10.)
        r.command("allowance",value=20.,refill=True)
        wait(lambda:r.snapshot.get("load",{}).get("limit_ml")==20.)
        assert r.snapshot["pending_allowance"] is None
    finally:
        r.close()
        r.thread.join(10)
