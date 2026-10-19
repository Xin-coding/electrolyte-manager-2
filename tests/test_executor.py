from types import SimpleNamespace
from dataclasses import asdict
import pytest

import em2.devices.pump as module
from em2.devices.pump import PumpWorker, Cancelled
from em2.models import Binding, Dose


class Clock:
    def __init__(self):
        self.t = 100.
    def monotonic(self):
        return self.t
    def sleep(self, seconds):
        self.t += seconds


class Flag:
    def __init__(self, clock):
        self.clock, self.value = clock, False
    def set(self): self.value = True
    def clear(self): self.value = False
    def is_set(self): return self.value
    def wait(self, seconds):
        self.clock.sleep(seconds)
        return self.value


class Device:
    def __init__(self, clock, delay=1.1):
        self.clock, self.delay = clock, delay
        self.counter = 40.
        self.started = None
        self.ended = None
        self.last_tx = 0
        self.starts = self.stops = 0
        self.parameters = None
        self.error = 0
    def configure_dose(self, dose, signature):
        self.clock.sleep(8.)  # Regression: preparation must not expire after 6 s.
        self.parameters = dose
        return {"hardware": {"signature": "s"}}
    def write(self, register, value, emergency=False):
        self.last_tx = self.clock.monotonic()
        if value == 1:
            self.starts += 1
            self.started = self.clock.monotonic()
            self.ended = None
        else:
            self.stops += 1
            self.ended = self.clock.monotonic()
    def status(self, emergency=False):
        now = self.clock.monotonic()
        state = 3
        if self.started is not None:
            end = self.started+.25+self.parameters.expected_s
            if self.ended is None and now >= end:
                self.ended = end
            if self.ended is None:
                state = 1
            elif now >= self.ended+self.delay:
                self.counter += self.parameters.volume_ml
                self.started = None
        return {"state": state, "error": self.error, "read_at": now,
                "counter_ml": self.counter, "resolution_ml": .0001, "flow_ml_min": 0. if state == 3 else self.parameters.flow_ml_min}


@pytest.mark.parametrize("dose_ml,flow", [(.1,1),(.1,60),(.001,60)])
def test_slow_prepare_short_pulse_delayed_counter_and_second_test(monkeypatch, dose_ml, flow):
    clock = Clock()
    monkeypatch.setattr(module, "time", clock)
    out = []
    w = PumpWorker(Binding("pump","none","none"), lambda name,data: out.append((name,data)), lambda *args: True)
    w.stop_event = Flag(clock)
    w.transport = Device(clock)
    w.hardware = {"signature":"s"}
    for _ in range(2):
        w.execute(Dose(dose_ml,flow,50,purpose="test"))
        assert not w.context and not w.busy
    results = [d for n,d in out if n == "dose_result"]
    assert len(results) == 2
    assert all(not r["error"] and r["amount_ml"] == pytest.approx(dose_ml) for r in results)
    assert w.transport.starts == 2


def test_stop_while_preparing_never_starts(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(module,"time",clock)
    out = []
    w = PumpWorker(Binding("pump","none","none"), lambda n,d:out.append((n,d)), lambda *args:True)
    w.stop_event = Flag(clock)
    w.transport = Device(clock)
    w.hardware = {"signature":"s"}
    def interrupted(*args):
        w.stop_event.set()
        raise Cancelled()
    w.transport.configure_dose = interrupted
    w.execute(Dose(.1,1,50,purpose="test"))
    result = [d for n,d in out if n == "dose_result"][0]
    assert result["amount_ml"] == 0 and not result["started"]
    assert w.transport.starts == 0
