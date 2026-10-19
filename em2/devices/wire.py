from __future__ import annotations

from datetime import datetime
import math
import re
import struct

from ..errors import ParameterError, CommunicationError


def crc(data):
    value = 0xffff
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ (0xa001 if value & 1 else 0)
    return value.to_bytes(2, "little")


def packet(function, register, values, address=1):
    for name, value, maximum in (("address", address, 255), ("function", function, 255), ("register", register, 65535)):
        if type(value) is not int or not 0 <= value <= maximum:
            raise ParameterError(f"Pump packet {name} must be an integer in 0..{maximum}")
    words = values if function == 16 else [values]
    if not isinstance(words, (list, tuple)) or not words:
        raise ParameterError("Pump packet register values must be a non-empty integer sequence")
    for index, value in enumerate(words):
        if type(value) is not int or not 0 <= value <= 65535:
            raise ParameterError(f"Pump register 0x{register+index:04X}: expected integer 0..65535, got {value!r}")
    if function == 16 and len(words) > 123:
        raise ParameterError("Too many pump register values")
    if function in (3, 6):
        body = struct.pack(">BBHH", address, function, register, values)
    elif function == 16:
        body = struct.pack(">BBHHB", address, function, register, len(values), len(values)*2)
        body += struct.pack(">"+"H"*len(values), *values)
    else:
        raise ValueError("Unsupported Modbus function")
    return body+crc(body)


def response(raw, function, address=1):
    if len(raw) < 5 or crc(raw[:-2]) != raw[-2:]:
        raise CommunicationError("Incomplete or corrupt pump response")
    if raw[0] != address:
        raise CommunicationError("Pump address mismatch")
    if raw[1] == function | 128:
        raise CommunicationError(f"Pump protocol exception {raw[2]}")
    if raw[1] != function:
        raise CommunicationError("Pump function mismatch")
    if function == 3:
        if len(raw) != raw[2]+5 or raw[2] % 2:
            raise CommunicationError("Pump register response length mismatch")
        return list(struct.unpack(">"+"H"*(raw[2]//2), raw[3:-2]))
    if len(raw) != 8:
        raise CommunicationError("Pump write response length mismatch")
    return struct.unpack(">HH", raw[2:6])


def volume(pair):
    n, u = pair
    if not 92 <= u <= 106:
        raise ValueError("Unsupported volume unit")
    return n*10.**(u-103)


def flow(pair):
    n, u = pair
    for offset, factor in ((0, 1.), (100, 60.), (200, 1/60)):
        if 94 <= u-offset <= 106:
            return n*10.**(u-offset-103)*factor
    raise ValueError("Unsupported flow unit")


TIME_UNITS = {97: .001, 98: .01, 99: .1, 100: 1., 101: 6., 102: 60., 103: 360., 104: 3600.}


def duration(pair):
    if pair[1] not in TIME_UNITS or pair[0] <= 0:
        raise ValueError("Unsupported pump time unit")
    return pair[0]*TIME_UNITS[pair[1]]


def encode(value, minimum=94):
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Pump parameter must be positive")
    candidates = []
    for u in range(minimum, 107):
        scaled = value/10.**(u-103)
        if not math.isfinite(scaled):
            continue
        n = round(scaled)
        if 1 <= n <= 9999:
            candidates.append((abs(n*10.**(u-103)-value), u > 103, abs(u-103), n, u))
    if not candidates:
        raise ValueError("Pump parameter cannot be encoded")
    error, _, _, n, u = min(candidates)
    if error > max(1e-12, value*.001):
        raise ValueError("Pump parameter encoding is too coarse")
    return (n, u)


def metrohm_values(text):
    main = re.search(r'\.Primary"\s*([-+\d.eE]+)"', text)
    secondary = re.search(r'\.\.Secondary"\s*([-+\d.eE]+)"', text)
    if not main:
        raise ValueError("Invalid Metrohm measurement response")
    value = float(main.group(1))
    temperature = float(secondary.group(1)) if secondary else None
    if not math.isfinite(value) or temperature is not None and not math.isfinite(temperature):
        raise ValueError("Non-finite Metrohm measurement")
    return value, temperature


class Frames:
    def __init__(self):
        self.buffer = ""

    def feed(self, text):
        self.buffer = (self.buffer+text)[-8192:]
        frames = []
        while "<" in self.buffer:
            start = self.buffer.index("<")
            end = self.buffer.find(">", start)
            if end < 0:
                self.buffer = self.buffer[start:]
                break
            frames.append(self.buffer[start:end+1])
            self.buffer = self.buffer[end+1:]
        return frames


def apera_frame(frame):
    if not frame.startswith("<") or not frame.endswith(">"):
        raise ValueError("Invalid Apera frame")
    p = frame[1:-1]
    if p.startswith("N") and len(p) == 17:
        return {"type": "identity", "model": p[1:7], "serial": p[7:17]}
    if p[:1] not in {"D", "S"}:
        return {"type": "other"}
    if len(p) != 17:
        raise ValueError("Invalid Apera measurement length")
    a, b, c, d = [int(p[i:i+4], 16) for i in (1, 5, 9, 13)]
    mode, encoded = divmod(a, 4096)
    unit, date = divmod(b, 2048)
    year, month = divmod(date, 16)
    day, tm = divmod(c, 2048)
    hour, minute = divmod(tm, 64)
    places, tail = divmod(d, 8192)
    if places > 3:
        raise ValueError("Invalid Apera decimal precision")
    _, tail = divmod(tail, 4096)
    _, tail = divmod(tail, 2048)
    temperature = (tail-500)/10.
    value = (encoded-2000)/10.**places
    if mode != 2 or unit not in {10, 11}:
        return {"type": "other"}
    if unit == 11:
        value /= 1000
    stamp = datetime(2000+year, month, day, hour, minute).isoformat()
    return {"type": "measurement", "kind": "ec", "value": value, "temperature": temperature,
            "device_time": stamp, "frame_type": p[0], "key": p[1:]}
