"""Volatile, single-use feedback reservation. No configuration persistence."""
import copy
import math
from datetime import datetime


def operational(settings):
    return {k: copy.deepcopy(v) for k, v in settings.items() if k not in {"version", "metadata"}}


def confirmation_context(settings, hardware, bindings, load):
    pump = bindings.get("pump", {})
    return {"settings": operational(settings), "signature": hardware.get("signature"),
            "pump": pump.get("identity") or pump.get("port"),
            "load_id": load["id"], "allowance": load["limit_ml"],
            "syringe_note": settings.get("metadata", {}).get("syringe_note", "")}


def duration(seconds):
    seconds = max(0, math.ceil(seconds))
    return f"{seconds//3600:02d}:{seconds//60%60:02d}:{seconds%60:02d}"


REASONS = {
    "cancelled": ("已手动取消", "Cancelled by operator"),
    "manual_pause": ("已暂停反馈", "Feedback paused"),
    "emergency_stop": ("已急停", "Emergency stop"),
    "finish": ("实验结束", "Experiment ended"),
    "enable": ("已选择立即启用反馈", "Immediate feedback requested"),
    "changed": ("反馈/采样参数、泵、注射器或装液额度已改变，请重新确认", "Settings, pump, syringe or load changed; confirm again"),
    "missed": ("错过启动时间超过 5 秒，未补启", "Start missed by more than 5 seconds; no retry"),
    "recording": ("实验已停止记录", "Experiment is no longer recording"),
    "busy": ("反馈已启用或泵忙碌", "Feedback enabled or pump busy"),
    "pump": ("泵未连接、未确认停止或存在故障", "Pump disconnected, not confirmed stopped or faulty"),
    "storage": ("数据存储异常", "Data storage unavailable"),
    "signal": ("控制传感器未连接或没有有效实时读数", "Control sensor disconnected or no valid live reading"),
    "stale": ("控制传感器读数已过期", "Control sensor reading expired"),
    "allowance": ("装液额度不足或未经确认", "Insufficient or unverified load allowance"),
    "settings": ("反馈设置无效", "Feedback settings invalid"),
    "pending": ("仍有设置或装液变更待处理", "Settings or allowance changes still pending"),
}


def status_text(value, zh=True):
    state = value.get("state", "idle")
    if state == "pending":
        expected = datetime.fromtimestamp(value["expected_at"]).strftime("%Y-%m-%d %H:%M:%S")
        return ((f"反馈启动倒计时 {duration(value['remaining_s'])} · 预计启用 {expected} · 仅记录，反馈未启用") if zh else
                f"Feedback starts in {duration(value['remaining_s'])} · Expected {expected} · Recording only")
    if state == "idle":
        return "倒计时未启用" if zh else "Countdown off"
    labels = {"started": ("反馈已按预约启用，等待新读数", "Scheduled feedback enabled; awaiting a new reading"),
              "failed": ("反馈未启动", "Feedback did not start"), "cancelled": ("倒计时已取消", "Countdown cancelled")}
    text = labels[state][0 if zh else 1]
    reason = value.get("reason", "")
    if reason:
        text += " · "+REASONS.get(reason, (reason, reason))[0 if zh else 1]
    return text
