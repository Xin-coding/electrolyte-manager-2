"""Display-only dilute-water OH estimate. Never used by feedback."""
import json
import math
from importlib.resources import files

_TABLE = json.loads(files("em2.resources").joinpath("pkw.json").read_text(encoding="utf-8"))["pkw"]


def hydroxide(ph, temperature):
    if temperature is None or not math.isfinite(temperature) or not 0 <= temperature <= 99:
        return float("nan")
    index = min(98, int(temperature))
    pkw = _TABLE[index] + (temperature-index)*(_TABLE[index+1]-_TABLE[index])
    try:
        return 1000*10**(ph-pkw)
    except OverflowError:
        return float("nan")
