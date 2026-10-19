from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import math
from typing import Literal
from uuid import uuid4

from .errors import ParameterError
from .parameters import force_integer, pump_parameters


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


@dataclass
class SignalSettings:
    target: float | None = None
    tolerance: float | None = None
    lower: float | None = None
    upper: float | None = None
    direction: str = "raise"

    def validate(self, complete=False):
        if self.direction not in {"raise", "lower"}:
            raise ValueError("Invalid control direction")
        for name in ("target", "tolerance", "lower", "upper"):
            value = getattr(self, name)
            if value is not None and not finite(value):
                raise ValueError(f"Invalid {name}")
        if self.tolerance is not None and self.tolerance <= 0:
            raise ValueError("Tolerance must be positive")
        if self.lower is not None and self.upper is not None and self.lower >= self.upper:
            raise ValueError("Alarm lower bound must be below upper bound")
        if complete and any(getattr(self, n) is None for n in ("tolerance", "lower", "upper")):
            raise ValueError("Set tolerance and both alarm boundaries")
        if self.target is not None and self.tolerance is not None and self.lower is not None and self.upper is not None:
            if not self.lower < self.target-self.tolerance < self.target+self.tolerance < self.upper:
                raise ValueError("Target band must be inside the alarm boundaries")


@dataclass
class Settings:
    version: int = 1
    control: str = "ph"
    ph_interval_s: float = 2.0
    smoothing: bool = True
    dose_ml: float | None = None
    flow_ml_min: float | None = None
    force_percent: int | None = None
    mix_s: float | None = None
    ph: SignalSettings = field(default_factory=SignalSettings)
    ec: SignalSettings = field(default_factory=SignalSettings)
    metadata: dict = field(default_factory=dict)

    def payload(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data, *, validate=True):
        keys = set(cls.__dataclass_fields__)
        if not isinstance(data, dict) or set(data)-keys:
            raise ValueError("Unsupported settings fields")
        value = dict(data)
        for kind in ("ph", "ec"):
            value[kind] = SignalSettings(**value.get(kind, {}))
        result = cls(**value)
        if validate:
            result.validate()
        return result

    def validate(self, complete=False):
        if self.control not in {"ph", "ec"}:
            raise ValueError("Invalid control signal")
        if not finite(self.ph_interval_s) or not .5 <= self.ph_interval_s <= 3600:
            raise ValueError("pH sampling interval must be 0.5–3600 s")
        if not isinstance(self.smoothing, bool) or not isinstance(self.metadata, dict):
            raise ValueError("Invalid settings")
        for key in ("dose_ml", "flow_ml_min", "mix_s"):
            v = getattr(self, key)
            if v is not None and (not finite(v) or v < 0 or key != "mix_s" and v == 0):
                raise ValueError(f"Invalid {key}")
        if self.force_percent is not None:
            self.force_percent = force_integer(self.force_percent)
        pump_parameters(self.dose_ml, self.flow_ml_min, self.force_percent, self.mix_s)
        self.ph.validate(complete and self.control == "ph")
        self.ec.validate(complete and self.control == "ec")
        if complete and any(getattr(self, k) is None for k in ("dose_ml", "flow_ml_min", "mix_s", "force_percent")):
            raise ValueError("Set dose, flow, force and mixing time")


@dataclass(frozen=True)
class Binding:
    kind: str
    port: str
    identity: str
    label: str = ""


@dataclass(frozen=True)
class Sample:
    kind: str
    value: float
    mono: float
    sequence: int
    identity: str = ""
    temperature: float | None = None
    device_time: str | None = None
    host_time: str = field(default_factory=utc_now)
    quality: str = "valid"
    live: bool = True

    @property
    def valid(self):
        return self.kind in {"ph", "ec"} and self.quality == "valid" and finite(self.value)


@dataclass
class Load:
    id: str = field(default_factory=lambda: uuid4().hex)
    limit_ml: float | None = None
    used_ml: float = 0.0
    identity: str = ""
    uncertain: bool = False
    estimated: bool = False

    @property
    def remaining(self):
        return None if self.limit_ml is None else max(0., self.limit_ml-self.used_ml)

    def allows(self, volume):
        return not self.uncertain and self.remaining is not None and volume <= self.remaining + 1e-10


@dataclass(frozen=True)
class Dose:
    volume_ml: float
    flow_ml_min: float
    force_percent: int
    mix_s: float = 0.
    purpose: Literal["experiment", "test", "stop_test"] = "experiment"
    hardware_cap_ml: float | None = None
    settings_version: int = 0
    id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self):
        object.__setattr__(self, "force_percent", force_integer(self.force_percent))
        if not finite(self.volume_ml) or self.volume_ml <= 0 or not finite(self.flow_ml_min) or self.flow_ml_min <= 0:
            raise ParameterError("Dose volume and flow must be positive")
        if not finite(self.mix_s) or self.mix_s < 0:
            raise ParameterError("Mixing time must be non-negative")
        if self.purpose not in {"experiment", "test", "stop_test"}:
            raise ParameterError("Invalid dose purpose")
        if self.purpose == "stop_test":
            if self.hardware_cap_ml is None or not finite(self.cap) or self.cap <= self.volume_ml:
                raise ParameterError("STOP test hardware cap must exceed its software cutoff")
        elif self.hardware_cap_ml is not None and self.hardware_cap_ml != self.volume_ml:
            raise ParameterError("Finite dose hardware cap must equal the requested volume")
        pump_parameters(self.cap, self.flow_ml_min, self.force_percent, self.mix_s)

    @property
    def cap(self):
        return self.volume_ml if self.hardware_cap_ml is None else self.hardware_cap_ml

    @property
    def expected_s(self):
        return self.volume_ml/self.flow_ml_min*60


@dataclass
class DoseResult:
    id: str
    stopped: bool
    stopped_at: float
    amount_ml: float | None
    source: str
    started: bool = True
    error: str = ""
    interrupted: bool = False
    recovered: bool = False
    evidence: dict = field(default_factory=dict)
    error_kind: str = ""
    warnings: list[str] = field(default_factory=list)
