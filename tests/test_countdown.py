"""Deterministic countdown acceptance; physical serial is blocked by conftest."""
from dataclasses import asdict
import json
import threading

import pytest

from em2.runtime import Runtime
from em2.models import Settings, SignalSettings, Binding
from em2.countdown import duration, status_text
from em2.storage import readonly


class Clock:
    now = 1000.
    def __call__(self):
        return self.now


@pytest.fixture
def runtime(tmp_path):
    clock = Clock()
    r = Runtime(tmp_path, autostart=False, clock=clock)
    r.engine.settings = Settings(smoothing=False, dose_ml=.1, flow_ml_min=1, force_percent=50, mix_s=20,
                                ph=SignalSettings(7, .1, 5, 9), ec=SignalSettings(7, .1, 5, 9))
    r.dispatch("start_recording", {"name": "Countdown acceptance"})
    r.engine.connected = dict.fromkeys(r.engine.connected, True)
    r.engine.hardware_ready = True
    r.bindings = {"pump": Binding("pump", "SIM1", "pump-one"), "ec": Binding("ec", "SIM2", "meter", driver="m80")}
    r.hardware = {"signature": "syringe-one", "capacity_ml": 10, "area_mm2": 20, "force_percent": 50}
    r.pump_status = {"state": 3, "error": 0, "read_at": clock()}
    r.engine.set_allowance(2, True, "pump-one")
    try:
        yield r, clock
    finally:
        r.engine.active = None
        if r.store:
            r.store.error = ""
            r.store.failure_since = None
            r.store.close("completed")
        from em2.paths import keep_awake
        keep_awake(False)
        for handler in list(r.log.handlers):
            handler.close(); r.log.removeHandler(handler)


def arm(r, seconds=60):
    r.dispatch("schedule_feedback", {"delay_s": seconds, "context": r.countdown_context(), "signature": "syringe-one"})


def reading(r, clock, *, age=0, **kwargs):
    r.pump_status["read_at"] = clock()
    r.handle("sample", {"kind": r.engine.settings.control, "value": 6.8, "mono": clock()-age, **kwargs})


def events(r, kind):
    r.store.event(0, "test_barrier", {}, critical=True)
    with readonly(r.store.path) as db:
        return [json.loads(row[0]) for row in db.execute("SELECT payload FROM events WHERE kind=?", (kind,))]


def test_hours_record_only_once_and_new_sample_gate(runtime, monkeypatch):
    r, clock = runtime
    r.engine.settings.metadata["current_a"] = 2
    r.charge.begin(clock(), 2)
    arm(r, 6*3600)
    expected = r.countdown["expected_at"]
    monkeypatch.setattr("em2.runtime.time.time", lambda: 1.)  # wall clock adjustment
    for step in (60, 3600, 5*3600, 6*3600-1):
        clock.now = 1000+step
        reading(r, clock)
        r.check_countdown()
        assert r.engine.recording and not r.engine.enabled
        assert r.engine.tick(clock()) is None
        assert r.countdown["expected_at"] == expected
    assert r.charge.snapshot(clock())["coulombs"] == (6*3600-1)*2
    clock.now = 1000+6*3600
    reading(r, clock)
    r.check_countdown()
    assert r.engine.enabled and r.countdown["state"] == "started"
    assert r.engine.tick(clock()) is None
    reading(r, clock, age=.01)  # queued sample taken before activation
    assert r.engine.tick(clock()) is None
    reading(r, clock)  # equal timestamp is not a post-activation reading either
    assert r.engine.tick(clock()) is None
    clock.now += .1
    reading(r, clock)
    assert r.engine.tick(clock()) is not None
    r.engine.active = None
    r.check_countdown()
    assert len(events(r, "feedback_countdown_started")) == 1
    assert len(events(r, "feedback_countdown_created")) == 1
    with readonly(r.store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 8


@pytest.mark.parametrize("reason,change", [
    ("pump", lambda r,c: r.engine.connected.update(pump=False)),
    ("pump", lambda r,c: r.pump_status.update(error=1)),
    ("pump", lambda r,c: r.pump_status.update(read_at=c()-6)),
    ("signal", lambda r,c: r.engine.connected.update(ph=False)),
    ("signal", lambda r,c: reading(r,c, quality="invalid_measurement")),
    ("signal", lambda r,c: reading(r,c, live=False)),
    ("stale", lambda r,c: reading(r,c, age=10)),
    ("allowance", lambda r,c: setattr(r.engine.load, "used_ml", 2)),
    ("allowance", lambda r,c: setattr(r.engine.load, "uncertain", True)),
    ("storage", lambda r,c: setattr(r.store, "error", "disk full")),
    ("storage", lambda r,c: setattr(r, "local_storage_error", "ledger unavailable")),
    ("recording", lambda r,c: setattr(r.engine, "recording", False)),
])
def test_deadline_failure_persists_without_retry(runtime, reason, change):
    r,c = runtime
    arm(r); c.now += 60; reading(r,c); change(r,c)
    r.check_countdown()
    assert r.countdown["state"] == "failed" and r.countdown["reason"] == reason
    assert not r.engine.enabled
    r.engine.connected = dict.fromkeys(r.engine.connected, True)
    r.pump_status.update(error=0)
    r.store.error = r.local_storage_error = ""
    r.engine.load.used_ml = 0; r.engine.load.uncertain = False
    c.now += 60; reading(r,c); r.check_countdown()
    assert not r.engine.enabled and r.countdown["state"] == "failed"
    assert len(events(r, "feedback_countdown_failed")) == 1
    assert "反馈未启动" in status_text(r.countdown_snapshot())


@pytest.mark.parametrize("driver,kind,period", [("m80", "ec",1), ("apera_pc8500","ec",60), ("m80","ph",30)])
@pytest.mark.parametrize("extra,started", [(0,True),(.01,False)])
def test_exact_freshness_boundary(runtime, driver,kind,period,extra,started):
    r,c=runtime
    r.engine.settings.control=kind
    r.engine.settings.ph_interval_s=period
    r.bindings["ec"]=Binding("ec","SIM2","meter",driver=driver)
    arm(r); c.now+=300 if period==60 else 60
    # Preserve a legitimate deadline while testing a long instrument cycle.
    r.countdown["deadline"]=c()
    reading(r,c,age=2*period+5+extra); r.check_countdown()
    assert r.engine.enabled == started


@pytest.mark.parametrize("late,started", [(0,True),(5,True),(5.001,False),(7200,False)])
def test_suspend_deadline_grace(runtime, late,started):
    r,c=runtime; arm(r); c.now+=60+late; reading(r,c); r.check_countdown()
    assert r.engine.enabled == started
    if not started:
        assert r.countdown["reason"]=="missed"


def test_service_suspend_inside_activation_does_not_start_late(runtime,monkeypatch):
    r,c=runtime; arm(r); c.now+=60; reading(r,c)
    original=r.engine.enable
    def suspended(**kwargs):
        original(**kwargs); c.now+=6
    monkeypatch.setattr(r.engine,"enable",suspended)
    r.check_countdown()
    assert not r.engine.enabled and r.countdown["reason"]=="missed"


def test_expected_local_time_tracks_wall_adjustment_not_deadline(runtime,monkeypatch):
    r,c=runtime; arm(r)
    deadline=r.countdown["deadline"]
    monkeypatch.setattr("em2.runtime.time.time",lambda:100.)
    assert r.countdown_snapshot()["expected_at"]==160.
    c.now+=10
    assert r.countdown_snapshot()["expected_at"]==150.
    assert r.countdown["deadline"]==deadline


@pytest.mark.parametrize("action", ["cancel_countdown","finish","enable","pause","stop"])
def test_priority_cancel_beats_due_and_queue_backlog(runtime, action):
    r,c=runtime; arm(r); c.now+=60; reading(r,c)
    if action=="stop": r.stop()
    else: r.command(action, reservation_id=r.countdown["id"])
    r.check_countdown()
    assert not r.engine.enabled and r.countdown["state"]=="cancelled"


def test_cancel_during_enable_callback_cannot_dose(runtime, monkeypatch):
    r,c=runtime; arm(r); c.now+=60; reading(r,c)
    original=r.engine.enable
    def racing(**kwargs):
        original(**kwargs)
        r.command("cancel_countdown", reservation_id=r.countdown["id"])
    monkeypatch.setattr(r.engine,"enable",racing)
    r.check_countdown()
    assert not r.engine.enabled and r.countdown["state"]=="cancelled"
    assert r.engine.tick(c()) is None


def test_cancel_from_pending_view_after_activation_stops_feedback(runtime):
    r,c=runtime; arm(r); ident=r.countdown["id"]; c.now+=60; reading(r,c); r.check_countdown()
    r.command("cancel_countdown",reservation_id=ident)
    r.apply_priority_stop()
    assert not r.engine.enabled


@pytest.mark.parametrize("change", ["settings","sampling","refill","allowance","hardware","pump","syringe_note"])
def test_confirmation_changes_cancel(runtime,change):
    r,c=runtime; arm(r)
    if change in {"settings","sampling","syringe_note"}:
        s=r.engine.settings.payload()
        if change=="settings": s["dose_ml"] = .2
        elif change=="sampling": s["ph_interval_s"]=3
        else: s["metadata"]["syringe_note"]="replacement"
        r.dispatch("apply",{"settings":s})
    elif change in {"allowance","refill"}:
        r.dispatch("allowance",{"value":3,"refill":change=="refill"})
    elif change=="hardware":
        r.handle("pump_hardware", {**r.hardware,"signature":"changed"})
    else:
        r.bindings["pump"]=Binding("pump","SIM3","pump-two")
    r.check_countdown()
    assert r.countdown["state"]=="cancelled" and r.countdown["reason"]=="changed"


def test_metadata_current_do_not_cancel_and_auto_target_retained(runtime):
    r,c=runtime
    r.engine.settings.ph.target=None
    for _ in range(5):
        reading(r,c); c.now+=2
    target=r.engine.auto_target.copy()
    arm(r)
    s=r.engine.settings.payload(); s["metadata"].update(notes="first phase",current_a=2)
    r.dispatch("apply",{"settings":s}); r.check_countdown()
    assert r.countdown["state"]=="pending"
    c.now=r.countdown["deadline"]; reading(r,c); r.check_countdown()
    assert r.engine.enabled and r.engine.auto_target==target


@pytest.mark.parametrize("delay", [0,59,61,3600000,60.,True])
def test_invalid_delay(runtime,delay):
    r,c=runtime
    with pytest.raises(ValueError): arm(r,delay)
    assert r.countdown["state"]=="idle"


def test_max_delay_and_no_restore(runtime,tmp_path):
    r,c=runtime; arm(r,999*3600+59*60)
    assert duration(r.countdown_snapshot()["remaining_s"])=="999:59:00"
    r.persist_ledger()
    fresh=Runtime(tmp_path,autostart=False)
    assert fresh.countdown=={"state":"idle"} and not fresh.engine.enabled
    assert "countdown" not in fresh.config_file.read_text(encoding="utf-8") if fresh.config_file.exists() else True


def test_stale_confirmation_and_stop_generation_rejected(runtime):
    r,c=runtime
    data={"delay_s":60,"context":r.countdown_context(),"signature":"syringe-one"}
    r.engine.settings.dose_ml=.2
    with pytest.raises(ValueError): r.dispatch("schedule_feedback",data)
    data["context"]=r.countdown_context(); data["_stop_generation"]=r.stop_generation
    r.stop(); r.apply_priority_stop(); r.dispatch("schedule_feedback",data)
    assert r.countdown["state"]=="idle"


def test_side_effect_free_check_and_manual_enable(runtime):
    r,c=runtime
    before=r.engine.settings.payload()
    r.engine.validate_enable()
    assert not r.engine.enabled and before==r.engine.settings.payload()
    arm(r); r.dispatch("enable",{"signature":"syringe-one"})
    assert r.engine.enabled and r.countdown["state"]=="cancelled"


@pytest.mark.parametrize("change", ["idle", "enabled", "busy", "disconnected", "no_allowance", "storage", "invalid_settings"])
def test_cannot_arm_when_conditions_fail(runtime,change):
    r,c=runtime
    if change=="idle": r.engine.recording=False
    elif change=="enabled": r.engine.enabled=True
    elif change=="busy": r.pump_status["state"]=1
    elif change=="disconnected": r.engine.connected["pump"]=False
    elif change=="no_allowance": r.engine.load.limit_ml=0
    elif change=="storage": r.local_storage_error="disk full"
    else: r.engine.settings.dose_ml=None
    with pytest.raises(ValueError): arm(r)
    assert r.countdown["state"]=="idle"


def test_cancel_in_another_thread_while_activation_is_in_progress(runtime,monkeypatch):
    r,c=runtime; arm(r); c.now+=60; reading(r,c)
    entering=threading.Event(); released=threading.Event()
    original=r.engine.enable
    def delayed(**kwargs):
        original(**kwargs); entering.set(); assert released.wait(3)
    monkeypatch.setattr(r.engine,"enable",delayed)
    worker=threading.Thread(target=r.check_countdown); worker.start()
    assert entering.wait(3)
    r.command("cancel_countdown",reservation_id=r.countdown["id"])
    released.set(); worker.join(3)
    assert not worker.is_alive() and not r.engine.enabled
    assert r.countdown["state"]=="cancelled"


def test_cannot_replace_pending_or_test_pump(runtime):
    r,c=runtime; arm(r)
    with pytest.raises(ValueError): arm(r,120)
    with pytest.raises(ValueError,match="Cancel"): r.dispatch("test",{})
    assert r.countdown["delay_s"]==60


def test_countdown_ui_confirmation_and_cancel(runtime,monkeypatch):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM","offscreen")
    from PySide6 import QtWidgets as W
    from em2.ui import Window
    app=W.QApplication.instance() or W.QApplication([])
    r,c=runtime; r.confirmed_signature="syringe-one"; r.publish()
    window=Window(r); window.timer.stop()
    questions=[]
    def cancel(*args):
        questions.append(args[2]); return W.QMessageBox.StandardButton.Cancel
    monkeypatch.setattr(W.QMessageBox,"question",cancel)
    try:
        window.delay_hours.setValue(3); window.delay_minutes.setValue(15)
        window.schedule_control()
        assert "03:15:00" in questions[-1] and r.commands.empty()
        monkeypatch.setattr(W.QMessageBox,"question",lambda *args: W.QMessageBox.StandardButton.Ok)
        window.schedule_control()
        action,data=r.commands.get_nowait(); r.dispatch(action,data)
        assert r.countdown["state"]=="pending"
        r.publish(); window.refresh()
        assert not window.delay_hours.isEnabled() and window.cancel_schedule_button.isEnabled()
        assert "03:15:00" in window.countdown_status.text()
        window.navigation.setCurrentRow(0); window.navigation.setCurrentRow(3)
        c.now=r.countdown["deadline"]; reading(r,c)
        monkeypatch.setattr(W.QMessageBox,"question",lambda *args: pytest.fail("No expiry dialog"))
        r.check_countdown(); assert r.engine.enabled
        r.publish(); window.switch_language(1); window.refresh()
        assert "Scheduled feedback enabled" in window.countdown_status.text()
    finally:
        r.engine.recording=False; r.publish(); window.close(); app.processEvents()
