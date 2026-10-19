"""Side-effect-free draft validation. Field paths are independent of Qt widgets."""
import math
import re

from .parameters import force_integer, pump_parameters

NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
REQUIRED = ("此项必填。", "This field is required.")
POSITIVE = ("必须大于 0。", "Must be greater than 0.")
FORMAT = ("请输入数字，使用英文小数点；不要输入单位、逗号或其他文字。",
          "Enter a number using a decimal point; omit units, commas and other text.")


class FormError(ValueError):
    """Already presented inline by the form; do not open another dialog."""


def parse_number(text):
    text = text.strip()
    if not text:
        return None, None
    if not NUMBER.fullmatch(text):
        return None, FORMAT
    value = float(text)
    if not math.isfinite(value):
        return None, ("数值过大，必须为有限数。", "Value is too large; enter a finite number.")
    if value == 0 and any(c in "123456789" for c in text.lower().split("e")[0]):
        return None, ("数值过小，无法表示；不会自动当作 0。", "Value is too small to represent; it will not be treated as zero.")
    return value, None


def parse_fields(texts, required=()):
    values, errors = {}, {}
    for key, text in texts.items():
        value, issue = parse_number(text)
        values[key] = value
        if issue or value is None and key in required:
            errors[key] = issue or REQUIRED
    return values, errors


def positive(values, errors, key, zero=False):
    value = values.get(key)
    if key not in errors and value is not None and (value < 0 or value == 0 and not zero):
        errors[key] = ("不能小于 0。", "Must not be negative.") if zero else POSITIVE


def pump_fields(values, errors, volume_key, flow_key, force_key, mix_key=None):
    for key in (volume_key, flow_key):
        positive(values, errors, key)
    if mix_key:
        positive(values, errors, mix_key, zero=True)
    force = values.get(force_key)
    if force is not None and force_key not in errors:
        try:
            values[force_key] = force_integer(force)
        except ValueError:
            errors[force_key] = ("推力必须为 20–100 的整数；50.0 可以，50.5 不可以。",
                                 "Force must be an integer from 20 to 100; 50.0 is valid, 50.5 is not.")
    # Use the actual protocol validation, including the firmware refresh flow.
    for key, argument in ((volume_key, "cap"), (flow_key, "speed")):
        if key not in errors and values.get(key) is not None:
            try:
                pump_parameters(**{argument: values[key]})
            except ValueError as exc:
                errors[key] = (f"泵无法使用此数值或精度：{exc}", f"Pump cannot represent this value or precision: {exc}")
    if not any(k in errors for k in (volume_key, flow_key)):
        try:
            pump_parameters(values.get(volume_key), values.get(flow_key))
        except ValueError:
            issue = ("体积与速率计算出的注射时间超出泵的可表示范围。",
                     "The volume / flow combination gives a duration outside the pump's supported range.")
            errors[volume_key] = errors[flow_key] = issue


def settings_fields(texts, control, complete=False, capacity=None):
    required = {"ph_interval_s"}
    if complete:
        required.update(("dose_ml", "flow_ml_min", "force_percent", "mix_s"))
        required.update(f"{control}.{k}" for k in ("tolerance", "lower", "upper"))
    values, errors = parse_fields(texts, required)
    pump_fields(values, errors, "dose_ml", "flow_ml_min", "force_percent", "mix_s")
    interval = values.get("ph_interval_s")
    if interval is not None and not .5 <= interval <= 3600:
        errors["ph_interval_s"] = ("采集间隔必须为 0.5–3600 秒。", "Sampling interval must be 0.5–3600 seconds.")
    if capacity is not None and values.get("dose_ml") is not None and values["dose_ml"] > capacity+1e-10:
        errors.setdefault("dose_ml", (f"单次量超过当前注射器容量 {capacity:g} mL。", f"Dose exceeds the current syringe capacity of {capacity:g} mL."))
    for kind in ("ph", "ec"):
        keys = {k: f"{kind}.{k}" for k in ("target", "tolerance", "lower", "upper")}
        positive(values, errors, keys["tolerance"])
        target, tol, low, high = (values.get(keys[k]) for k in keys)
        if low is not None and high is not None and low >= high:
            issue = ("下侧报警必须小于上侧报警。", "Lower alarm must be below upper alarm.")
            for k in ("lower", "upper"):
                errors.setdefault(keys[k], issue)
        if not any(k in errors for k in keys.values()) and None not in (target, tol, low, high):
            if not low < target-tol < target+tol < high:
                issue = ("目标 ± 容差必须严格位于固定报警范围内（不能碰到报警边界）。",
                         "Target ± tolerance must be strictly inside the fixed alarm range (not on a boundary).")
                for key in keys.values():
                    errors[key] = issue
    return values, errors


def test_fields(texts, stop_test=None, complete=False, capacity=None, remaining=None):
    # None checks the draft, including an optional STOP cap. False ignores that
    # field: an unfinished STOP test must not prevent a valid finite-dose test.
    texts = dict(texts)
    if stop_test is False:
        texts.pop("hardware_cap_ml", None)
    required = {"volume_ml", "flow_ml_min", "force_percent"} if complete else set()
    if complete and stop_test:
        required.add("hardware_cap_ml")
    values, errors = parse_fields(texts, required)
    pump_fields(values, errors, "volume_ml", "flow_ml_min", "force_percent")
    cap = values.get("hardware_cap_ml")
    dose = values.get("volume_ml")
    if stop_test is not False:
        positive(values, errors, "hardware_cap_ml")
        pump_fields(values, errors, "hardware_cap_ml", "flow_ml_min", "force_percent")
        if cap is not None and dose is not None and cap <= dose:
            issue = ("STOP 测试泵端上限必须大于软件停止量。", "STOP hardware cap must exceed the software cutoff.")
            errors.setdefault("hardware_cap_ml", issue)
    for key in ("volume_ml", "hardware_cap_ml"):
        value = values.get(key)
        if value is None:
            continue
        if capacity is not None and value > capacity+1e-10:
            errors.setdefault(key, (f"超过注射器容量 {capacity:g} mL。", f"Exceeds syringe capacity of {capacity:g} mL."))
        if remaining is not None and value > remaining+1e-10:
            errors.setdefault(key, (f"超过本次装液剩余额度 {remaining:g} mL。", f"Exceeds remaining load allowance of {remaining:g} mL."))
    return values, errors


def allowance_field(text):
    values, errors = parse_fields({"allowance": text}, {"allowance"})
    positive(values, errors, "allowance")
    return values["allowance"], errors.get("allowance")
