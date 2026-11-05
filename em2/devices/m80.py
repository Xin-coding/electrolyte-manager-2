"""Read-only M80 SM Modbus driver. No configuration or calibration commands."""
from __future__ import annotations

import math
import struct
import time

import serial

from .wire import crc


CONDUCTIVITY_FACTORS = {9: 1000., 10: 1., 11: .001, 12: .000001,
                        13: 10., 14: .01, 15: .00001, 16: .00000001}


def ascii_words(words):
    return b"".join(w.to_bytes(2, "big") for w in words).decode("ascii").strip(" \0")


def next_poll(previous, now, interval=1.):
    """Keep a monotonic cadence; skip missed slots instead of issuing a burst."""
    due = previous + interval
    if due < now:
        due += (math.floor((now-due)/interval)+1)*interval
    return due


class M80:
    def __init__(self, port, log=lambda *_: None, address=1):
        if type(address) is not int or not 1 <= address <= 247:
            raise ValueError("M80 address must be 1–247")
        self.port, self.log, self.address = port, log, address
        self.io = None
        self.last_request = 0.
        self.timeout = .7

    def close(self):
        if self.io is not None:
            self.io.close()
            self.io = None

    def _read_exact(self, count, deadline):
        data = bytearray()
        while len(data) < count and time.monotonic() < deadline:
            data.extend(self.io.read(count-len(data)))
        if len(data) != count:
            raise TimeoutError("M80 response timed out or is incomplete")
        return bytes(data)

    def registers(self, start, count):
        if not 0 <= start <= 65535 or not 1 <= count <= 125:
            raise ValueError("Invalid M80 register read")
        time.sleep(max(0., .004-(time.monotonic()-self.last_request)))
        body = struct.pack(">BBHH", self.address, 3, start, count)
        request = body+crc(body)
        self.io.reset_input_buffer()
        self.log("tx", "M80 "+request.hex(" "))
        self.io.write(request)
        self.io.flush()
        deadline = time.monotonic()+self.timeout
        header = self._read_exact(3, deadline)
        if header[0] != self.address:
            raise ValueError("M80 response address mismatch")
        if header[1] not in (3, 0x83):
            raise ValueError("M80 response function mismatch")
        if header[1] == 3 and header[2] != count*2:
            raise ValueError("M80 response byte count mismatch")
        rest = self._read_exact(2 if header[1] == 0x83 else count*2+2, deadline)
        frame = header+rest
        self.last_request = time.monotonic()
        self.log("rx", "M80 "+frame.hex(" "))
        if crc(frame[:-2]) != frame[-2:]:
            raise ValueError("M80 response CRC mismatch")
        if header[1] == 0x83:
            raise ValueError(f"M80 Modbus exception {header[2]}")
        return list(struct.unpack(">"+"H"*count, frame[3:-2]))

    def open(self):
        try:
            self.io = serial.Serial(self.port, 38400, bytesize=8, parity="N", stopbits=1,
                                    timeout=.05, write_timeout=.7, rtscts=False,
                                    dsrdtr=False, xonxoff=False)
            part = ascii_words(self.registers(280, 8))
            manufacturer = ascii_words(self.registers(320, 8))
            if part != "30530566" or "METTLER" not in manufacturer.upper():
                raise ValueError("Expected METTLER TOLEDO M80 SM identity")
            serial_number = ascii_words(self.registers(312, 8))
            sensor = ascii_words(self.registers(436, 8))
            if self.registers(336, 1) != [0x0103]:
                raise ValueError("M80 is not configured for conductivity; no settings were written")
            units = self.registers(120, 8)
            if units[0] != 5 or units[1] not in CONDUCTIVITY_FACTORS or units[2] != 5 or units[3] not in (3, 4):
                raise ValueError("M80 channels 1/2 must be conductivity and temperature")
            self.factor, self.fahrenheit = CONDUCTIVITY_FACTORS[units[1]], units[3] == 4
            return {"model": "METTLER TOLEDO M80 SM", "serial": serial_number,
                    "sensor": sensor, "units": units, "driver": "m80", "interval_s": 1.,
                    "serial_settings": f"38400 8N1 · Modbus {self.address} · 1 s"}
        except Exception:
            self.close()
            raise

    def read(self):
        # Contiguous status + four channels, one coherent read per second.
        words = self.registers(98, 10)
        values = struct.unpack(">ffff", struct.pack(">8H", *words[2:]))
        problem = ("sensor_disconnected" if words[0] else
                   "sensor_type_mismatch" if words[1] else
                   "invalid_measurement" if not all(math.isfinite(v) for v in values[:2]) else "")
        if problem:
            return {"value": None, "temperature": None, "quality": problem, "live": False}
        value = values[0]*self.factor
        temperature = (values[1]-32)*5/9 if self.fahrenheit else values[1]
        if value < 0 or not math.isfinite(value):
            return {"value": None, "temperature": temperature, "quality": "invalid_measurement", "live": False}
        return {"value": value, "temperature": temperature, "quality": "valid", "live": True}
