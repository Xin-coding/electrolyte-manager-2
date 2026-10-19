"""Input failures must not escape the draft or cause any device action."""
import copy
import json
import os
from dataclasses import replace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
import serial
from PySide6 import QtWidgets as W

from em2 import forms
from em2.devices.wire import encode
from em2.errors import ParameterError
from em2.models import Dose, DoseResult, Load, Settings, SignalSettings
from em2.parameters import pump_parameters
from em2.runtime import Runtime
from em2.ui import Window


@pytest.fixture
def ui(tmp_path, monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail("Input validation must never open a real serial port")
    monkeypatch.setattr(serial, "Serial", forbidden)
    def modal(*args, **kwargs):
        pytest.fail(f"Input errors must be inline, not a modal: {args[2]}")
    monkeypatch.setattr(W.QMessageBox, "warning", modal)
    app = W.QApplication.instance() or W.QApplication([])
    r = Runtime(tmp_path)
    # Deterministic service dispatch below, without timers or physical workers.
    r.close(); r.thread.join(10)
    r.publish()
    w = Window(r)
    w.timer.stop()
    yield w, r, app
    w.pool.waitForDone(3000)
    w.close(); app.processEvents()


def configured():
    return Settings(dose_ml=.1, flow_ml_min=5, force_percent=50, mix_s=10,
                    ph=SignalSettings(7, .1, 5, 9), ec=SignalSettings(7, .1, 5, 9))


@pytest.mark.parametrize("text", ["0，1", "0,1", "1,000", "1..2", "5 mL", "NaN", "inf", "1e309", "1e-999", "1_000", "１", "1 2", "-", ".", "1e"])
def test_malformed_numbers_are_not_silently_coerced(text):
    value, error = forms.parse_number(text)
    assert value is None and all(error)


@pytest.mark.parametrize("text,expected", [("  0.100  ", .1), ("50.0", 50), ("1e-1", .1), (".5", .5), ("", None)])
def test_unambiguous_number_formats(text, expected):
    assert forms.parse_number(text) == (expected, None)


@pytest.mark.parametrize("key,text,reason", [
    ("dose_ml", "-1", "大于 0"), ("dose_ml", ".123456", "精度"),
    ("dose_ml", "1e308", "无法"), ("flow_ml_min", "0", "大于 0"),
    ("flow_ml_min", "1e-30", "无法"), ("force_percent", "50.5", "整数"),
    ("force_percent", "101", "20–100"), ("mix_s", "-1", "小于 0"),
    ("ph_interval_s", ".1", "0.5–3600"), ("ph_interval_s", "", "必填")])
def test_blur_marks_specific_field_and_translation(ui, key, text, reason):
    w, r, app = ui
    w.populate_settings(configured().payload())
    field = w.fields[key]
    field.setText(text)
    assert field.issue is None  # Draft typing alone is not a submit.
    field.editingFinished.emit()
    assert reason in field.error_label.text()
    assert field.property("invalid") and not field.error_label.isHidden()
    assert r.commands.empty()
    before = field.error_label.text()
    w.switch_language(1)
    assert field.error_label.text() != before and field.accessibleDescription() == field.error_label.text()
    w.populate_settings(configured().payload())
    assert field.issue is None and field.error_label.isHidden()


@pytest.mark.parametrize("kind", ["ph", "ec"])
def test_cross_field_alarms_and_target_band(ui, kind):
    w, r, app = ui
    w.populate_settings(configured().payload())
    f = w.signal_fields[kind]
    f["lower"].setText("10")
    with pytest.raises(forms.FormError): w.collect_settings()
    assert "下侧报警" in f["lower"].error_label.text()
    assert f["upper"].issue
    f["lower"].setText("6.9")  # target minus tolerance equals boundary
    with pytest.raises(forms.FormError): w.collect_settings()
    assert all(f[k].issue for k in ("target", "tolerance", "lower", "upper"))
    f["lower"].setText("5")
    f["lower"].editingFinished.emit()
    assert not any(f[k].issue for k in ("target", "tolerance", "lower", "upper"))
    assert w.collect_settings()[kind]["target"] == 7


@pytest.mark.parametrize("phase", ["idle", "injecting", "mixing"])
def test_invalid_apply_preserves_effective_pending_timer_and_no_commands(ui, phase):
    w, r, app = ui
    e = r.engine
    e.settings = configured()
    e.load = Load(limit_ml=1)
    e.begin(); e.connected = dict.fromkeys(e.connected, True); e.hardware_ready = True
    if phase != "idle":
        e.enable()
        e.active = Dose(.1, 5, 50, 10)
        e.apply(replace(configured(), flow_ml_min=4), 100)
        if phase == "mixing":
            e.complete(DoseResult(e.active.id, True, 100, .1, "device_counter"))
    r.publish()
    w.populate_settings(configured().payload())
    before = copy.deepcopy((e.settings, e.pending_settings, e.active, e.ready_at, e.enabled, e.load))
    w.fields["force_percent"].setText("50.5")
    w.guard(w.apply_settings)  # Button handler must report inline, not pop up.
    assert w.fields["force_percent"].issue
    assert not w.draft_notice.isHidden()
    assert r.commands.empty()
    assert (e.settings, e.pending_settings, e.active, e.ready_at, e.enabled, e.load) == before
    # Corrected values enter the normal versioned path, retaining the old wait.
    w.fields["force_percent"].setText("50.0")
    w.fields["mix_s"].setText("20")
    w.apply_settings()
    action, data = r.commands.get_nowait()
    assert action == "apply"
    e.apply(Settings.from_dict(data["settings"]), 105)
    if phase == "injecting":
        assert e.active.mix_s == 10 and e.settings.mix_s == 10
        e.complete(DoseResult(e.active.id, True, 100, .1, "device_counter"))
    if phase != "idle":
        assert e.ready_at == 110 and e.settings.mix_s == 10
        assert e.pending_settings.mix_s == 20
        e.tick(109.99)
        assert e.settings.mix_s == 10
        e.tick(110)
        assert e.pending_settings is None and e.settings.mix_s == 20
    else:
        assert e.settings.mix_s == 20


def test_missing_controls_allow_recording_and_partial_preset_but_not_enable(ui, monkeypatch, tmp_path):
    w, r, app = ui
    assert w.collect_settings()["dose_ml"] is None
    w.start_button.click()
    action, data = r.commands.get_nowait()
    assert action == "start_recording"  # Does not run draft validation.
    w.guard(w.enable_control)
    assert r.commands.empty() and not w.feedback_notice.isHidden()
    assert w.fields["dose_ml"].issue and w.signal_fields["ph"]["tolerance"].issue
    assert w.signal_fields["ph"]["target"].issue is None
    preset = tmp_path/"partial.json"
    monkeypatch.setattr(W.QFileDialog, "getSaveFileName", lambda *a: (str(preset), ""))
    w.save_settings()
    assert json.loads(preset.read_text())["settings"]["dose_ml"] is None


def test_invalid_save_and_allowance_do_not_write_or_queue(ui, monkeypatch, tmp_path):
    w, r, app = ui
    w.populate_settings(configured().payload())
    w.fields["dose_ml"].setText("0,1")
    def dialog(*a): pytest.fail("Invalid preset must not reach the save dialog")
    monkeypatch.setattr(W.QFileDialog, "getSaveFileName", dialog)
    w.guard(w.save_settings)
    for text in ("", "0", "-1", "1e999", "1 mL"):
        w.allowance.setText(text)
        w.guard(lambda: w.set_allowance(False))
        assert w.allowance.issue and r.commands.empty()
    w.allowance.setText("0.01")  # Below consumption is allowed; no next dose.
    w.set_allowance(False)
    assert r.commands.get_nowait() == ("allowance", {"value": .01, "refill": False})


def test_valid_effective_resume_ignores_invalid_unapplied_draft(ui, monkeypatch):
    w, r, app = ui
    r.engine.settings = configured(); r.publish()
    w.populate_settings(configured().payload())
    w.fields["dose_ml"].setText("bad draft")
    seen = []
    monkeypatch.setattr(w, "confirm_hardware", lambda details: seen.append(details) or "known")
    w.enable_control()
    assert seen[0]["settings"]["dose_ml"] == .1
    action, data = r.commands.get_nowait()
    assert action == "enable" and data["signature"] == "known"


def test_stop_cap_and_capacity_are_inline_finite_test_is_independent(ui):
    w, r, app = ui
    r.snapshot.update(hardware={"capacity_ml": 1}, remaining=.5, test_block_reason="")
    for edit, text in ((w.test_volume, ".1"), (w.test_flow, "5"), (w.test_force, "50.0"), (w.test_cap, ".05")):
        edit.setText(text)
    w.test_cap.editingFinished.emit()
    assert "必须大于" in w.test_cap.error_label.text()
    assert w.test_data(False)["hardware_cap_ml"] is None
    w.guard(lambda: w.start_test(True))
    assert r.commands.empty()
    w.test_cap.setText(".6")
    w.test_cap.editingFinished.emit()
    assert "剩余额度" in w.test_cap.error_label.text()
    w.test_cap.setText("2")
    w.test_cap.editingFinished.emit()
    assert "注射器容量" in w.test_cap.error_label.text()
    w.test_cap.setText(".2")
    w.test_cap.editingFinished.emit()
    assert w.test_cap.issue is None and w.test_data(True)["force_percent"] == 50


def test_history_range_errors_do_not_submit_queries(ui, monkeypatch):
    w, r, app = ui
    queries = []
    monkeypatch.setattr(w, "submit", lambda *a: queries.append(a))
    w.range_start.setText("2"); w.range_end.setText("1")
    w.guard(w.compare_history)
    assert "greater than" in w.range_end.error_label.text()
    assert not queries and r.commands.empty()
    w.switch_language(0)
    assert "greater than" in w.range_end.error_label.text()


def test_extreme_finite_pump_number_reports_validation_not_overflow():
    with pytest.raises(ValueError, match="cannot be encoded"): encode(1e308)
    with pytest.raises(ParameterError): pump_parameters(1e308, 5, 50)


def test_clearing_required_field_during_feedback_cannot_apply(ui):
    w, r, app = ui
    r.engine.settings = configured(); r.engine.enabled = True; r.publish()
    w.populate_settings(configured().payload())
    w.fields["mix_s"].clear()
    w.guard(w.apply_settings)
    assert "必填" in w.fields["mix_s"].error_label.text() and r.commands.empty()
    assert r.engine.enabled and r.engine.settings.mix_s == 10


def test_duration_pair_and_capacity_fields(ui):
    w, r, app = ui
    w.populate_settings(configured().payload())
    w.fields["flow_ml_min"].setText("9999")
    with pytest.raises(forms.FormError): w.collect_settings()
    assert "注射时间" in w.fields["flow_ml_min"].error_label.text()
    w.fields["flow_ml_min"].setText("5")
    r.snapshot["hardware"] = {"capacity_ml": .05}
    with pytest.raises(forms.FormError): w.collect_settings()
    assert "注射器容量" in w.fields["dose_ml"].error_label.text()
    assert r.commands.empty()
