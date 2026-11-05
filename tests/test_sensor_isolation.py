import json
import threading
import time

import pytest

from em2.devices import sensors
from em2.models import Binding
from em2.runtime import Runtime
import em2.runtime as runtime_module
from em2 import storage
from test_m80 import Port as M80Port


def wait(predicate, seconds=5):
    deadline = time.monotonic()+seconds
    while time.monotonic() < deadline:
        if predicate(): return
        time.sleep(.01)
    assert predicate()


class TextPort:
    def __init__(self, port, baud, **kwargs):
        self.kind = port
        self.pending = bytearray()
        self.commands = []
        self.cts, self.dsr = False, True
        self.rtscts, self.dsrdtr = kwargs.get("rtscts"), kwargs.get("dsrdtr")
        self.closed = False
        self.record = 0
        assert baud == (9600 if port == "ph" else 19200)

    @property
    def in_waiting(self): return len(self.pending)

    def reset_input_buffer(self): self.pending.clear()

    def write(self, command):
        self.commands.append(command)
        if self.kind == "ph":
            replies = {b'&Config.Aux.Prog $Q\r\n': '"5.781.0020"', b'&Mode.Select $Q\r\n': '"pH"',
                       b'&Info.ActualInfo.MeasValue $Q\r\n': '.Primary"7.344"\r\n..Secondary"21.5"'}
            self.pending.extend((replies[command]+"\r\r\n").encode())
        elif command == b"&A#":
            self.pending.extend(b"<NPC85001234567890>")
        elif command == b"&B#":
            self.record += 1
            words = (2*4096+2000+1450, 10*2048+26*16+10, 19*2048+14*64+self.record, 8192+2048+500+234)
            self.pending.extend(("<D"+"".join(f"{v:04X}" for v in words)+">").encode())
        else:
            assert command == b"&V#"
        return len(command)

    def read(self, count):
        count = min(count, 7)
        data = bytes(self.pending[:count])
        del self.pending[:count]
        if not data: time.sleep(.005)
        return data

    def close(self): self.closed = True


@pytest.fixture
def serial_fixture(monkeypatch):
    made = []
    def factory(port, baud, **kwargs):
        item = M80Port() if port == "m80" else TextPort(port, baud, **kwargs)
        item.port = port
        made.append(item)
        return item
    inventory = [{"port": p, "identity": "fixture-"+p, "description": "fixture"} for p in ("ph", "apera", "m80")]
    monkeypatch.setattr(sensors.serial, "Serial", factory)
    monkeypatch.setattr(sensors, "ports", lambda: inventory)
    return made


@pytest.mark.parametrize("devices", [("ph",), ("apera",), ("m80",), ("ph", "apera"), ("ph", "m80")])
def test_independent_device_recording_without_pump(tmp_path, serial_fixture, devices):
    r = Runtime(tmp_path)
    try:
        if "m80" in devices:
            r.command("select_conductivity", driver="m80")
            wait(lambda: r.snapshot.get("ec_choice", {}).get("driver") == "m80")
        for device in devices:
            kind = "ph" if device == "ph" else "ec"
            r.command("connect", kind=kind, port=device, identity="fixture-"+device,
                      driver="m80" if device == "m80" else "")
        wait(lambda: all(r.snapshot.get("connections", {}).get("ph" if p == "ph" else "ec", {}).get("connected") for p in devices))
        r.command("start_recording", name="Independent sensors")
        wait(lambda: r.snapshot.get("recording"))
        path = r.snapshot["path"]
        kinds = {"ph" if p == "ph" else "ec" for p in devices}
        wait(lambda: all(r.sequence[k] >= 2 for k in kinds), 6)
        assert "pump" not in r.workers
        if len(devices) == 2:
            r.command("disconnect", kind="ph")
            wait(lambda: "ph" not in r.snapshot["bindings"])
            before = r.sequence["ec"]
            wait(lambda: r.sequence["ec"] > before)
            assert r.snapshot["recording"]
        if "m80" in devices:
            before = r.sequence["ec"]
            r.command("disconnect", kind="ec")
            wait(lambda: "ec" not in r.snapshot["bindings"])
            r.command("connect", kind="ec", port="m80", identity="fixture-m80", driver="m80")
            wait(lambda: r.sequence["ec"] > before)
        r.command("finish")
        wait(lambda: not r.snapshot.get("recording"))
        with storage.readonly(path) as db:
            rows = list(db.execute("SELECT kind,raw,live FROM samples"))
            assert {x[0] for x in rows} == kinds
            assert all(x[1] is not None for x in rows)
            if "m80" in devices:
                assert all(x[2] == 1 for x in rows if x[0] == "ec")
            if "apera" in devices:
                # Initial and rapid PC8500 frames remain buffered, not new control samples.
                assert all(x[2] == 0 for x in rows if x[0] == "ec")
        storage.export_data(path, tmp_path/"export")
        assert list((tmp_path/"export").glob("*.csv"))
    finally:
        r.close()
        r.thread.join(10)
    assert all(port.closed for port in serial_fixture)
    for port in serial_fixture:
        if port.port == "apera": assert port.commands.count(b"&A#") == 1


def test_binding_compatibility_preferences_and_change_lock(tmp_path, serial_fixture):
    assert Binding("ec", "apera", "fixture-apera").sensor_driver == "apera_pc8500"
    r = Runtime(tmp_path)
    try:
        r.select_conductivity("m80", 7)
        assert json.loads((tmp_path/"data/devices.json").read_text())["address"] == 7
        r.command("connect", kind="ec", port="m80", identity="fixture-m80", driver="m80", address=7)
        wait(lambda: "ec" in r.snapshot["bindings"])
        with pytest.raises(ValueError, match="disconnect"):
            r.select_conductivity("apera_pc8500")
        with pytest.raises(ValueError, match="already owned"):
            r.connect(Binding("ph", "M80", "other"))
        r.command("disconnect", kind="ec")
        wait(lambda: "ec" not in r.snapshot["bindings"])
        r.command("start_recording", name="Lock")
        wait(lambda: r.snapshot.get("recording"))
        with pytest.raises(ValueError, match="recording"):
            r.select_conductivity("apera_pc8500")
    finally:
        r.close(); r.thread.join(10)


def test_late_connection_events_and_invalid_samples_are_isolated(tmp_path):
    r = Runtime(tmp_path)
    try:
        r.connection_ids["ec"] = 2
        r.emit("sample", {"kind": "ec", "value": 2., "mono": time.monotonic(), "_device_kind": "ec", "_connection_id": 1})
        r.emit("sample", {"kind": "ec", "value": 3., "mono": time.monotonic(), "_device_kind": "ec", "_connection_id": 2})
        wait(lambda: r.samples.get("ec", {}).get("value") == 3.)
        assert r.sequence["ec"] == 1
        r.emit("sample", {"kind": "ec", "value": None, "mono": time.monotonic(), "quality": "invalid_measurement", "live": False})
        wait(lambda: r.samples.get("ec", {}).get("value") is None)
        assert "ec" not in r.engine.latest
    finally:
        r.close(); r.thread.join(10)


def test_scan_only_uses_selected_ec_driver_and_skips_owned_ports(monkeypatch):
    r = Runtime.__new__(Runtime)
    r.scan_busy = False
    r.ec_choice = {"driver": "m80", "address": 7}
    r.bindings = {"ph": Binding("ph", "occupied", "occupied-id")}
    r.workers = {"ph": object()}
    r.trace = lambda *_: None
    calls, events = [], []
    done = threading.Event()
    monkeypatch.setattr(runtime_module, "ports", lambda: [{"port": "occupied", "identity": "occupied-id"}, {"port": "m80", "identity": "m80-id"}])
    def identify(kind, port, log, **options):
        calls.append((kind, port, options))
        return {"model": "M80"}
    monkeypatch.setattr(runtime_module, "identify", identify)
    r.emit = lambda name, value: (events.append((name, value)), done.set())
    r.scan()
    assert done.wait(2)
    assert calls == [("ec", "m80", {"driver": "m80", "address": 7})]
    assert events[0][1]["found"][0][0].sensor_driver == "m80"
