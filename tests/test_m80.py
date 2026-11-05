import math
import struct
import time

import pytest

from em2.devices import m80
from em2.devices.wire import crc


class Port:
    def __init__(self, *args, **kwargs):
        self.settings = kwargs
        self.pending = bytearray()
        self.commands = []
        self.closed = False
        self.chunk = 3
        self.fault = None
        self.values = (.2128, 21.5, 4699., 70.7)
        self.flags = [0, 0]
        self.units = [5, 10, 5, 3, 5, 6, 5, 4]
        self.mode = 0x0103
        self.part = "30530566"

    def reset_input_buffer(self):
        self.pending.clear()

    def write(self, request):
        assert request[1] == 3, "Driver must never write M80 configuration"
        assert request[-2:] == crc(request[:-2])
        self.commands.append(request)
        address, _, start, count = struct.unpack(">BBHH", request[:-2])
        strings = {280: self.part, 320: "METTLER TOLEDO", 312: "fixture-M80", 436: "InPro7100i"}
        if start in strings:
            payload = strings[start].ljust(count*2).encode()
        else:
            words = {336: [self.mode], 120: self.units,
                     98: self.flags+list(struct.unpack(">8H", struct.pack(">ffff", *self.values)))}[start]
            payload = struct.pack(">"+"H"*len(words), *words)
        body = bytes([address, 3, len(payload)])+payload
        if self.fault == "exception":
            body = bytes([address, 0x83, 2])
        if self.fault == "address":
            body = bytes([address+1])+body[1:]
        if self.fault == "function":
            body = bytes([address, 4])+body[2:]
        if self.fault == "count":
            body = body[:2]+bytes([2])+body[3:]
        frame = body+crc(body)
        if self.fault == "crc":
            frame = frame[:-1]+bytes([frame[-1] ^ 255])
        if self.fault == "short":
            frame = frame[:-3]
        if self.fault == "timeout":
            frame = b""
        self.pending.extend(frame)
        return len(request)

    def read(self, count):
        count = min(count, self.chunk)
        data = bytes(self.pending[:count])
        del self.pending[:count]
        if not data:
            time.sleep(.001)
        return data

    def flush(self):
        pass

    def close(self):
        self.closed = True


@pytest.fixture
def meter(monkeypatch):
    port = Port()
    monkeypatch.setattr(m80.serial, "Serial", lambda *args, **kwargs: port)
    device = m80.M80("fixture", address=7)
    device.timeout = .03
    yield device, port
    device.close()


def test_identity_fragmented_read_and_fixed_parameters(monkeypatch):
    ports = []
    def factory(*args, **kwargs):
        p = Port(*args, **kwargs)
        ports.append(p)
        assert args == ("fixture", 38400)
        assert kwargs["parity"] == "N" and kwargs["stopbits"] == 1
        assert not kwargs["rtscts"] and not kwargs["dsrdtr"]
        return p
    monkeypatch.setattr(m80.serial, "Serial", factory)
    d = m80.M80("fixture")
    info = d.open()
    assert info["serial"] == "fixture-M80" and info["interval_s"] == 1.
    assert d.read()["value"] == pytest.approx(.2128)
    assert d.read()["temperature"] == 21.5
    d.close()
    assert ports[0].closed


@pytest.mark.parametrize("unit,factor", list(m80.CONDUCTIVITY_FACTORS.items()))
def test_unit_normalization(meter, unit, factor):
    device, port = meter
    port.units[1] = unit
    port.units[3] = 4
    port.values = (2., 77., 0., 0.)
    device.open()
    reading = device.read()
    assert reading["value"] == pytest.approx(2*factor)
    assert reading["temperature"] == 25.


@pytest.mark.parametrize("fault", ["crc", "short", "timeout", "exception", "address", "function", "count"])
def test_bad_frames_never_become_samples(meter, fault):
    device, port = meter
    device.open()
    port.fault = fault
    with pytest.raises((ValueError, TimeoutError)):
        device.read()


@pytest.mark.parametrize("failure", ["identity", "mode", "units"])
def test_failed_open_releases_port(meter, failure):
    device, port = meter
    if failure == "identity": port.part = "OTHER"
    if failure == "mode": port.mode = 0x0100
    if failure == "units": port.units[1] = 17
    with pytest.raises(ValueError):
        device.open()
    assert port.closed and device.io is None


@pytest.mark.parametrize("values,flags,quality", [((math.nan, 20., 0., 0.), [0, 0], "invalid_measurement"),
    ((1., math.inf, 0., 0.), [0, 0], "invalid_measurement"),
    ((1., 20., 0., 0.), [1, 0], "sensor_disconnected"),
    ((1., 20., 0., 0.), [0, 1], "sensor_type_mismatch")])
def test_invalid_measurement_not_zero_or_live(meter, values, flags, quality):
    device, port = meter
    device.open()
    port.values, port.flags = values, flags
    reading = device.read()
    assert reading == {"value": None, "temperature": None, "quality": quality, "live": False}


def test_monotonic_schedule_does_not_accumulate_read_time_or_catch_up():
    assert m80.next_poll(0, .2) == 1
    assert m80.next_poll(1, 1.3) == 2
    assert m80.next_poll(2, 4.3) == 5
    assert m80.next_poll(5, 5.8) == 6
