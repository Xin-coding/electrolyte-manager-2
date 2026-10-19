from __future__ import annotations

from collections import deque
from dataclasses import asdict
import threading
import time

import serial
from serial.tools import list_ports

from .wire import Frames, apera_frame, metrohm_values


def ports():
    unique = {}
    for p in list_ports.comports():
        entry = {"port": p.device, "identity": p.serial_number or p.hwid or p.device,
                 "description": p.description, "vid": p.vid, "pid": p.pid}
        # Windows can enumerate a stale interface alongside the active USB one.
        prior = unique.get(p.device)
        if not prior or ("&" in prior["identity"] and "&" not in entry["identity"]):
            unique[p.device] = entry
    return sorted(unique.values(), key=lambda p: p["port"])


def resolve(binding):
    inventory = ports()
    if binding.identity:
        matches = [p for p in inventory if p["identity"] == binding.identity]
        if len(matches) != 1:
            raise OSError("Original bound device is unavailable or ambiguous")
        return matches[0]
    matches = [p for p in inventory if p["port"].casefold() == binding.port.casefold()]
    if len(matches) != 1:
        raise OSError("Select a unique available port")
    return matches[0]


class Metrohm:
    def __init__(self, port, log=lambda *_: None):
        self.port, self.log = port, log
        self.io = None

    def open(self):
        self.io = serial.Serial(self.port, 9600, timeout=.1, write_timeout=.5, rtscts=True)
        firmware = self.query("&Config.Aux.Prog $Q")
        if "5.781." not in firmware:
            raise ValueError("Expected Metrohm 781")
        if '"pH"' not in self.query("&Mode.Select $Q"):
            raise ValueError("Metrohm must be in pH mode")
        return {"model": "Metrohm 781", "firmware": firmware.strip()}

    def query(self, command):
        self.io.reset_input_buffer()
        self.log("tx", command)
        self.io.write((command+"\r\n").encode("ascii"))
        data = bytearray()
        end = time.monotonic()+1.5
        while time.monotonic() < end:
            data.extend(self.io.read(self.io.in_waiting or 1))
            if data.endswith(b"\r\r\n"):
                text = data.decode("cp437", errors="replace")
                self.log("rx", text)
                return text
        raise TimeoutError("Metrohm response timed out")

    def read(self):
        return metrohm_values(self.query("&Info.ActualInfo.MeasValue $Q"))

    def close(self):
        if self.io:
            self.io.close()


class Apera:
    def __init__(self, port, log=lambda *_: None):
        self.port, self.log = port, log
        self.io = None
        self.frames = Frames()

    def send(self, command):
        self.log("tx", command)
        self.io.write(command.encode("ascii"))

    def read(self):
        data = self.io.read(self.io.in_waiting or 1).decode("ascii", errors="replace")
        result = []
        for frame in self.frames.feed(data):
            self.log("rx", frame)
            result.append(apera_frame(frame))
        return result

    def open(self):
        self.io = serial.Serial(self.port, 19200, timeout=.1, write_timeout=.5)
        self.send("&A#")
        end = time.monotonic()+2
        identity = None
        while time.monotonic() < end and identity is None:
            for message in self.read():
                if message["type"] == "identity":
                    identity = message
        if not identity or identity["model"] != "PC8500":
            raise ValueError("Expected Apera PC8500 identity reply")
        time.sleep(.5)
        self.send("&V#")
        return identity

    def close(self):
        if self.io:
            self.io.close()


def identify(kind, port, log=lambda *_: None):
    if kind == "pump":
        from .pump import PumpTransport
        device = PumpTransport(port, log)
        try:
            device.open()
            status = device.status()
            hardware = device.hardware()
            return {"model": "Longer ASP", "status": status, **hardware}
        finally:
            device.close()
    device = (Metrohm if kind == "ph" else Apera)(port, log)
    try:
        return device.open()
    finally:
        device.close()


class SensorWorker:
    def __init__(self, binding, emit, interval=lambda: 2., log=lambda *_: None):
        self.binding, self.emit, self.interval, self.log = binding, emit, interval, log
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, name="em2-"+binding.kind, daemon=True)
        self.expected_serial = None
        self.seen = set()
        self.recent = deque()

    def start(self):
        self.thread.start()

    def close(self):
        self.stop_event.set()

    def run(self):
        kind = self.binding.kind
        while not self.stop_event.is_set():
            device = None
            try:
                endpoint = resolve(self.binding)
                device = (Metrohm if kind == "ph" else Apera)(endpoint["port"], self.log)
                info = device.open()
                if self.expected_serial and info.get("serial") != self.expected_serial:
                    raise ValueError("Instrument identity changed")
                self.expected_serial = info.get("serial")
                self.emit("connection", {"kind": kind, "connected": True, "endpoint": endpoint, "info": info})
                next_read = 0.
                previous_frame_at = None
                while not self.stop_event.is_set():
                    now = time.monotonic()
                    if kind == "ph":
                        value, temperature = device.read()
                        self.emit("sample", {"kind": kind, "value": value, "temperature": temperature,
                                             "mono": now, "identity": endpoint["identity"], "live": True})
                        self.stop_event.wait(max(.05, self.interval()-(time.monotonic()-now)))
                        continue
                    # PC8500 identity handshake is only valid at connection.
                    # Re-sending &A# during PC-Link produces no reliable reply.
                    # USB failures are detected by the serial driver; quiet
                    # intervals remain waiting for a genuinely new record.
                    if now >= next_read:
                        device.send("&B#")
                        next_read = now+1
                    for message in device.read():
                        if message["type"] == "identity":
                            if message["serial"] != self.expected_serial:
                                raise ValueError("Instrument identity changed")
                        if message["type"] != "measurement" or message["key"] in self.seen:
                            continue
                        self.seen.add(message["key"])
                        self.recent.append(message["key"])
                        while len(self.recent) > 4096:
                            self.seen.discard(self.recent.popleft())
                        received = time.monotonic()
                        # The first/burst frame after connection can be queued data.
                        # Keep it in the record, but never use it to resume dosing.
                        live = message["frame_type"] == "D" and previous_frame_at is not None and received-previous_frame_at >= 30
                        previous_frame_at = received
                        self.emit("sample", {"kind": "ec", "value": message["value"], "temperature": message["temperature"],
                                             "device_time": message["device_time"], "identity": endpoint["identity"],
                                             "mono": received, "live": live})
            except Exception as exc:
                self.emit("connection", {"kind": kind, "connected": False, "error": str(exc)})
            finally:
                if device:
                    device.close()
            self.stop_event.wait(2)
        self.emit("connection", {"kind": kind, "connected": False, "closed": True})
