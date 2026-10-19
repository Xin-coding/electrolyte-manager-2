"""Fault injection through executor/service paths. Real serial ports are forbidden."""
from dataclasses import asdict
import json
import time
from types import SimpleNamespace

import pytest
import serial

import em2.devices.pump as pump_module
import em2.runtime as runtime_module
from em2.devices.pump import PumpWorker, PumpTransport
from em2.engine import Engine
from em2.errors import ConfigurationError
from em2.models import Binding, Dose, DoseResult, Load, Sample, Settings, SignalSettings
from em2.runtime import Runtime
from em2.storage import Store, readonly


@pytest.fixture(autouse=True)
def no_real_hardware(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Boundary regressions must never open a real serial port")
    monkeypatch.setattr(serial, "Serial", forbidden)


class Clock:
    def __init__(self): self.t = 100.
    def monotonic(self): return self.t
    def sleep(self, seconds): self.t += seconds


class Flag:
    def __init__(self, clock): self.clock, self.value = clock, False
    def set(self): self.value = True
    def clear(self): self.value = False
    def is_set(self): return self.value
    def wait(self, seconds): self.clock.sleep(seconds); return self.value


class Pump:
    """Models delayed motion and independently delayed counter publication."""
    def __init__(self, clock, start_delay=.65, counter_delay=.8):
        self.clock, self.start_delay, self.counter_delay = clock, start_delay, counter_delay
        self.start_at = self.stop_at = None
        self.last_tx = 0.
        self.starts, self.stops, self.verifications = 0, 0, 0
        self.trace = []
        self.signature = "syringe"
        self.error = 0
        self.lost_start_reply = False
        self.fail_queries = 0
        self.fail_after_start = False
        self.offline = False
        self.counter_reset = False
        self.speed_factor = 1.
        self.cap_fraction = 1.
        self.dose = None

    def hardware(self, emergency=False):
        if self.offline: raise TimeoutError("offline hardware")
        return {"signature": self.signature, "capacity_ml": 60, "force_percent": 50}

    def configure_dose(self, dose, signature):
        self.dose = dose
        self.clock.sleep(8.)
        return self.verify_dose(dose, signature)

    def verify_dose(self, dose, signature, emergency=False):
        if self.offline: raise TimeoutError("offline parameters")
        self.verifications += 1
        if self.signature != signature:
            raise ConfigurationError("changed syringe")
        return {"hardware": self.hardware(emergency), "expected_duration_s": dose.expected_s}

    def write(self, register, value, emergency=False):
        self.last_tx = self.clock.t
        self.trace.append("START" if value else "STOP")
        if self.offline: raise TimeoutError("offline write")
        if value:
            self.starts += 1
            self.start_at = self.clock.t
            self.stop_at = None
            if self.lost_start_reply: raise TimeoutError("START reply lost")
        else:
            self.stops += 1
            if self.stop_at is None: self.stop_at = self.clock.t

    def status(self, emergency=False):
        self.trace.append("status")
        if self.offline: raise TimeoutError("offline query")
        if self.fail_queries:
            self.fail_queries -= 1
            raise TimeoutError("single query timeout")
        if self.start_at is not None and self.fail_after_start and not emergency:
            self.fail_after_start = False
            self.clock.sleep(.5)
            raise TimeoutError("transient running query timeout")
        now, state, amount = self.clock.t, 3, 0.
        if self.start_at is not None:
            motion = self.start_at+self.start_delay
            actual_flow = self.dose.flow_ml_min*self.speed_factor
            end = motion+self.dose.cap*self.cap_fraction/actual_flow*60
            if self.stop_at is not None: end = min(end, self.stop_at)
            state = 1 if motion <= now < end else 3
            if state == 1:
                amount = (now-motion)*actual_flow/60
            elif now >= end+self.counter_delay:
                amount = max(0., end-motion)*actual_flow/60
        return dict(state=state, error=self.error, read_at=now,
                    counter_ml=0. if self.counter_reset and self.start_at is not None else 40+amount,
                    resolution_ml=.001, flow_ml_min=self.dose.flow_ml_min*self.speed_factor if state == 1 else 0.)


def worker(monkeypatch, **kwargs):
    clock = Clock()
    monkeypatch.setattr(pump_module, "time", clock)
    events = []
    w = PumpWorker(Binding("pump", "unused", "offline-id"), lambda n,d: events.append((n,d)), lambda *a: True)
    w.stop_event = Flag(clock)
    w.transport = Pump(clock, **kwargs)
    w.hardware = w.transport.hardware()
    w.connected = True
    w.status = w.transport.status()
    return w, clock, events


def results(events): return [d for n,d in events if n == "dose_result"]


def configured():
    return Settings(dose_ml=.1, flow_ml_min=5, force_percent=50, mix_s=60,
                    ph=SignalSettings(7,.1,5,9), ec=SignalSettings(7,.1,5,9))


def test_stop_sent_before_query_and_retried_if_query_fails(monkeypatch):
    w, clock, events = worker(monkeypatch)
    w.transport.trace.clear()
    w.transport.fail_queries = 1
    assert w.ensure_stopped()["state"] == 3
    assert w.transport.trace == ["STOP", "status", "STOP", "status"]


def test_lost_start_ack_stops_even_with_cached_stopped_status(monkeypatch):
    w, clock, events = worker(monkeypatch)
    w.transport.lost_start_reply = True
    w.execute(Dose(.1,5,50))
    assert w.transport.starts == 1 and w.transport.stops >= 1
    r = results(events)[0]
    assert r["stopped"] and r["recovered"] and r["amount_ml"] == .1
    assert r["source"] == "programmed_cap_estimate"
    assert not r["error"]
    assert "START reply lost" in r["evidence"]["recovered_error"]
    assert w.transport.trace.index("STOP") > w.transport.trace.index("START")


def test_stop_invalidates_already_queued_test_after_stop_flag_cleared(monkeypatch):
    w, clock, events = worker(monkeypatch)
    w.submit(Dose(.1,5,50,purpose="test"))
    w.stop("emergency_stop")
    w.service_stop()
    assert not w.stop_event.is_set()
    kind, payload = w.queue.get_nowait()
    w.execute(*payload)
    assert w.transport.starts == 0
    assert results(events)[0]["started"] is False
    assert results(events)[0]["amount_ml"] == 0
    # Only a newly submitted user request is eligible after the stop.
    w.submit(Dose(.1,5,50,purpose="test"))
    w.execute(*w.queue.get_nowait()[1])
    assert w.transport.starts == 1


@pytest.mark.parametrize("delay,volume,speed", [(.65,.1,5),(1.5,.1,5),(.1,.001,60)])
def test_delayed_start_and_unobserved_short_pulse(monkeypatch, delay, volume, speed):
    w, clock, events = worker(monkeypatch, start_delay=delay, counter_delay=.3)
    w.execute(Dose(volume,speed,50))
    assert w.context is None
    r = results(events)[0]
    assert not r["error"] and r["amount_ml"] == pytest.approx(volume)
    assert w.transport.starts == 1


def test_transient_error_recovers_without_manual_pause_and_keeps_full_mix(monkeypatch):
    w, clock, events = worker(monkeypatch, start_delay=0, counter_delay=.2)
    w.transport.fail_after_start = True
    e = Engine(configured(), Load(limit_ml=10))
    e.begin(); e.enabled = True
    e.active = Dose(.1,5,50,60)
    w.execute(e.active)
    r = DoseResult(**results(events)[0])
    assert 0 < r.amount_ml < .1 and r.recovered and not r.error
    assert w.transport.verifications == 2
    e.complete(r)
    assert e.enabled and not e.manual_reason
    assert e.ready_at == r.stopped_at+60


@pytest.mark.parametrize("reason", ["manual_pause", "emergency_stop", "storage_paused", "pump_fault"])
def test_successful_reconciliation_never_clears_existing_manual_pause(reason):
    e = Engine(configured(), Load(limit_ml=10))
    e.active = Dose(.1,5,50,60)
    e.pause(reason)
    e.complete(DoseResult(e.active.id,True,10,.04,"device_counter",interrupted=True,recovered=True))
    assert not e.enabled and e.manual_reason == reason


def test_disconnect_during_preparation_resolves_without_start_or_stuck_task(monkeypatch):
    w, clock, events = worker(monkeypatch)
    def lose_connection(*args):
        w.transport.offline = True
        raise TimeoutError("lost during preparation")
    w.transport.configure_dose = lose_connection
    with pytest.raises(Exception, match="stop is unconfirmed"):
        w.execute(Dose(.1,5,50))
    assert w.context["signature"] == "syringe" and not w.context["started"]
    w.transport.offline = False
    w.connected = True
    w.reconcile(reconnected=True)
    assert not w.context and not w.busy and w.transport.starts == 0
    r = results(events)[0]
    assert r["amount_ml"] == 0 and r["source"] == "not_started" and not r["error"]


def test_reconnect_changed_syringe_stays_pending_without_false_disconnect(monkeypatch):
    w, clock, events = worker(monkeypatch)
    def lose_connection(*args):
        w.transport.offline = True
        raise TimeoutError("lost during preparation")
    w.transport.configure_dose = lose_connection
    with pytest.raises(Exception): w.execute(Dose(.1,5,50))
    w.transport.offline = False; w.transport.signature = "different"; w.connected = True
    w.reconcile(reconnected=True)
    assert w.context and w.connected and not results(events)
    assert events[-1][1]["error_kind"] == "configuration_error"


def test_final_counter_late_publication_is_estimated_without_blocking_feedback(monkeypatch):
    w, clock, events = worker(monkeypatch, counter_delay=5.)
    w.execute(Dose(.1,5,50))
    assert not w.context and w.connected
    assert results(events)[0]["amount_ml"] == pytest.approx(.1)
    assert not results(events)[0]["error"]
    assert results(events)[0]["source"] == "programmed_cap_estimate"
    assert results(events)[0]["warnings"]
    assert w.transport.starts == 1


def test_missing_final_count_does_not_hold_task_for_manual_review(monkeypatch):
    w, clock, events = worker(monkeypatch, counter_delay=1000.)
    w.execute(Dose(.1,5,50))
    assert not w.context and w.connected
    assert results(events)[0]["source"] == "programmed_cap_estimate"
    assert results(events)[0]["amount_ml"] == .1
    assert not any(n == "dose_pending" for n,d in events)
    trace_count = len(w.transport.trace)
    clock.sleep(10); w.reconcile()
    assert len(w.transport.trace) == trace_count


@pytest.mark.parametrize("anomaly", ["slow_flow", "under_delivery", "counter_reset", "counter_jump", "unstable_count"])
def test_measurement_anomalies_never_send_stop_or_pause_next_feedback(monkeypatch, anomaly):
    w, clock, events = worker(monkeypatch, counter_delay=.2)
    p = w.transport
    if anomaly == "slow_flow": p.speed_factor = .15
    if anomaly == "under_delivery": p.cap_fraction = .4
    if anomaly == "counter_reset": p.counter_reset = True
    original = p.status
    def changed(emergency=False):
        s = original(emergency)
        if p.start_at is not None:
            if anomaly == "counter_jump": s["counter_ml"] += 100
            if anomaly == "unstable_count" and s["state"] == 3: s["counter_ml"] += clock.t*.01
        return s
    p.status = changed
    e = Engine(configured(), Load(limit_ml=10))
    e.begin(); e.connected = dict.fromkeys(e.connected, True); e.hardware_ready = True; e.enable()
    e.active = Dose(.1,5,50,10)
    w.execute(e.active)
    r = DoseResult(**results(events)[0])
    assert p.starts == 1 and p.stops == 0
    assert r.stopped and not r.error and r.warnings
    assert e.complete(r) and e.enabled and not e.manual_reason
    assert e.ready_at == r.stopped_at+10
    e.observe(Sample("ph",6.,e.ready_at+1,1))
    assert e.tick(e.ready_at+1)
    if anomaly in {"counter_reset", "counter_jump", "unstable_count"}:
        assert r.source == "programmed_cap_estimate" and e.load.estimated
    if anomaly == "under_delivery": assert r.amount_ml == pytest.approx(.04)


def test_undecodable_flow_and_counter_do_not_become_connection_failure():
    p = PumpTransport("unused")
    p.read = lambda *args: [0,1,100,999,200,999]
    s = p.status()
    assert s["state"] == 1 and s["error"] == 0
    assert s["flow_ml_min"] is None and s["counter_ml"] is None
    assert len(s["warnings"]) == 3


def test_flow_and_duration_readback_mismatch_only_produces_notes():
    p = PumpTransport("unused")
    regs = [0]*15
    regs[:4] = [100,100,2,103]  # 0.1 mL, 2 mL/min instead of 5.
    regs[13:] = [40,100]       # Derived time is also inconsistent.
    p.read = lambda *args: regs
    p.hardware = lambda *args: {"signature":"syringe", "force_percent":50}
    result = p.verify_dose(Dose(.1,5,50),"syringe")
    assert len(result["warnings"]) == 2
    regs[0] = 200
    with pytest.raises(ConfigurationError,match="finite volume limit"):
        p.verify_dose(Dose(.1,5,50),"syringe")


def test_explicit_volume_limit_still_sends_stop(monkeypatch):
    w, clock, events = worker(monkeypatch, start_delay=0,counter_delay=0)
    original = w.transport.status
    def running_at_limit(emergency=False):
        s = original(emergency)
        if w.transport.start_at is not None and w.transport.stop_at is None and clock.t >= w.transport.start_at+1.2:
            s.update(state=1,counter_ml=40.1,flow_ml_min=5.)
        return s
    w.transport.status = running_at_limit
    w.execute(Dose(.1,5,50))
    assert w.transport.stops == 1
    assert results(events)[0]["evidence"]["volume_limit_reached"]


def test_protection_fault_still_sends_stop_and_requires_resume(monkeypatch):
    w, clock, events = worker(monkeypatch, start_delay=0,counter_delay=0)
    original = w.transport.status
    def protection(emergency=False):
        s = original(emergency)
        if w.transport.start_at is not None: s["error"] = 3
        return s
    w.transport.status = protection
    w.execute(Dose(.1,5,50))
    r = DoseResult(**results(events)[0])
    assert w.transport.stops and r.error_kind == "pump_fault"
    e = Engine(configured(),Load(limit_ml=10)); e.active = Dose(.1,5,50,id=r.id); e.enabled = True
    e.complete(r)
    assert not e.enabled and e.manual_reason == "pump_fault"


def test_failed_start_persistence_does_not_issue_start_or_unnecessary_stop(monkeypatch):
    w,clock,events = worker(monkeypatch)
    w.authorize = lambda *args: False
    w.execute(Dose(.1,5,50))
    assert w.transport.starts == w.transport.stops == 0
    assert results(events)[0]["amount_ml"] == 0


def test_storage_pause_still_monitors_signal_alarm_for_active_finite_dose():
    e = Engine(configured(),Load(limit_ml=10))
    e.begin(); e.enabled = True; e.active = Dose(.1,5,50)
    e.pause("storage_paused",interrupt=False)
    assert not e.stop_requested
    for seq in range(3): e.observe(Sample("ph",10.,10+seq,seq))
    assert e.alarm and e.stop_requested and not e.enabled


def test_test_completion_cannot_shorten_formal_mixing():
    e = Engine(configured(), Load(limit_ml=10))
    e.begin(); e.connected = dict.fromkeys(e.connected,True); e.hardware_ready = True; e.enable()
    e.active = Dose(.1,5,50,60)
    e.complete(DoseResult(e.active.id,True,10,.1,"device_counter"))
    assert e.ready_at == 70
    e.pause()
    e.test(Dose(.1,5,50,purpose="test"))
    e.complete(DoseResult(e.active.id,True,20,.1,"device_counter"))
    assert e.ready_at == 70 and e.test_ready_at == 20
    e.enable(); e.observe(Sample("ph",6.,21,1))
    assert e.tick(21) is None and e.state == "mixing"
    assert e.tick(70) is None
    e.observe(Sample("ph",6.,71,2))
    assert e.tick(71)
    assert e.load.used_ml == .2 and e.cumulative_ml == .1


@pytest.fixture
def runtime(tmp_path):
    r = Runtime(tmp_path)
    r.close(); r.thread.join(10)  # Run service operations deterministically.
    yield r
    for s in (r.store,r.bench):
        if s: s.close()


def test_service_queue_start_commands_are_invalidated_by_stop(runtime):
    r = runtime
    r.command("test",volume_ml=.1,flow_ml_min=5,force_percent=50,purpose="test")
    r.command("enable",signature="syringe")
    r.stop()
    r.apply_priority_stop()
    while not r.commands.empty(): r.dispatch(*r.commands.get_nowait())
    assert not r.engine.active and not r.engine.enabled and not r.bench
    assert r.engine.manual_reason == "emergency_stop"
    assert len([x for x in r.notices if x["kind"] == "command_cancelled"]) == 2


def active_result(r):
    r.dispatch("start_recording", {"name":"Boundary"})
    r.engine.load = Load(limit_ml=10,used_ml=1.)
    d = Dose(.1,5,50,10)
    r.engine.active, r.engine.enabled = d, True
    r.intent = {"dose": asdict(d), "time":"test"}
    r.persist_ledger()
    result = DoseResult(d.id,True,time.monotonic(),.1,"device_counter")
    return d, result


def test_failed_result_commit_retains_intent_and_amount_until_retry(runtime, monkeypatch):
    r = runtime
    d, result = active_result(r)
    put = r.store.put
    monkeypatch.setattr(r.store,"put",lambda kind,data,critical=False: False if kind == "injection" else put(kind,data,critical))
    r.handle("dose_result",asdict(result))
    ledger = json.loads(r.ledger_file.read_text())
    assert ledger["intent"]["dose"]["id"] == d.id
    assert ledger["pending_result"]["data"]["result"]["amount_ml"] == .1
    assert ledger["load"]["used_ml"] == 1 and r.engine.load.used_ml == 1
    assert r.engine.active and r.phase == "saving_result" and not r.engine.enabled
    monkeypatch.setattr(r.store,"put",put)
    assert r.retry_result()
    assert r.engine.load.used_ml == pytest.approx(1.1) and not r.engine.active
    assert not r.intent and not r.result_pending and not r.engine.enabled
    r.handle("dose_result",asdict(result))
    with readonly(r.store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM injections").fetchone()[0] == 1
    assert r.engine.load.used_ml == pytest.approx(1.1)


@pytest.mark.parametrize("ack_lost", [False, True])
def test_restart_recovers_journal_without_second_accounting_or_feedback(runtime, monkeypatch, ack_lost):
    r = runtime
    d, result = active_result(r)
    put = r.store.put
    def fail_result(kind,data,critical=False):
        if kind == "injection":
            if ack_lost: assert put(kind,data,critical)
            return False
        return put(kind,data,critical)
    monkeypatch.setattr(r.store,"put",fail_result)
    r.handle("dose_result",asdict(result))
    path = r.store.path
    r.store.close("interrupted"); r.store = None
    recovered = Runtime(r.root)
    try:
        end = time.monotonic()+5
        while recovered.result_pending and time.monotonic() < end: time.sleep(.02)
        assert not recovered.result_pending
        assert recovered.engine.load.used_ml == pytest.approx(1.1)
        assert not recovered.engine.active and not recovered.engine.enabled and not recovered.engine.recording
        with readonly(path) as db:
            assert db.execute("SELECT COUNT(*) FROM injections").fetchone()[0] == 1
        assert json.loads(recovered.ledger_file.read_text())["intent"] is None
    finally:
        recovered.close(); recovered.thread.join(10)


def test_ledger_clear_failure_after_sql_commit_retries_exactly_once(runtime, monkeypatch):
    r = runtime
    d,result = active_result(r)
    atomic = runtime_module.atomic_json
    def fail_clear(path,data):
        if path == r.ledger_file and data["intent"] is None:
            raise OSError("disk full clearing journal")
        return atomic(path,data)
    monkeypatch.setattr(runtime_module,"atomic_json",fail_clear)
    r.handle("dose_result",asdict(result))
    assert r.engine.active and r.engine.load.used_ml == 1 and r.intent
    with readonly(r.store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM injections").fetchone()[0] == 1
    monkeypatch.setattr(runtime_module,"atomic_json",atomic)
    assert r.retry_result()
    assert r.engine.load.used_ml == pytest.approx(1.1)
    with readonly(r.store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM injections").fetchone()[0] == 1


def test_stop_during_unconfirmed_result_commit_stays_immediate(runtime, monkeypatch):
    r = runtime
    _,result = active_result(r)
    stops = []
    r.workers["pump"] = SimpleNamespace(stop=lambda reason:stops.append(reason))
    put = r.store.put
    def stop_during_commit(kind,data,critical=False):
        if kind == "injection":
            r.stop("emergency_stop")
            assert stops == ["emergency_stop"]
        return put(kind,data,critical)
    monkeypatch.setattr(r.store,"put",stop_during_commit)
    r.handle("dose_result",asdict(result))
    r.apply_priority_stop()
    assert r.engine.manual_reason == "emergency_stop" and not r.engine.enabled
