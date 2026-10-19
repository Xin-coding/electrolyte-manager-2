"""Deterministic feedback decisions. No threads, serial ports or GUI imports."""
from __future__ import annotations

from collections import deque
from dataclasses import asdict
from statistics import mean

from .models import Dose, DoseResult, Load, Sample, Settings


class Engine:
    def __init__(self, settings=None, load=None, emit=lambda *_: None):
        self.settings = settings or Settings()
        self.load = load or Load()
        self.emit = emit
        self.pending_settings = None
        self.recording = False
        self.enabled = False
        self.connected = {"ph": False, "ec": False, "pump": False}
        self.hardware_ready = False
        self.active: Dose | None = None
        self.ready_at = 0.
        self.cumulative_ml = 0.
        self.latest = {}
        self.filtered = {}
        self.baseline = {"ph": deque(maxlen=5), "ec": deque(maxlen=3)}
        self.auto_target = {"ph": None, "ec": None}
        self.last_sequence = {}
        self.consumed = None
        self.alarm = False
        self.bad_count = self.good_count = 0
        self.manual_reason = ""
        self.stop_requested = False
        self.storage_failed = False
        self.state = "idle"

    def begin(self):
        if self.active:
            raise ValueError("Wait for the pump to stop")
        self.recording = True
        self.enabled = False
        self.cumulative_ml = 0.
        self.auto_target = {"ph": None, "ec": None}
        for q in self.baseline.values():
            q.clear()
        self._reset_signal()
        self.state = "recording"

    def _reset_signal(self):
        self.filtered.clear()
        self.latest.clear()
        self.consumed = None
        self.bad_count = self.good_count = 0

    def connection(self, kind, connected):
        previous = self.connected[kind]
        self.connected[kind] = connected
        if previous != connected and kind != "pump":
            self.filtered.pop(kind, None)
            self.latest.pop(kind, None)
            self.baseline[kind].clear()
            if kind == self.settings.control:
                self.bad_count = self.good_count = 0

    @property
    def target(self):
        c = getattr(self.settings, self.settings.control)
        return self.auto_target[self.settings.control] if c.target is None else c.target

    def observe(self, sample: Sample):
        if not sample.valid or not sample.live:
            return None
        if sample.sequence <= self.last_sequence.get(sample.kind, -1):
            return None
        self.last_sequence[sample.kind] = sample.sequence
        x = sample.value
        previous = self.filtered.get(sample.kind, x)
        y = .5*x+.5*previous if self.settings.smoothing else x
        self.filtered[sample.kind] = y
        self.latest[sample.kind] = sample
        if self.recording and self.auto_target[sample.kind] is None:
            q = self.baseline[sample.kind]
            q.append(x)
            tolerance = .05 if sample.kind == "ph" else abs(mean(q))*.01
            if len(q) == q.maxlen and max(q)-min(q) <= tolerance+1e-12:
                self.auto_target[sample.kind] = mean(q)
                self.emit("initial_target", {"signal": sample.kind, "value": mean(q)})
        if self.enabled and sample.kind == self.settings.control:
            cfg = getattr(self.settings, sample.kind)
            outside = x < cfg.lower or x > cfg.upper
            self.bad_count = min(5, self.bad_count+1) if outside else 0
            self.good_count = min(5, self.good_count+1) if not outside else 0
            count = 3 if sample.kind == "ph" else 1
            if not self.alarm and self.bad_count >= count:
                self.alarm = True
                self.stop_requested = self.active is not None
                self.emit("alarm_started", {"signal": sample.kind, "raw": x})
            elif self.alarm and self.good_count >= count:
                self.alarm = False
                self.consumed = None
                self.emit("alarm_recovered", {"signal": sample.kind, "raw": x})
        return y

    def enable(self):
        if not self.recording:
            raise ValueError("Start recording first")
        if self.active:
            raise ValueError("Wait for the pump to stop")
        self.settings.validate(complete=True)
        if self.storage_failed:
            raise ValueError("Data storage is unavailable")
        if not self.connected["pump"] or not self.hardware_ready:
            raise ValueError("Pump stop and configuration must be confirmed")
        if not self.load.allows(self.settings.dose_ml):
            raise ValueError("Insufficient or unverified load allowance")
        self.enabled = True
        self.manual_reason = ""
        self.alarm = False
        self._reset_signal()
        self.emit("feedback_enabled", {"version": self.settings.version})

    def pause(self, reason="manual_pause"):
        self.enabled = False
        self.manual_reason = reason
        self.stop_requested = self.active is not None
        self.emit("feedback_paused", {"reason": reason})

    def apply(self, settings: Settings, now):
        settings.validate(complete=self.enabled)
        old = self.settings.payload()
        settings = Settings.from_dict(settings.payload())
        settings.version = max(self.settings.version, self.pending_settings.version if self.pending_settings else 0)+1
        self.emit("settings_requested", {"old": old, "new": settings.payload()})
        # Record-only metadata is immediate even while operational changes wait.
        self.settings.metadata = dict(settings.metadata)
        if self.active or self.enabled and now < self.ready_at:
            self.pending_settings = settings
        else:
            self._activate(settings)

    def _activate(self, settings):
        old = self.settings.payload()
        self.settings = settings
        self.pending_settings = None
        self._reset_signal()
        self.emit("settings_applied", {"old": old, "new": settings.payload()})

    def tick(self, now):
        if not self.active and now >= self.ready_at and self.pending_settings:
            self._activate(self.pending_settings)
        if self.active:
            self.state = "injecting"
            return None
        if not self.enabled:
            self.state = self.manual_reason or ("recording" if self.recording else "idle")
            return None
        if self.alarm:
            self.state = "alarm_paused"
            return None
        if self.storage_failed:
            self.state = "storage_paused"
            return None
        if not self.connected["pump"] or not self.hardware_ready:
            self.state = "waiting_pump"
            return None
        kind = self.settings.control
        if not self.connected[kind]:
            self.state = "waiting_signal"
            return None
        if now < self.ready_at:
            self.state = "mixing"
            return None
        if self.target is None:
            self.state = "waiting_target"
            return None
        cfg = getattr(self.settings, kind)
        if not cfg.lower < self.target-cfg.tolerance < self.target+cfg.tolerance < cfg.upper:
            self.state = "target_outside_alarm"
            return None
        s = self.latest.get(kind)
        if not s or s.mono < self.ready_at or (kind, s.sequence) == self.consumed:
            self.state = "waiting_new_sample"
            return None
        self.consumed = (kind, s.sequence)
        y = self.filtered[kind]
        need = y < self.target-cfg.tolerance if cfg.direction == "raise" else y > self.target+cfg.tolerance
        self.emit("decision", {"signal": kind, "sequence": s.sequence, "raw": s.value,
                               "filtered": y, "target": self.target, "demand": need,
                               "version": self.settings.version})
        if not need:
            self.state = "monitoring"
            return None
        if not self.load.allows(self.settings.dose_ml):
            self.state = "allowance_exhausted"
            return None
        c = self.settings
        self.active = Dose(c.dose_ml, c.flow_ml_min, c.force_percent, c.mix_s, settings_version=c.version)
        self.state = "preparing"
        return self.active

    def test(self, dose):
        if self.active or self.enabled:
            raise ValueError("Pump is reserved by feedback or another test")
        if dose.purpose not in {"test", "stop_test"}:
            raise ValueError("Invalid test purpose")
        if self.load.uncertain or self.load.limit_ml is not None and not self.load.allows(dose.cap):
            raise ValueError("Insufficient or unverified load allowance")
        self.active = dose

    def complete(self, result: DoseResult):
        if not self.active or result.id != self.active.id:
            return False
        if not result.stopped or result.amount_ml is None:
            self.load.uncertain = True
            self.hardware_ready = False
            self.state = "waiting_pump"
            return False
        dose = self.active
        self.load.used_ml += max(0., result.amount_ml)
        self.load.uncertain = False
        if dose.purpose == "experiment":
            self.cumulative_ml += max(0., result.amount_ml)
        self.ready_at = result.stopped_at + (dose.mix_s if result.started else 0.)
        self.active = None
        self.stop_requested = False
        self.emit("dose_accounted", {"dose": asdict(dose), "result": asdict(result),
                                    "load": asdict(self.load), "cumulative_ml": self.cumulative_ml})
        if result.error:
            self.pause("pump_fault")
        return True

    def set_allowance(self, value, refill=False, identity=""):
        if self.active:
            raise ValueError("Wait for the pump to stop")
        from .models import finite
        if not finite(value) or value <= 0:
            raise ValueError("Load allowance must be positive")
        old = asdict(self.load)
        if refill:
            self.load = Load(limit_ml=value, identity=identity)
        else:
            self.load.limit_ml = value
            self.load.identity = identity or self.load.identity
        self.emit("refill" if refill else "allowance_changed", {"old": old, "new": asdict(self.load)})
