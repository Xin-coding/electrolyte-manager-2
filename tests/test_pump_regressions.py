"""Real form/config/decision/packet paths with an in-memory serial endpoint."""
import json
import os
import struct
import time
from dataclasses import asdict, replace
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
import serial
from PySide6 import QtWidgets as W

import em2.devices.pump as pump_module
from em2.devices.pump import PumpTransport, PumpWorker
from em2.devices.wire import crc, flow, packet, volume
from em2.engine import Engine
from em2.errors import AccountingError, ParameterError
from em2.models import Binding, Dose, DoseResult, Load, Sample, Settings, SignalSettings
from em2.runtime import Runtime
from em2.ui import Window


@pytest.fixture(autouse=True)
def prohibit_real_ports(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Offline regressions must never open a real port")
    monkeypatch.setattr(serial, "Serial", forbidden)


class Clock:
    def __init__(self): self.t = 100.
    def monotonic(self): return self.t
    def sleep(self, seconds): self.t += seconds


class Flag:
    def __init__(self, clock): self.clock = clock
    def is_set(self): return False
    def wait(self, seconds): self.clock.sleep(seconds); return False


class MemorySerial:
    """Emulates register replies, not packet serialization or verification."""
    def __init__(self):
        self.regs = {0x5e: 0, 0x61: 60, 0x62: 103, 0x63: 6627, 0x64: 99, 0x6e: 50}
        self.writes = []
        self.answer = b""
    def reset_input_buffer(self): pass
    def write(self, request):
        assert crc(request[:-2]) == request[-2:]
        address, function, reg, value = struct.unpack(">BBHH", request[:6])
        if function == 3:
            words = [self.regs.get(reg+i, 0) for i in range(value)]
            body = bytes((address, function, 2*value))+struct.pack(">"+"H"*value, *words)
        else:
            self.writes.append(request)
            words = [value] if function == 6 else struct.unpack(">"+"H"*value, request[7:-2])
            self.regs.update({reg+i: word for i, word in enumerate(words)})
            if reg == 0x6a and 0x68 in self.regs:
                seconds = volume([self.regs[0x68],self.regs[0x69]])/flow([self.regs[0x6a],self.regs[0x6b]])*60
                self.regs[0x75], self.regs[0x76] = round(seconds*1000), 97
            body = request[:6]
        self.answer = body+crc(body)
    def read(self, size):
        assert len(self.answer) == size
        return self.answer


def transport(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(pump_module, "time", clock)
    p = PumpTransport("never-opened", stop_event=Flag(clock))
    p.io = MemorySerial()
    return p


def configured(kind="ph"):
    return Settings(control=kind, dose_ml=.1, flow_ml_min=5, force_percent=50., mix_s=10,
                    ph=SignalSettings(7,.1,5,9), ec=SignalSettings(7,.1,5,9))


@pytest.mark.parametrize("kind", ["ph", "ec"])
@pytest.mark.parametrize("path", ["form", "profile", "pending"])
def test_form_to_actual_wire_including_profiles_and_pending(tmp_path, monkeypatch, kind, path):
    app = W.QApplication.instance() or W.QApplication([])
    r = Runtime(tmp_path)
    w = Window(r)
    try:
        w.populate_settings(configured(kind).payload())
        w.fields["force_percent"].setText("50.0")
        if path == "profile":
            profile = str(tmp_path/"preset.json")
            monkeypatch.setattr(W.QFileDialog, "getSaveFileName", lambda *a: (profile,""))
            monkeypatch.setattr(W.QFileDialog, "getOpenFileName", lambda *a: (profile,""))
            w.save_settings()
            w.fields["force_percent"].setText("60")
            w.load_settings()
        cfg = Settings.from_dict(json.loads(json.dumps(w.collect_settings())))
        assert type(cfg.force_percent) is int
        e = Engine(cfg, Load(limit_ml=1))
        e.begin(); e.connected = dict.fromkeys(e.connected, True); e.hardware_ready = True; e.enable()
        if path == "pending":
            e.active = Dose(.1,5,50,10)
            e.apply(cfg, 100)
            assert e.pending_settings
            e.complete(DoseResult(e.active.id,True,100,.1,"device_counter"))
            assert e.tick(109) is None and e.pending_settings
            e.tick(110)
            assert e.pending_settings is None
        e.observe(Sample(kind,6.8,111,1))
        dose = e.tick(111)
        assert dose and type(dose.force_percent) is int
        p = transport(monkeypatch)
        verified = p.configure_dose(dose, p.hardware()["signature"])
        assert verified["target_ml"] == .1
        assert verified["device_duration_s"] == pytest.approx(1.2)
        assert p.io.regs[0x6e] == 50
        assert len(p.io.writes) == 6
        assert not any(struct.unpack(">H", frame[2:4])[0] == 1 for frame in p.io.writes)
    finally:
        r.close(); r.thread.join(10); w.close(); app.processEvents()


@pytest.mark.parametrize("value", [50.5, True, float("nan"), float("inf"), 19, 101, "50"])
def test_invalid_force_rejected_by_settings_and_test_command(value):
    with pytest.raises(ParameterError): Settings.from_dict({"force_percent":value})
    with pytest.raises(ParameterError): Dose(.1,5,value,purpose="test")


@pytest.mark.parametrize("value", [50, 50.0])
def test_force_normalized_at_command_boundary(value):
    d = Dose(.1,5,value)
    assert type(d.force_percent) is int
    assert packet(6,0x6e,d.force_percent)


@pytest.mark.parametrize("value", [50.0, True, -1, 65536])
def test_packet_reports_bad_register_value(value):
    with pytest.raises(ParameterError, match="0x006E"):
        packet(6,0x6e,value)


@pytest.mark.parametrize("cap,speed", [(.10001,1),(.1,1e-9),(.001,1e8)])
def test_unrepresentable_parameters_rejected_before_writes(monkeypatch, cap, speed):
    with pytest.raises(ParameterError):
        Settings.from_dict({"dose_ml":cap,"flow_ml_min":speed})
    p = transport(monkeypatch)
    raw = SimpleNamespace(cap=cap, flow_ml_min=speed, force_percent=50, mix_s=0)
    with pytest.raises(ParameterError): p.configure_dose(raw,"irrelevant")
    assert p.io.writes == []


@pytest.mark.parametrize("stop_after", [6, None])
def test_one_tick_changes_restart_counter_stability(monkeypatch, stop_after):
    clock = Clock(); monkeypatch.setattr(pump_module,"time",clock)
    worker = PumpWorker(Binding("pump","unused","offline"),lambda *a:None,lambda *a:False)
    reads = []
    def status(emergency=False):
        count = len(reads)+1
        tick = count if stop_after is None else min(count,stop_after)
        s = dict(state=3,error=0,read_at=clock.t,counter_ml=1+tick*.001,resolution_ml=.001)
        reads.append(s); return s
    worker.transport = SimpleNamespace(status=status)
    initial = dict(state=3,error=0,read_at=100.,counter_ml=1.,resolution_ml=.001)
    if stop_after is None:
        with pytest.raises(AccountingError,match="not settled"): worker.settle(initial)
    else:
        final = worker.settle(initial)
        assert clock.t >= 101.6-1e-9
        assert final["counter_ml"] == pytest.approx(1.006)
        assert reads[-1]["counter_ml"] == reads[-2]["counter_ml"]


@pytest.mark.parametrize("kind", ["parameter_error","configuration_error","communication_error","software_error","pump_fault"])
def test_failed_results_keep_specific_reason_and_formal_label(tmp_path, kind):
    app = W.QApplication.instance() or W.QApplication([])
    r = Runtime(tmp_path); w = Window(r)
    try:
        r.engine.active = Dose(.1,5,50)
        result = DoseResult(r.engine.active.id,True,100.,0.,"not_started",False,
                            "preparing: diagnostic detail "+"x"*400,error_kind=kind)
        r.handle("dose_result",asdict(result))
        r.engine.tick(100.); r.publish(); w.refresh()
        assert r.snapshot["state"] == kind
        assert r.snapshot["last_failure"]["result"]["error"] == result.error
        assert result.error in w.dose_result_label.text()
        assert "正式注射" in w.dose_result_label.text()
        assert "未发送" in w.dose_result_label.text()
        assert not w.test_results.toPlainText()
        w.switch_language(1); w.refresh()
        assert "Experiment dose" in w.dose_result_label.text()
    finally:
        r.close(); r.thread.join(10); w.close(); app.processEvents()


def test_readiness_and_legacy_startup(tmp_path):
    (tmp_path/"data").mkdir()
    (tmp_path/"data/settings.json").write_text(json.dumps(configured().payload()))
    app = W.QApplication.instance() or W.QApplication([])
    r = Runtime(tmp_path); w = Window(r)
    try:
        assert type(r.engine.settings.force_percent) is int
        for field,value in ((w.test_volume,"0.1"),(w.test_flow,"5"),(w.test_force,"50"),(w.test_cap,"0.2")):
            field.setText(value)
        for connected,ready,uncertain,enabled,error,expected in [
                (False,False,False,False,0,"pump_disconnected"),
                (True,False,False,False,0,"waiting_pump"),
                (True,True,False,False,3,"pump_fault"),
                (True,True,True,False,0,"accounting_error"),
                (True,True,False,True,0,"feedback_reserved"),
                (True,True,False,False,0,"")]:
            r.engine.connected["pump"]=connected; r.engine.hardware_ready=ready
            r.engine.load.uncertain=uncertain; r.engine.enabled=enabled
            r.pump_status={"state":3,"error":error}; r.hardware={"capacity_ml":60}
            r.publish(); w.refresh()
            assert r.snapshot["test_block_reason"] == expected
            assert w.finite_test_button.isEnabled() == (expected == "")
        r.engine.enabled = False
        r.engine.load = Load(limit_ml=.05)
        r.publish(); w.refresh()
        assert not w.finite_test_button.isEnabled()
        assert r.test_block_reason(.1) == "allowance_exhausted"
    finally:
        r.engine.enabled=False; r.close(); r.thread.join(10); w.close(); app.processEvents()


def test_old_invalid_precision_profile_can_open_for_editing(tmp_path):
    (tmp_path/"data").mkdir()
    payload=configured().payload(); payload["dose_ml"] = .10001
    (tmp_path/"data/settings.json").write_text(json.dumps(payload))
    r=Runtime(tmp_path)
    try:
        assert r.engine.settings.dose_ml == .10001
        r.engine.begin()
        with pytest.raises(ParameterError): r.engine.enable()
        assert r.engine.recording and not r.engine.enabled
    finally:
        r.close(); r.thread.join(10)
