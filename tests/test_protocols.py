import struct
import threading
import pytest

from em2.devices.wire import *
from em2.devices.pump import PumpTransport, PumpWorker, Cancelled, assess_counter
from em2.models import Dose, Binding
from em2.units import hydroxide


def test_crc_read_and_corruption():
    request = packet(3, 0, 10)
    assert request.hex() == "01030000000ac5cd"
    raw = bytes.fromhex("01030400010002")
    assert response(raw+crc(raw), 3) == [1, 2]
    with pytest.raises(ValueError):
        response(raw+b"xx", 3)


@pytest.mark.parametrize("x", [.001, .1, .101, 1, 9.99, 10, 100])
def test_encode_units(x):
    assert flow(encode(x)) == pytest.approx(x)
    assert volume(encode(x, 92)) == pytest.approx(x)
    if x == 10:
        assert encode(x) == (10, 103)


def test_counter_resolution_and_coarse():
    b = {"counter_ml": 40, "resolution_ml": .001}
    a = {"counter_ml": 40.101, "resolution_ml": .001}
    assert assess_counter(b, a, Dose(.1, 1, 50))[0] == pytest.approx(.101)
    a["counter_ml"] = 40.05
    assert assess_counter(b, a, Dose(.1, 1, 50))[0] == pytest.approx(.05)
    assert assess_counter(b, a, Dose(.1, 1, 50), True)[0] == pytest.approx(.05)
    b["resolution_ml"] = a["resolution_ml"] = 1
    assert assess_counter(b, a, Dose(.1, 1, 50)) == (.1, "bounded_estimate")
    assert assess_counter(b, a, Dose(.1, 1, 50), True)[0] is None


def test_metrohm_and_apera():
    assert metrohm_values('.Primary" 7.001" ..Secondary"23.4"') == (7.001, 23.4)
    assert apera_frame("<NPC85001234567890>")["serial"] == "1234567890"
    # 145.00 mS/cm cannot fit 12-bit signed-offset value at two decimals;
    # instrument uses one decimal for this measurement.
    a = 2*4096+2000+1450
    b = 10*2048+26*16+10
    c = 19*2048+14*64+30
    d = 1*8192+2048+500+234
    frame = "<D"+"".join(f"{v:04X}" for v in (a,b,c,d))+">"
    parsed = apera_frame(frame)
    assert parsed["value"] == 145
    assert parsed["temperature"] == 23.4
    assert parsed["device_time"] == "2026-10-19T14:30:00"
    f = Frames()
    assert f.feed(frame[:5]) == []
    assert f.feed(frame[5:]) == [frame]
    f.feed("x"*100000)
    assert len(f.buffer) <= 8192


def test_oh_temperature_and_missing():
    assert hydroxide(14, 25) == pytest.approx(1013.094, rel=.001)
    assert math.isnan(hydroxide(14, None))
    assert math.isnan(hydroxide(14, 101))
    assert hydroxide(14, 30) > hydroxide(14, 25)


def test_stop_priority_aborts_normal_exchange():
    flag = threading.Event()
    flag.set()
    pump = PumpTransport("unused", stop_event=flag)
    with pytest.raises(Cancelled):
        pump.read(0, 1)


class RegisterPump(PumpTransport):
    def __init__(self):
        self.calls = []
        self.regs = {}
    def hardware(self):
        return {"signature": "same", "capacity_ml": 60, "force_percent": 50}
    def checked(self, reg, value):
        self.calls.append((reg, value))
    def pair(self, reg, value):
        self.calls.append((reg, value))
        self.regs[reg] = value
    def read(self, reg, count):
        r = [0]*15
        r[:2], r[2:4], r[13:15] = self.regs[0x68], self.regs[0x6a], [6, 100]
        return r


def test_firmware_refresh_same_flow_every_dose():
    p = RegisterPump()
    for _ in range(2):
        p.configure_dose(Dose(.1, 1, 50), "same")
    relevant = [reg for reg, value in p.calls if reg in (0x68,0x6a)]
    assert relevant == [0x68,0x6a,0x6a]*2


def test_unknown_start_ack_is_never_retried():
    worker = PumpWorker(Binding("pump", "none", "none"), lambda *args: None, lambda *args: True)
    class LostAck:
        last_tx = 1
        starts = 0
        def status(self):
            return {"state": 3, "error": 0, "counter_ml": 0, "resolution_ml": .001}
        def configure_dose(self, dose, sig):
            return {"hardware": {"signature": "same"}}
        def write(self, reg, value):
            if value == 1:
                self.starts += 1
                raise TimeoutError("lost START reply")
    p = LostAck()
    worker.transport = p
    worker.hardware = {"signature": "same"}
    seen = []
    worker._finish_context = lambda: seen.append(worker.context.copy()) or True
    worker.execute(Dose(.1,1,50))
    assert p.starts == 1
    assert seen[0]["started"] and seen[0]["interrupted"]
