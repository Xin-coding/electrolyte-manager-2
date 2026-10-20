"""Charge derived only from declared current and monotonic recording time."""
import math
from .forms import parse_number


def current_value(metadata):
    raw = metadata.get("current_a", "")
    value, error = parse_number(str(raw if raw is not None else ""))
    return None if error else value


class Charge:
    def __init__(self):
        self.started = False
        self.running = False
        self.last = 0.
        self.current = None
        self.coulombs = 0.
        self.known_s = 0.
        self.unknown_s = 0.
        self.overflow = False

    def begin(self, now, current):
        self.__init__()
        self.started = self.running = True
        self.last, self.current = now, current

    def advance(self, now):
        if not self.running:
            return
        seconds = max(0., now-self.last)
        if self.current is None:
            self.unknown_s += seconds
        else:
            self.known_s += seconds
            total = self.coulombs+self.current*seconds
            if math.isfinite(total):
                self.coulombs = total
            else:
                self.overflow = True
        self.last = max(self.last, now)

    def change(self, now, current):
        self.advance(now)
        self.current = current

    def finish(self, now):
        self.advance(now)
        self.running = False

    def snapshot(self, now):
        self.advance(now)
        return {"coulombs": self.coulombs,
                "available": self.started and self.current is not None and not self.overflow,
                "partial": self.unknown_s > 1e-6,
                "known_s": self.known_s, "unknown_s": self.unknown_s,
                "current_a": self.current, "estimated": True, "overflow": self.overflow}
