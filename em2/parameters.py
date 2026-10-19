"""Pure pump parameter validation; no ports or device actions."""
import math

from .devices.wire import TIME_UNITS, encode, volume
from .errors import ParameterError


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def force_integer(value):
    if not finite(value) or int(value) != value or not 20 <= value <= 100:
        raise ParameterError("Pump force must be an integer from 20 to 100 percent / 推力须为 20–100 的整数")
    return int(value)


def pump_parameters(cap=None, speed=None, force=None, mixing=None):
    """Check partial forms or complete commands before any parameter writes."""
    encoded = {}
    if force is not None:
        encoded["force"] = force_integer(force)
    for label, value, allow_zero in (("Volume / 体积", cap, False), ("Flow / 速率", speed, False), ("Mixing time / 混合时间", mixing, True)):
        if value is not None and (not finite(value) or value < 0 or value == 0 and not allow_zero):
            raise ParameterError(f"{label}: invalid value")
    try:
        if cap is not None:
            encoded["volume"] = encode(cap, 92)
            actual = volume(encoded["volume"])
            if not math.isclose(actual, cap, rel_tol=1e-12, abs_tol=1e-15):
                raise ParameterError(f"Volume {cap:g} mL cannot be represented exactly; nearest encodable value: {actual:g} mL. Edit explicitly. / 体积精度超限，请明确修改")
        if speed is not None:
            encoded["flow"] = encode(speed)
            encoded["refresh_flow"] = encode(speed/2)
        if cap is not None and speed is not None:
            seconds = cap/speed*60
            if not math.isfinite(seconds) or not min(TIME_UNITS.values()) <= seconds <= 65535*max(TIME_UNITS.values()):
                raise ParameterError("Dose duration cannot be represented by the pump / 注射时间超出泵协议可表示范围")
    except ValueError as exc:
        if isinstance(exc, ParameterError):
            raise
        raise ParameterError(f"Pump parameters / 泵参数: {exc}") from exc
    return encoded
