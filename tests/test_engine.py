from dataclasses import replace
import pytest

from em2.engine import Engine
from em2.models import Settings, SignalSettings, Sample, DoseResult, Dose, Load


def engine(kind="ph", direction="raise", smoothing=False):
    settings = Settings(control=kind, smoothing=smoothing, dose_ml=.1, flow_ml_min=1, force_percent=50, mix_s=20,
                        ph=SignalSettings(7, .1, 5, 9, direction), ec=SignalSettings(7, .1, 5, 9, direction))
    e = Engine(settings, Load(limit_ml=2))
    e.begin()
    e.connected = dict.fromkeys(e.connected, True)
    e.hardware_ready = True
    e.enable()
    return e


def sample(e, value, t, seq, kind=None, **kw):
    return e.observe(Sample(kind or e.settings.control, value, t, seq, **kw))


@pytest.mark.parametrize("kind", ["ph", "ec"])
@pytest.mark.parametrize("direction,value", [("raise", 6.8), ("lower", 7.2)])
def test_pulse_mix_fresh_sample(kind, direction, value):
    e = engine(kind, direction)
    sample(e, value, 1, 1)
    d = e.tick(1)
    assert d and d.volume_ml == .1
    # Ordinary target arrival does not interrupt the finite dose.
    sample(e, 7, 2, 2)
    assert not e.stop_requested
    assert e.tick(3) is None
    assert e.complete(DoseResult(d.id, True, 10, .101, "device_counter"))
    assert e.ready_at == 30
    assert e.cumulative_ml == .101
    sample(e, value, 29.9, 3)
    assert e.tick(29.9) is None
    assert e.tick(30) is None
    assert e.tick(40) is None  # pre-mixing sample remains ineligible
    sample(e, value, 40.1, 4)
    assert e.tick(40.1)


@pytest.mark.parametrize("kind,count", [("ph", 3), ("ec", 1)])
@pytest.mark.parametrize("side", [4., 10.])
def test_raw_alarm_resume(kind, count, side):
    e = engine(kind, smoothing=True)
    sample(e, 6.8, 1, 1)
    d = e.tick(1)
    for i in range(count):
        sample(e, side, i+2, i+2)
        assert e.alarm == (i == count-1)
    assert e.stop_requested
    e.complete(DoseResult(d.id, True, 10, .04, "device_counter", interrupted=True))
    for i in range(count):
        sample(e, 6.8, 11+i, 10+i)
    assert not e.alarm
    assert not e.tick(29)
    # EMA can legitimately remain above the trigger after the raw alarm clears.
    for i in range(10):
        sample(e, 6.8, 31+i, 99+i)
    assert e.tick(41)  # New full dose; no residual volume catch-up


def test_noncontrol_alarm_cannot_pause():
    e = engine()
    for i in range(5):
        sample(e, 100, i, i, "ec")
    assert not e.alarm


def test_sample_dedupe_and_history():
    e = engine()
    sample(e, 7, 1, 1)
    assert e.tick(1) is None
    sample(e, 6, 2, 1)
    assert e.filtered["ph"] == 7
    sample(e, 6, 3, 2, live=False)
    sample(e, 6, 4, 3, quality="invalid")
    assert e.tick(5) is None
    assert e.filtered["ph"] == 7


def test_complete_before_sensor_wait_and_unrelated_disconnect():
    e = engine()
    sample(e, 6.8, 1, 1)
    d = e.tick(1)
    e.connection("ph", False)
    assert not e.stop_requested
    e.complete(DoseResult(d.id, True, 10, .1, "counter"))
    assert not e.tick(40)
    assert e.state == "waiting_signal"
    e.connection("ph", True)
    e.connection("ec", False)
    sample(e, 6.8, 41, 2)
    assert e.tick(41)


def test_initial_targets_and_ema_reset():
    e = engine(smoothing=True)
    e.settings.ph.target = None
    e.auto_target = {"ph": None, "ec": None}
    for i, v in enumerate([7, 7.01, 7.02, 7.03, 7.04]):
        sample(e, v, i, i)
    assert e.target == pytest.approx(7.02)
    e.connection("ph", False)
    e.connection("ph", True)
    sample(e, 6, 20, 10)
    assert e.target == pytest.approx(7.02)
    assert e.filtered["ph"] == 6
    for i, v in enumerate([100, 100.4, 100.8]):
        sample(e, v, 30+i, 30+i, "ec")
    assert e.auto_target["ec"] == pytest.approx(100.4)


def test_pending_settings_keep_current_mix_and_require_fresh():
    e = engine()
    sample(e, 6, 1, 1)
    d = e.tick(1)
    new = Settings.from_dict(e.settings.payload())
    new.mix_s = 1
    new.control = "ec"
    e.apply(new, 2)
    assert e.settings.mix_s == 20
    assert e.pending_settings.mix_s == 1
    e.complete(DoseResult(d.id, True, 10, .1, "counter"))
    assert e.ready_at == 30
    assert not e.tick(29)
    sample(e, 6, 30, 1, "ec")
    assert not e.tick(30)  # apply resets channel's old decisions
    assert e.settings.control == "ec"
    sample(e, 6, 31, 2, "ec")
    assert e.tick(31).mix_s == 1


@pytest.mark.parametrize("reason", ["manual_pause", "emergency_stop", "pump_fault", "storage_paused"])
def test_settings_and_signal_recovery_do_not_resume_manual(reason):
    e = engine()
    e.pause(reason)
    e.apply(e.settings, 1)
    for i in range(4):
        sample(e, 6.8, i+1, i)
    assert not e.enabled and not e.tick(10)


def test_load_test_and_refill_accounting():
    e = engine()
    e.pause()
    d = Dose(.1, 1, 50, purpose="test")
    e.test(d)
    r = DoseResult(d.id, True, 10, .1, "counter")
    assert e.complete(r)
    assert not e.complete(r)
    assert e.load.used_ml == .1 and e.cumulative_ml == 0
    e.cumulative_ml = 1.2
    e.set_allowance(1, refill=True)
    assert e.load.used_ml == 0 and e.cumulative_ml == 1.2
    e.load.used_ml = .95
    assert not e.load.allows(.1)
    with pytest.raises(ValueError):
        e.enable()


def test_unknown_is_not_zero_or_accounted_twice():
    e = engine()
    sample(e, 6, 1, 1)
    d = e.tick(1)
    assert not e.complete(DoseResult(d.id, False, 5, None, "unverified"))
    assert e.active and e.load.uncertain
    assert e.cumulative_ml == 0
    assert e.complete(DoseResult(d.id, True, 10, .05, "counter", recovered=True))
    assert e.load.used_ml == .05 and not e.load.uncertain


def test_missing_settings_allows_recording_but_not_feedback():
    e = Engine()
    e.begin()
    assert e.recording
    with pytest.raises(ValueError):
        e.enable()


@pytest.mark.parametrize("value", [6.9, 7.0, 7.1])
def test_target_band_boundary(value):
    e = engine()
    sample(e, value, 1, 1)
    assert not e.tick(1)


def test_paused_settings_apply_immediately_without_shortening_mix():
    e = engine()
    e.ready_at = 100
    e.pause()
    new = Settings.from_dict(e.settings.payload())
    new.mix_s = 1
    e.apply(new, 80)
    assert e.pending_settings is None and e.settings.mix_s == 1
    assert e.ready_at == 100 and not e.enabled
