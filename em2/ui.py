from __future__ import annotations

from datetime import datetime
from collections import deque
import json
import math
from pathlib import Path
import time
import traceback

import numpy as np
import pyqtgraph as pg
import pyqtgraph.exporters
from PySide6 import QtCore as C, QtGui as G, QtWidgets as W

from . import storage, reporting, charts
from . import __version__
from .devices.sensors import ports
from .models import Settings, Dose
from . import forms
from .paths import atomic_json, read_json
from .units import hydroxide

pg.setConfigOptions(antialias=True, background="white", foreground="#17324d")

STYLE = """
QMainWindow, QDialog { background: #f4f7fb; color: #18324a; }
QWidget { font-family: 'Microsoft YaHei UI', 'Segoe UI'; font-size: 10pt; color: #18324a; }
QLabel#title { font-size: 21pt; font-weight: 700; }
QLabel#subtitle { color: #668098; }
QLabel#cardValue { font-size: 23pt; font-weight: 700; color: #0a5571; }
QLabel#status { padding: 10px; background: #e4eef5; border-radius: 6px; }
QFrame#card, QGroupBox { background: white; border: 1px solid #d9e3ed; border-radius: 8px; }
QGroupBox { margin-top: 13px; padding: 18px 12px 12px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 14px; padding: 0 4px; }
QPushButton { background: white; border: 1px solid #becfdf; border-radius: 5px; padding: 8px 14px; }
QPushButton:hover { border-color: #1685b2; background: #edf7fc; }
QPushButton:disabled { color: #9caebb; background: #edf1f5; }
QPushButton#primary { background: #076e9a; border-color: #076e9a; color: white; font-weight: 600; }
QPushButton#danger { background: #b83645; color: white; border-color: #b83645; font-weight: 700; }
QPushButton#primary:disabled, QPushButton#danger:disabled { background: #edf1f5; color: #9caebb; border-color: #c9d7e3; }
QLineEdit, QComboBox, QDoubleSpinBox, QSpinBox, QPlainTextEdit, QListWidget, QTableWidget {
 background: white; border: 1px solid #c9d7e3; border-radius: 4px; padding: 5px; }
QLineEdit:focus, QComboBox:focus { border-color: #087ba9; }
QLineEdit[invalid="true"], QLineEdit[invalid="true"]:focus { border: 2px solid #b83645; background: #fff5f5; }
QLabel#fieldError { color: #a82033; font-size: 9pt; }
QListWidget#navigation { background: #132f48; border: none; padding: 9px; color: #d9e7f2; }
QListWidget#navigation::item { color: #d9e7f2; padding: 16px 9px; margin: 3px 0; border-radius: 5px; }
QListWidget#navigation::item:selected { background: #17678d; color: white; }
QScrollArea { border: none; background: transparent; }
QCheckBox { spacing: 6px; }
QToolTip { background: #18324a; color: white; padding: 6px; border: none; }
"""

STATE_ZH = {
    "idle": "就绪", "recording": "正在记录", "preparing": "准备泵参数",
    "injecting": "正在注射", "confirming_stop": "确认停止与最终计量", "mixing": "等待充分混合",
    "awaiting_counter": "已停泵，等待最终计数", "accounting_review": "已停泵，最终计量需核实",
    "saving_result": "已停泵，等待注射结果保存",
    "waiting_pump": "等待泵连接或状态核实", "waiting_signal": "等待控制信号重连",
    "waiting_target": "等待初始信号稳定", "waiting_new_sample": "等待混合后的新读数",
    "target_outside_alarm": "目标容差区不在报警范围内", "monitoring": "目标容差内，继续监测",
    "allowance_exhausted": "余量不足完整一针", "manual_pause": "反馈已手动暂停",
    "emergency_stop": "急停已请求，需手动继续", "alarm_paused": "信号报警，暂停加液",
    "storage_paused": "存储失败，反馈已暂停", "pump_fault": "泵故障，需核实后继续",
    "parameter_error": "泵参数无效，修改后可继续", "configuration_error": "泵参数读回不一致，需核实",
    "communication_error": "泵通信异常，需核实后继续", "software_error": "软件执行错误，反馈已暂停",
    "accounting_error": "注射计量需核实", "pump_disconnected": "泵未连接",
    "pump_busy": "泵正被注射或计量占用", "feedback_reserved": "反馈占用泵",
    "hardware_changed": "注射器信息已变更，需重新确认", "ending": "正在结束实验",
    "manual_review": "计量已人工确认，需手动继续", "pump_unavailable": "泵暂不可用"}


class JobSignals(C.QObject):
    done = C.Signal(int, object, str)


class Job(C.QRunnable):
    def __init__(self, ident, function):
        super().__init__()
        self.ident, self.function = ident, function
        self.signals = JobSignals()

    def run(self):
        try:
            self.signals.done.emit(self.ident, self.function(), "")
        except Exception as exc:
            self.signals.done.emit(self.ident, None, str(exc))


def layout_row(*widgets):
    row = W.QHBoxLayout()
    for widget in widgets:
        row.addWidget(widget)
    return row


class NumericEdit(W.QLineEdit):
    """Keep drafts editable; report errors on blur or explicit submission."""
    def __init__(self):
        super().__init__()
        self.reviewed = False
        self.issue = None
        self.error_label = W.QLabel()
        self.error_label.setObjectName("fieldError")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        self.container = W.QWidget()
        column = W.QVBoxLayout(self.container)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(3)
        column.addWidget(self)
        column.addWidget(self.error_label)

    def show_issue(self, issue, language):
        self.issue = issue
        message = issue[0 if language == "zh" else 1] if issue else ""
        self.error_label.setText(message)
        self.error_label.setVisible(bool(issue))
        self.setAccessibleDescription(message)
        invalid = bool(issue)
        if self.property("invalid") != invalid:
            self.setProperty("invalid", invalid)
            self.style().unpolish(self)
            self.style().polish(self)
            self.update()


def line_number(placeholder="", width=None):
    edit = NumericEdit()
    edit.setPlaceholderText(placeholder)
    if width:
        edit.setMaximumWidth(width)
    return edit


def number(edit):
    value, issue = forms.parse_number(edit.text())
    if issue:
        raise ValueError(" / ".join(issue))
    return value


def fmt(value, digits=3):
    return "—" if value is None or not math.isfinite(value) else f"{value:.{digits}f}"


def dose_result_text(entry, zh):
    if not entry:
        return ""
    dose, result = entry["dose"], entry["result"]
    purpose = {"experiment": ("正式注射", "Experiment dose"), "test": ("定量测试", "Finite test"),
               "stop_test": ("软件 STOP 测试", "Software STOP test")}[dose["purpose"]][0 if zh else 1]
    kind = result.get("error_kind") or ("pump_fault" if result.get("error") else "")
    category = STATE_ZH.get(kind, kind) if zh else kind.replace("_", " ")
    start = ("已尝试发送" if result["started"] else "未发送") if zh else ("attempted" if result["started"] else "not sent")
    text = (f"{entry['time'][11:19]} · {purpose} · "
            f"{'设定' if zh else 'Requested'} {fmt(dose['volume_ml'])} mL · "
            f"{'记账' if zh else 'Accounted'} {fmt(result['amount_ml'])} mL\n"
            f"START: {start} · {'停止已确认' if zh else 'Stop confirmed'}: {result['stopped']} · Source: {result['source']}")
    if dose["purpose"] == "stop_test":
        text += f" · STOP test passed: {result['evidence'].get('stop_test_passed', False)}"
    if result.get("error"):
        text += f"\n{category}: {result['error']}"
    if result.get("warnings"):
        text += "\n"+("监测提示（不暂停反馈）：" if zh else "Monitoring notes (feedback continues): ")+"; ".join(result["warnings"])
    if "estimate" in result.get("source", ""):
        text += "\n"+("记账量为估算，非实测；计数不可信时按本针设定的泵端限量计入额度。" if zh else "Accounted volume is an estimate, not a measurement; an unusable counter is charged at the programmed finite cap.")
    return text


class Window(W.QMainWindow):
    def __init__(self, runtime):
        super().__init__()
        self.runtime = runtime
        self.root = runtime.root
        self.language = read_json(self.root/"data/ui.json", {}).get("language", "zh")
        self.localized = []
        self.callbacks = {}
        self.error_callbacks = {}
        self.jobs = {}
        self.next_job = 0
        self.pool = C.QThreadPool(self)
        self.pool.setMaxThreadCount(2)
        self.chart_busy = False
        self.last_chart = 0
        self.last_error = ""
        self.last_path = ""
        self.popup = None
        self.history_popup = None
        self.history_generation = 0
        self.bundle_queue = deque()
        self.bundle_active = None
        self.failed_exports = []
        self.export_seen = deque(maxlen=32)
        self.notice_text = ""
        self.closing = False
        self.inventory = []
        self.setWindowTitle("Electrolyte Manager "+__version__)
        self.resize(1420, 930)
        self.setMinimumSize(1100, 760)
        self.setStyleSheet(STYLE)
        base = W.QWidget()
        self.setCentralWidget(base)
        outer = W.QVBoxLayout(base)
        outer.setContentsMargins(20, 16, 20, 12)
        brand = W.QLabel("Electrolyte Manager")
        brand.setObjectName("title")
        subtitle = self.label(f"电解液实验工作台 · {__version__}", f"Laboratory workspace · {__version__}")
        subtitle.setObjectName("subtitle")
        brandcol = W.QVBoxLayout()
        brandcol.addWidget(brand)
        brandcol.addWidget(subtitle)
        header = W.QHBoxLayout()
        header.addLayout(brandcol)
        header.addStretch()
        self.language_box = W.QComboBox()
        self.language_box.addItems(["中文", "English"])
        self.language_box.setCurrentIndex(self.language == "en")
        self.language_box.currentIndexChanged.connect(self.switch_language)
        header.addWidget(self.language_box)
        self.estop = self.button("泵急停", "STOP PUMP", lambda: self.runtime.stop(), "danger")
        self.estop.setMinimumSize(145, 43)
        self.estop.setToolTip("立即请求软件 STOP；通信中断时仍须使用泵上的物理停止键。\nRequests priority software STOP. Use the physical stop if communication is unavailable.")
        header.addWidget(self.estop)
        outer.addLayout(header)
        body = W.QHBoxLayout()
        self.navigation = W.QListWidget()
        self.navigation.setObjectName("navigation")
        self.navigation.setFixedWidth(175)
        self.navigation.setFocusPolicy(C.Qt.FocusPolicy.NoFocus)
        self.pages = W.QStackedWidget()
        self.page_names = [("设备连接", "Connections"), ("泵测试", "Pump tests"), ("实验设置", "Settings"), ("实时监控", "Monitor"), ("数据管理", "Data manager")]
        for zh, en in self.page_names:
            self.navigation.addItem(zh if self.language == "zh" else en)
        for build in (self.build_connections, self.build_tests, self.build_settings, self.build_monitor, self.build_history):
            self.pages.addWidget(build())
        self.navigation.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.navigation.setCurrentRow(0)
        body.addWidget(self.navigation)
        body.addWidget(self.pages, 1)
        outer.addLayout(body, 1)
        self.footer = W.QLabel()
        self.footer.setWordWrap(True)
        self.footer.setObjectName("status")
        footer_row = layout_row(self.footer)
        self.retry_export_button = self.button("重试导出", "Retry export", self.retry_exports)
        self.retry_export_button.hide()
        footer_row.addWidget(self.retry_export_button)
        outer.addLayout(footer_row)
        self.apply_language()
        self.populate_settings(runtime.engine.settings.payload())
        self.refresh_ports()
        self.timer = C.QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(250)

    def label(self, zh, en, wordwrap=False):
        obj = W.QLabel()
        obj.setWordWrap(wordwrap)
        self.localized.append((obj, "setText", zh, en))
        obj.setText(zh if self.language == "zh" else en)
        return obj

    def button(self, zh, en, callback, role=""):
        obj = W.QPushButton()
        self.localized.append((obj, "setText", zh, en))
        obj.setText(zh if self.language == "zh" else en)
        obj.clicked.connect(lambda checked=False: self.guard(callback))
        obj.setObjectName(role)
        obj.destroyed.connect(lambda: self._forget_localized(obj))
        return obj

    def _forget_localized(self, obj):
        self.localized = [entry for entry in self.localized if entry[0] is not obj]

    def group(self, zh, en):
        obj = W.QGroupBox()
        self.localized.append((obj, "setTitle", zh, en))
        obj.setTitle(zh if self.language == "zh" else en)
        return obj

    def guard(self, callback):
        try:
            return callback()
        except forms.FormError:
            pass  # The invalid fields already contain actionable explanations.
        except Exception as exc:
            W.QMessageBox.warning(self, "Electrolyte Manager", str(exc))

    def submit(self, function, callback=lambda result: None, on_error=None):
        self.next_job += 1
        job = Job(self.next_job, function)
        self.jobs[self.next_job] = job
        self.callbacks[self.next_job] = callback
        self.error_callbacks[self.next_job] = on_error
        job.signals.done.connect(self.job_done)
        self.pool.start(job)

    @C.Slot(int, object, str)
    def job_done(self, ident, result, error):
        callback = self.callbacks.pop(ident, None)
        on_error = self.error_callbacks.pop(ident, None)
        self.jobs.pop(ident, None)
        if error:
            self.chart_busy = False
            self.notice_text = error
            if on_error:
                on_error(error)
            return
        if callback:
            self.guard(lambda: callback(result))

    def switch_language(self, index):
        self.language = "en" if index else "zh"
        atomic_json(self.root/"data/ui.json", {"language": self.language})
        self.apply_language()

    def apply_language(self):
        for obj, setter, zh, en in self.localized:
            getattr(obj, setter)(zh if self.language == "zh" else en)
        for i, (zh, en) in enumerate(self.page_names):
            self.navigation.item(i).setText(zh if self.language == "zh" else en)
        for edit in self.numeric_edits():
            edit.show_issue(edit.issue, self.language)

    def numeric_edits(self):
        return [*self.settings_edits().values(), *self.test_edits().values(), self.allowance]

    def settings_edits(self):
        return {**self.fields, **{f"{kind}.{key}": edit for kind, fields in self.signal_fields.items()
                                 for key, edit in fields.items() if key != "direction"},
                **({"metadata.current_a": self.meta_fields["current_a"]} if hasattr(self, "meta_fields") else {})}

    def test_edits(self):
        return {"volume_ml": self.test_volume, "flow_ml_min": self.test_flow,
                "force_percent": self.test_force, "hardware_cap_ml": self.test_cap}

    def watch_number(self, edit, validate):
        def finished():
            edit.reviewed = True
            validate()
        edit.editingFinished.connect(finished)

    def show_field_errors(self, edits, errors, all_fields=False, focus=False):
        for key, edit in edits.items():
            if all_fields:
                edit.reviewed = True
            edit.show_issue(errors.get(key) if edit.reviewed else None, self.language)
        if focus and errors:
            edit = edits[next(key for key in edits if key in errors)]
            edit.setFocus()
            # Settings lives in a scroll area; make the field and reason visible.
            if self.pages.currentIndex() == 2:
                self.pages.widget(2).ensureWidgetVisible(edit.container)

    def validate_settings_form(self, all_fields=False, complete=None):
        if complete is None:
            complete = self.runtime.snapshot.get("enabled", False)
        edits = self.settings_edits()
        values, errors = forms.settings_fields({k: e.text() for k, e in edits.items()}, self.control.currentData(), complete,
                                               self.runtime.snapshot.get("hardware", {}).get("capacity_ml"))
        self.show_field_errors(edits, errors, all_fields, focus=all_fields)
        self.draft_notice.setVisible(any(e.issue for e in edits.values()))
        return values, errors

    def validate_test_form(self, stop_test=None, submit=False):
        snap = self.runtime.snapshot
        values, errors = forms.test_fields({k: e.text() for k, e in self.test_edits().items()}, stop_test, submit,
                                          snap.get("hardware", {}).get("capacity_ml"), snap.get("remaining"))
        edits = self.test_edits()
        if stop_test is False:
            edits.pop("hardware_cap_ml")
        self.show_field_errors(edits, errors, submit, focus=submit)
        return values, errors

    def validate_allowance(self):
        value, issue = forms.allowance_field(self.allowance.text())
        self.allowance.show_issue(issue, self.language)
        return value, issue

    def build_connections(self):
        page = W.QWidget()
        box = W.QVBoxLayout(page)
        row = layout_row(self.button("自动识别并连接", "Detect & connect", lambda: self.runtime.command("scan"), "primary"),
                         self.button("刷新端口", "Refresh ports", self.refresh_ports))
        row.addStretch()
        self.profile = W.QLineEdit("Bench 1")
        self.profile.setMaximumWidth(160)
        row.addWidget(self.profile)
        row.addWidget(self.button("保存绑定", "Save bindings", self.save_bindings))
        row.addWidget(self.button("加载绑定", "Load bindings", self.load_bindings))
        box.addLayout(row)
        self.device_widgets = {}
        for kind, title in (("ph", "Metrohm 781 · pH"), ("ec", "Apera PC8500 · Conductivity"), ("pump", "Longer ASP · Syringe pump")):
            group = W.QGroupBox(title)
            form = W.QVBoxLayout(group)
            state = W.QLabel()
            state.setWordWrap(True)
            combo = W.QComboBox()
            combo.setMinimumWidth(310)
            connect = self.button("连接", "Connect", lambda k=kind: self.connect_selected(k))
            disconnect = self.button("断开", "Disconnect", lambda k=kind: self.runtime.command("disconnect", kind=k))
            form.addLayout(layout_row(combo, connect, disconnect))
            form.addWidget(state)
            identity = W.QLabel("—")
            identity.setTextInteractionFlags(C.Qt.TextInteractionFlag.TextSelectableByMouse)
            identity.setWordWrap(True)
            form.addWidget(identity)
            box.addWidget(group)
            self.device_widgets[kind] = (combo, state, identity, connect, disconnect)
        self.connection_note = self.label("电导率仪请在仪器上设为 1 min 自动上传。", "Set the conductivity instrument to 1 min automatic upload.")
        box.addWidget(self.connection_note)
        diag = W.QCheckBox("Diagnostic communication log · max 200 MiB")
        self.localized.append((diag, "setText", "记录通信诊断日志 · 上限 200 MiB", "Diagnostic communication log · max 200 MiB"))
        diag.toggled.connect(lambda value: self.runtime.command("diagnostics", enabled=value))
        box.addWidget(diag)
        box.addStretch()
        return page

    def refresh_ports(self):
        self.submit(ports, self.set_ports)

    def set_ports(self, inventory):
        self.inventory = inventory
        for combo, *_ in self.device_widgets.values():
            selection = combo.currentData()
            combo.clear()
            for p in inventory:
                combo.addItem(f"{p['port']}  ·  {p['description']}", p)
            if selection:
                for i in range(combo.count()):
                    if combo.itemData(i)["port"] == selection["port"]:
                        combo.setCurrentIndex(i)

    def connect_selected(self, kind):
        p = self.device_widgets[kind][0].currentData()
        if not p:
            raise ValueError("Select a COM port")
        self.runtime.command("connect", kind=kind, port=p["port"], identity=p["identity"])

    def save_bindings(self):
        name = self.profile.text().strip()
        if not name or any(c in name for c in '/\\:*?"<>|'):
            raise ValueError("Enter a valid profile name")
        path = self.root/"data/profiles"/(name+".json")
        atomic_json(path, {"format": "em2-bindings-1", "bindings": self.runtime.snapshot.get("bindings", {})})
        self.notice_text = str(path)

    def load_bindings(self):
        path, _ = W.QFileDialog.getOpenFileName(self, "Device bindings", str(self.root/"data/profiles"), "JSON (*.json)")
        if not path:
            return
        data = read_json(path, {})
        if data.get("format") != "em2-bindings-1":
            raise ValueError("Unsupported binding profile")
        for binding in data["bindings"].values():
            self.runtime.command("connect", **binding)

    def build_tests(self):
        page = W.QWidget()
        box = W.QVBoxLayout(page)
        group = self.group("独立泵测试", "Independent pump tests")
        form = W.QFormLayout(group)
        self.test_volume = line_number("mL")
        self.test_flow = line_number("mL/min")
        self.test_cap = line_number("mL")
        self.test_force = line_number("20–100 %")
        for zh, en, field in (("定量体积 / 软件停止量 (mL)", "Dose / software cutoff (mL)", self.test_volume),
                              ("注射速率 (mL/min)", "Flow (mL/min)", self.test_flow),
                              ("STOP 测试泵端上限 (mL)", "STOP test hardware cap (mL)", self.test_cap),
                              ("测试推力 (%)", "Test force (%)", self.test_force)):
            form.addRow(self.label(zh, en), field.container)
            self.watch_number(field, self.validate_test_form)
        self.test_cap.setToolTip("STOP 测试：泵端限量必须大于软件停止量。\nThe finite hardware cap must exceed the software cutoff.")
        self.finite_test_button = self.button("单次定量测试", "Run finite dose", lambda: self.start_test(False), "primary")
        self.stop_test_button = self.button("软件 STOP 测试", "Run software STOP test", lambda: self.start_test(True))
        row = layout_row(self.finite_test_button, self.stop_test_button)
        form.addRow(row)
        box.addWidget(group)
        self.test_state = W.QLabel()
        self.test_state.setObjectName("status")
        self.test_state.setWordWrap(True)
        box.addWidget(self.test_state)
        self.hardware_text = W.QPlainTextEdit()
        self.hardware_text.setReadOnly(True)
        self.hardware_text.setMaximumHeight(170)
        box.addWidget(self.hardware_text)
        self.test_results = W.QPlainTextEdit()
        self.test_results.setReadOnly(True)
        box.addWidget(self.test_results, 1)
        row = layout_row(self.button("查看泵状态 / 加液曲线", "Pump status / volume plot", self.show_pump),
                         self.button("核实中断针的实际量", "Resolve interrupted dose", self.resolve_dose))
        box.addLayout(row)
        return page

    def confirm_hardware(self, details):
        snap = self.runtime.snapshot
        h = snap.get("hardware", {})
        if not h:
            raise ValueError("Connect the pump first")
        signature = h.get("signature")
        if signature == snap.get("confirmed_signature"):
            return signature
        zh = self.language == "zh"
        cfg = details.get("settings", details)
        text = ((f"注射器容量：{h.get('capacity_ml')} mL\n有效面积：{h.get('area_mm2')} mm²\n仪器推力：{h.get('force_percent')} %\n型号与校准历史：仪器未提供\n\n"
                 if zh else f"Capacity: {h.get('capacity_ml')} mL\nEffective area: {h.get('area_mm2')} mm²\nInstrument force: {h.get('force_percent')} %\nModel and calibration history: not provided\n\n")+
                (f"本次设置：\n体积：{cfg.get('dose_ml',cfg.get('volume_ml'))} mL\n速率：{cfg.get('flow_ml_min')} mL/min\n推力：{cfg.get('force_percent')} %\n混合时间：{cfg.get('mix_s',0)} s"
                 if zh else f"Effective settings:\nVolume: {cfg.get('dose_ml',cfg.get('volume_ml'))} mL\nFlow: {cfg.get('flow_ml_min')} mL/min\nForce: {cfg.get('force_percent')} %\nMixing: {cfg.get('mix_s',0)} s"))
        if details.get("hardware_cap_ml"):
            text += f"\nHardware cap: {details['hardware_cap_ml']} mL"
        if cfg.get("metadata",{}).get("syringe_note"):
            text += "\n\n"+cfg["metadata"]["syringe_note"]
        title = "确认注射器与本次设置" if self.language == "zh" else "Confirm syringe and effective settings"
        answer = W.QMessageBox.question(self, title, text, W.QMessageBox.StandardButton.Ok | W.QMessageBox.StandardButton.Cancel)
        return signature if answer == W.QMessageBox.StandardButton.Ok else None

    def test_data(self, stop_test, submit=False):
        snap = self.runtime.snapshot
        values, errors = forms.test_fields({k: e.text() for k, e in self.test_edits().items()}, stop_test, True,
                                          snap.get("hardware", {}).get("capacity_ml"), snap.get("remaining"))
        if submit:
            self.validate_test_form(stop_test, submit=True)
        if errors:
            issue = next(iter(errors.values()))
            raise forms.FormError(issue[0 if self.language == "zh" else 1])
        data = {"volume_ml": values["volume_ml"], "flow_ml_min": values["flow_ml_min"], "force_percent": values["force_percent"],
                "purpose": "stop_test" if stop_test else "test", "hardware_cap_ml": values.get("hardware_cap_ml")}
        validated = Dose(**data)
        data["force_percent"] = validated.force_percent
        snap = self.runtime.snapshot
        reason = snap.get("test_block_reason", "pump_disconnected")
        if reason:
            raise ValueError(STATE_ZH.get(reason, reason) if self.language == "zh" else reason.replace("_", " "))
        if validated.cap > snap["hardware"]["capacity_ml"]+1e-10:
            raise ValueError("Dose exceeds syringe capacity / 剂量超过注射器容量")
        if snap["remaining"] is not None and validated.cap > snap["remaining"]+1e-10:
            raise ValueError("Insufficient load allowance / 可用额度不足")
        return data

    def start_test(self, stop_test):
        data = self.test_data(stop_test, submit=True)
        signature = self.confirm_hardware(data)
        if signature:
            self.runtime.command("test", signature=signature, **data)

    def resolve_dose(self):
        value, ok = W.QInputDialog.getDouble(self, "Resolve delivered volume", "Verified actual delivered volume (mL)", 0, 0, 100000, 6)
        if ok:
            self.runtime.command("resolve", amount=value)

    def build_settings(self):
        scroll = W.QScrollArea()
        scroll.setWidgetResizable(True)
        page = W.QWidget()
        scroll.setWidget(page)
        box = W.QVBoxLayout(page)
        self.settings_status = W.QLabel()
        self.settings_status.setObjectName("status")
        self.settings_status.setWordWrap(True)
        box.addWidget(self.settings_status)
        self.effective_settings = W.QLabel()
        self.effective_settings.setWordWrap(True)
        self.effective_settings.setTextInteractionFlags(C.Qt.TextInteractionFlag.TextSelectableByMouse)
        box.addWidget(self.effective_settings)
        bar = layout_row(self.button("应用设置", "Apply settings", self.apply_settings, "primary"),
                         self.button("保存配置", "Save preset", self.save_settings),
                         self.button("加载配置", "Load preset", self.load_settings),
                         self.button("读取当前有效设置", "Read effective settings", lambda: self.populate_settings(self.runtime.snapshot["settings"])))
        box.addLayout(bar)
        self.draft_notice = self.label("草稿有错误：请修改红色字段。尚未提交；当前有效设置和已排队的设置继续保持。",
                                       "Invalid draft: correct the red fields. Nothing was submitted; effective and pending settings are unchanged.", True)
        self.draft_notice.setObjectName("fieldError")
        self.draft_notice.hide()
        box.addWidget(self.draft_notice)
        self.feedback_notice = self.label("当前有效配置尚不完整或无效。请应用完整配置后再启用反馈；数据记录可继续。",
                                          "Effective settings are incomplete or invalid. Apply a complete configuration before enabling feedback; recording can continue.", True)
        self.feedback_notice.setObjectName("fieldError")
        self.feedback_notice.hide()
        box.addWidget(self.feedback_notice)
        self.fields = {}
        group = self.group("正式实验 · 泵设置", "Experiment · Pump settings")
        form = W.QFormLayout(group)
        for key, zh, en in (("dose_ml", "单次注射量 (mL)", "Dose (mL)"), ("flow_ml_min", "注射速率 (mL/min)", "Flow (mL/min)"),
                            ("force_percent", "推力 (%)", "Force (%)"), ("mix_s", "完整混合等待 (s)", "Full mixing wait (s)")):
            field = line_number()
            self.fields[key] = field
            form.addRow(self.label(zh, en), field.container)
            self.watch_number(field, self.validate_settings_form)
        self.allowance = line_number("mL")
        self.watch_number(self.allowance, self.validate_allowance)
        allowance_row = layout_row(self.allowance.container, self.button("更改额度", "Change allowance", lambda: self.set_allowance(False)),
                                  self.button("已换液，重置额度", "Refilled · reset allowance", lambda: self.set_allowance(True)))
        form.addRow(self.label("本次装液可用额度 (mL)", "Current load allowance (mL)"), allowance_row)
        self.load_status = W.QLabel()
        form.addRow(self.load_status)
        box.addWidget(group)
        group = self.group("反馈与采集", "Feedback & acquisition")
        form = W.QFormLayout(group)
        self.control = W.QComboBox()
        self.control.addItem("pH", "ph")
        self.control.addItem("Conductivity", "ec")
        form.addRow(self.label("控制信号", "Control signal"), self.control)
        self.fields["ph_interval_s"] = line_number()
        form.addRow(self.label("pH 采集间隔 (s)", "pH sampling interval (s)"), self.fields["ph_interval_s"].container)
        self.watch_number(self.fields["ph_interval_s"], self.validate_settings_form)
        form.addRow(self.label("电导率采样间隔", "Conductivity sampling interval"), W.QLabel("1 min · instrument setting"))
        self.smoothing = W.QCheckBox("EMA · α = 0.5")
        form.addRow(self.label("平滑显示与正常反馈", "Smooth display and normal feedback"), self.smoothing)
        box.addWidget(group)
        self.signal_fields = {}
        pair = W.QHBoxLayout()
        for kind, title in (("ph", "pH"), ("ec", "Conductivity · mS/cm")):
            group = W.QGroupBox(title)
            form = W.QFormLayout(group)
            fields = {}
            for key, zh, en in (("target", "目标（空白 = 初始稳定值）", "Target (blank = stable initial)"),
                                ("tolerance", "目标容差 ±", "Target tolerance ±"),
                                ("lower", "固定下侧报警", "Fixed lower alarm"), ("upper", "固定上侧报警", "Fixed upper alarm")):
                field = line_number()
                fields[key] = field
                form.addRow(self.label(zh, en), field.container)
                self.watch_number(field, self.validate_settings_form)
            direction = W.QComboBox()
            direction.addItem("Low → inject / 低于目标加液", "raise")
            direction.addItem("High → inject / 高于目标加液", "lower")
            fields["direction"] = direction
            form.addRow(self.label("加液方向", "Dosing direction"), direction)
            self.signal_fields[kind] = fields
            pair.addWidget(group)
        box.addLayout(pair)
        group = self.group("实验信息 · 仅记录，可留空", "Experiment notes · optional, record only")
        form = W.QFormLayout(group)
        self.meta_fields = {}
        metadata = [("initial_volume_ml", "初始电解液体积 (mL)", "Initial electrolyte volume (mL)"),
                    ("electrolyte", "电解液组分", "Electrolyte composition"), ("counter_electrolyte", "对电极电解液", "Counter electrolyte"),
                    ("reaction", "电解反应", "Reaction"), ("current_a", "电流 (A)", "Current (A)"),
                    ("current_density_ma_cm2", "电流密度 (mA/cm²)", "Current density (mA/cm²)"),
                    ("electrode", "电极种类", "Electrode"), ("area_cm2", "电极面积 (cm²)", "Electrode area (cm²)"),
                    ("cell_type", "电解池类型", "Cell type"), ("counter_reaction", "对电极反应", "Counter reaction"),
                    ("counter_electrode", "对电极种类", "Counter electrode"),
                    ("syringe_note", "注射器 / 校准人工备注", "Syringe / calibration note"), ("notes", "其他备注", "Other notes")]
        for key, zh, en in metadata:
            field = line_number() if key == "current_a" else W.QLineEdit()
            self.meta_fields[key] = field
            form.addRow(self.label(zh, en), field.container if key == "current_a" else field)
            if key == "current_a":
                self.watch_number(field, self.validate_settings_form)
        box.addWidget(group)
        return scroll

    def collect_settings(self, complete=None):
        values, errors = self.validate_settings_form(all_fields=True, complete=complete)
        if errors:
            raise forms.FormError("Invalid settings draft")
        result = dict(self.runtime.snapshot.get("settings", Settings().payload()))
        for key, edit in self.fields.items():
            result[key] = values[key]
        result["control"] = self.control.currentData()
        result["smoothing"] = self.smoothing.isChecked()
        for kind, fields in self.signal_fields.items():
            result[kind] = {k: (v.currentData() if k == "direction" else values[f"{kind}.{k}"]) for k, v in fields.items()}
        result["metadata"] = {k: v.text().strip() for k, v in self.meta_fields.items()}
        return Settings.from_dict(result).payload()

    def populate_settings(self, settings):
        for key, field in self.fields.items():
            value = settings.get(key)
            field.setText("" if value is None else str(value))
        self.control.setCurrentIndex(self.control.findData(settings["control"]))
        self.smoothing.setChecked(settings["smoothing"])
        for kind, fields in self.signal_fields.items():
            for key, field in fields.items():
                value = settings[kind].get(key)
                if key == "direction":
                    field.setCurrentIndex(field.findData(value))
                else:
                    field.setText("" if value is None else str(value))
        for key, field in self.meta_fields.items():
            field.setText(str(settings.get("metadata", {}).get(key, "")))
        for edit in self.settings_edits().values():
            edit.reviewed = False
            edit.show_issue(None, self.language)
        self.draft_notice.hide()

    def apply_settings(self):
        self.runtime.command("apply", settings=self.collect_settings())
        self.feedback_notice.hide()

    def save_settings(self):
        settings = self.collect_settings(complete=False)
        path, _ = W.QFileDialog.getSaveFileName(self, "Save settings", str(self.root/"data/profiles"/"Experiment.json"), "JSON (*.json)")
        if path:
            atomic_json(path, {"format": "em2-settings-1", "settings": settings})
            self.notice_text = path

    def load_settings(self):
        path, _ = W.QFileDialog.getOpenFileName(self, "Load settings", str(self.root/"data/profiles"), "JSON (*.json)")
        if path:
            data = read_json(path, {})
            if data.get("format") != "em2-settings-1":
                raise ValueError("Unsupported settings profile")
            self.populate_settings(Settings.from_dict(data["settings"]).payload())
            self.notice_text = "配置已加载到表单，点击应用后生效 / Preset loaded; click Apply to activate."

    def set_allowance(self, refill):
        value, issue = self.validate_allowance()
        if issue:
            self.allowance.setFocus()
            raise forms.FormError("Invalid allowance")
        self.runtime.command("allowance", value=value, refill=refill)

    def make_plot(self, title, ylabel):
        plot = pg.PlotWidget(title=title)
        plot.setLabel("left", ylabel)
        charts.configure(plot)
        plot.showGrid(x=True, y=True, alpha=.13)
        plot.setMinimumHeight(210)
        plot.addLegend(offset=(10, 8))
        return plot

    def build_monitor(self):
        page = W.QWidget()
        box = W.QVBoxLayout(page)
        row = W.QHBoxLayout()
        self.experiment_name = W.QLineEdit("Experiment")
        self.experiment_name.setMinimumWidth(150)
        row.addWidget(self.experiment_name, 1)
        self.start_button = self.button("开始记录", "Start recording", lambda: self.runtime.command("start_recording", name=self.experiment_name.text()), "primary")
        self.enable_button = self.button("启用 / 继续反馈", "Enable / resume", self.enable_control)
        self.pause_button = self.button("暂停反馈", "Pause feedback", lambda: self.runtime.stop("manual_pause"))
        self.finish_button = self.button("结束实验", "End experiment", lambda: self.runtime.command("finish"))
        for b in (self.start_button, self.enable_button, self.pause_button, self.finish_button):
            row.addWidget(b)
        box.addLayout(row)
        self.filename = W.QLabel("—")
        self.filename.setTextInteractionFlags(C.Qt.TextInteractionFlag.TextSelectableByMouse)
        box.addWidget(self.filename)
        cards = W.QHBoxLayout()
        self.cards = {}
        for key, zh, en, unit in (("ph", "pH", "pH", "pH"), ("ec", "电导率", "Conductivity", "mS/cm"),
                                  ("temp", "温度", "Temperature", "°C"), ("used", "本次装液已用", "Load delivered", "mL"),
                                  ("remaining", "可用额度剩余", "Allowance left", "mL"), ("charge", "累计电荷（估算）", "Estimated charge", "")):
            card = W.QFrame()
            card.setObjectName("card")
            v = W.QVBoxLayout(card)
            v.addWidget(self.label(zh, en))
            value = W.QLabel("—")
            value.setObjectName("cardValue")
            if key == "charge":
                value.setStyleSheet("font-size: 16pt;")
            v.addWidget(value)
            unit_label = W.QLabel(unit)
            v.addWidget(unit_label)
            if key == "charge":
                self.charge_unit = unit_label
            cards.addWidget(card, 1)
            self.cards[key] = value
        box.addLayout(cards)
        self.monitor_status = W.QLabel()
        self.monitor_status.setObjectName("status")
        self.monitor_status.setWordWrap(True)
        box.addWidget(self.monitor_status)
        self.dose_result_label = W.QLabel()
        self.dose_result_label.setWordWrap(True)
        self.dose_result_label.setTextInteractionFlags(C.Qt.TextInteractionFlag.TextSelectableByMouse)
        box.addWidget(self.dose_result_label)
        self.reading_note = W.QLabel()
        self.reading_note.setWordWrap(True)
        box.addWidget(self.reading_note)
        self.ph_axis = W.QComboBox()
        self.ph_axis.addItems(["pH", "Estimated OH⁻ (mmol/L)"])
        self.raw_check = W.QCheckBox("Raw")
        self.processed_check = W.QCheckBox("Processed")
        self.processed_check.setChecked(True)
        self.window_box = W.QComboBox()
        self.window_box.addItem("Last 60 min", 3600)
        self.window_box.addItem("Last 6 h", 21600)
        self.window_box.addItem("Entire experiment", None)
        tools = layout_row(self.ph_axis, self.raw_check, self.processed_check, self.window_box,
                           self.button("泵状态 / 加液曲线", "Pump status / volume", self.show_pump),
                           self.button("导出图表", "Export plots", lambda: self.export_plots([self.ph_plot, self.ec_plot])))
        box.addLayout(tools)
        plots = W.QHBoxLayout()
        self.ph_plot = self.make_plot("pH feedback", "pH")
        self.ec_plot = self.make_plot("Conductivity", "Conductivity (mS/cm)")
        self.ec_plot.setXLink(self.ph_plot)
        plots.addWidget(self.ph_plot)
        plots.addWidget(self.ec_plot)
        box.addLayout(plots, 1)
        self.live_curves = {}
        for kind, plot in (("ph", self.ph_plot), ("ec", self.ec_plot)):
            self.live_curves[kind] = {"raw": plot.plot(pen=pg.mkPen("#a9b9c8", width=1), name="Raw"),
                                      "filtered": plot.plot(pen=pg.mkPen("#177db5" if kind == "ph" else "#0e9e93", width=2), name="Processed")}
        self.target_lines = []
        for plot in (self.ph_plot, self.ec_plot):
            lines = [pg.InfiniteLine(angle=0, pen=pg.mkPen(color, style=C.Qt.PenStyle.DashLine)) for color in ("#dd9a25", "#dd9a25", "#d9636a", "#d9636a")]
            for line in lines:
                plot.addItem(line, ignoreBounds=True)
                line.hide()
            self.target_lines.append(lines)

        return page

    def enable_control(self):
        settings = self.runtime.snapshot["settings"]
        try:
            Settings.from_dict(settings).validate(complete=True)
        except ValueError:
            self.navigation.setCurrentRow(2)
            self.validate_settings_form(all_fields=True, complete=True)
            self.feedback_notice.show()
            raise forms.FormError("Apply complete settings before enabling feedback")
        sig = self.confirm_hardware({"purpose": "experiment", "settings": settings})
        if sig:
            self.runtime.command("enable", signature=sig)

    def build_history(self):
        page = W.QWidget()
        box = W.QVBoxLayout(page)
        row = layout_row(self.button("Refresh", "Refresh", self.refresh_history), self.button("Open files", "Open files", self.open_history),
                         self.button("Import package", "Import package", self.import_history),
                         self.button("Pump injection", "Pump injection", self.show_history_pump))
        row.addStretch()
        self.align = W.QComboBox()
        self.align.addItems(["Elapsed time", "Absolute time"])
        self.history_field = W.QComboBox()
        self.history_field.addItems(["filtered", "raw"])
        row.addWidget(self.history_field)
        row.addWidget(self.align)
        row.addWidget(self.button("Compare selected", "Compare selected", self.compare_history, "primary"))
        box.addLayout(row)
        self.history_list = W.QListWidget()
        self.history_list.setMaximumHeight(120)
        box.addWidget(self.history_list)
        row = W.QHBoxLayout()
        self.range_start = line_number("Start min (optional)", width=140)
        self.range_end = line_number("End min (optional)", width=140)
        row.addWidget(self.range_start.container)
        row.addWidget(self.range_end.container)
        for edit in (self.range_start, self.range_end):
            self.watch_number(edit, self.history_range)
        row.addWidget(self.button("Export CSV / JSON", "Export CSV / JSON", lambda: self.export_history(False)))
        row.addWidget(self.button("Export packages", "Export packages", lambda: self.export_history(True)))
        row.addWidget(self.button("Export full report", "Export full report", self.export_full_reports))
        row.addWidget(self.button("Export PNG / SVG", "Export PNG / SVG", lambda: self.export_plots(self.history_plots)))
        box.addLayout(row)
        plots = W.QHBoxLayout()
        self.history_plots = [self.make_plot("pH comparison", "pH"), self.make_plot("Conductivity comparison", "Conductivity (mS/cm)")]
        self.history_plots[1].setXLink(self.history_plots[0])
        for plot in self.history_plots:
            plots.addWidget(plot)
        box.addLayout(plots, 1)
        self.history_detail = W.QPlainTextEdit()
        self.history_detail.setReadOnly(True)
        self.history_detail.setMaximumHeight(140)
        box.addWidget(self.history_detail)
        return page

    def add_history(self, paths):
        present = {self.history_list.item(i).data(C.Qt.ItemDataRole.UserRole) for i in range(self.history_list.count())}
        for path in paths:
            path = str(path)
            if path not in present:
                item = W.QListWidgetItem(Path(path).stem)
                item.setData(C.Qt.ItemDataRole.UserRole, path)
                item.setToolTip(path)
                item.setFlags(item.flags() | C.Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(C.Qt.CheckState.Unchecked)
                self.history_list.addItem(item)

    def refresh_history(self):
        self.add_history(sorted((self.root/"data").glob("*.sqlite"), reverse=True))

    def open_history(self):
        paths, _ = W.QFileDialog.getOpenFileNames(self, "EM2 experiment files", str(self.root/"data"), "EM2 SQLite (*.sqlite)")
        self.add_history(paths)

    def selected_history(self):
        paths = [self.history_list.item(i).data(C.Qt.ItemDataRole.UserRole) for i in range(self.history_list.count()) if self.history_list.item(i).checkState() == C.Qt.CheckState.Checked]
        if not paths:
            raise ValueError("Check one or more experiment files")
        if len(paths) > 12:
            raise ValueError("Compare up to 12 experiments at a time")
        return paths

    def compare_history(self):
        start, end = self.history_range(submit=True)
        paths = self.selected_history()
        field = self.history_field.currentText()
        absolute = self.align.currentIndex() == 1
        def read():
            return [(storage.summary(path), [storage.series(path, k, field, start*60, end*60 if end is not None else None) for k in ("ph", "ec")], storage.recent_events(path, 20)) for path in paths]
        self.submit(read, lambda data: self.draw_history(data, absolute))
        if self.history_popup:
            self.load_history_pump(paths, start, end, absolute)

    def history_range(self, submit=False):
        edits = {"start": self.range_start, "end": self.range_end}
        values, errors = forms.parse_fields({k: e.text() for k, e in edits.items()})
        for key in edits:
            forms.positive(values, errors, key, zero=True)
        start, end = values["start"] or 0., values["end"]
        if not errors and end is not None and end <= start:
            errors["end"] = ("结束时间必须大于开始时间。", "End time must be greater than start time.")
        for key, edit in edits.items():
            if submit:
                edit.reviewed = True
            edit.show_issue(errors.get(key) if edit.reviewed else None, "en")
        if submit and errors:
            edits[next(iter(errors))].setFocus()
            raise forms.FormError("Invalid history time range")
        return start, end

    def draw_history(self, data, absolute):
        for plot in self.history_plots:
            plot.clear()
            plot.setAxisItems({"bottom": pg.DateAxisItem() if absolute else pg.AxisItem("bottom")})
            plot.setLabel("bottom", "Time (local)" if absolute else "Elapsed time (min)")
            plot.getAxis("bottom").enableAutoSIPrefix(False)
        colors = ["#167cb5", "#13a192", "#a85eb3", "#da8438", "#cc506d", "#6c8d34"]
        details = []
        for i, (meta, series, events) in enumerate(data):
            offset = datetime.fromisoformat(meta["started"]).timestamp() if absolute else 0
            for plot, rows in zip(self.history_plots, series):
                if rows:
                    x = [r[0]+offset if absolute else r[0]/60 for r in rows]
                    plot.plot(x, [r[1] for r in rows], pen=pg.mkPen(colors[i%len(colors)], width=2), name=Path(meta["path"]).stem)
            details.append(json.dumps({"file": meta["path"], "metadata": meta, "recent_events": events}, ensure_ascii=False, indent=2))
        self.history_detail.setPlainText("\n\n".join(details))

    def import_history(self):
        path, _ = W.QFileDialog.getOpenFileName(self, "Import EM2 package", str(self.root/"exports"), "EM2 package (*.em2zip)")
        if path:
            self.submit(lambda: storage.import_package(path, self.root/"data"), lambda p: self.add_history([p]))

    def export_history(self, package):
        paths = self.selected_history()
        destination = self.root/"exports"/datetime.now().strftime("Export_%Y-%m-%d_%H-%M-%S")
        destination.mkdir(exist_ok=True)
        def export():
            for path in paths:
                if package:
                    storage.export_package(path, destination/(Path(path).stem+".em2zip"))
                else:
                    storage.export_data(path, destination/Path(path).stem)
            return str(destination)
        self.submit(export, self.export_finished)

    def export_finished(self, path):
        self.notice_text = "Exported: "+path
        W.QMessageBox.information(self, "Export complete", path)

    def export_plots(self, plots):
        path = self.root/"exports"/datetime.now().strftime("Plots_%Y-%m-%d_%H-%M-%S")
        path.mkdir(exist_ok=True)
        for i, plot in enumerate(plots, 1):
            exporter = pg.exporters.ImageExporter(plot.plotItem)
            exporter.parameters()["width"] = 1800
            exporter.export(str(path/f"plot_{i}.png"))
            pg.exporters.SVGExporter(plot.plotItem).export(str(path/f"plot_{i}.svg"))
        self.export_finished(str(path))

    def show_history_pump(self):
        paths = self.selected_history()
        start, end = self.history_range(submit=True)
        if not self.history_popup:
            dialog = W.QDialog(self)
            dialog.setWindowTitle("Historical pump injection comparison")
            dialog.resize(1100, 720)
            dialog.setAttribute(C.Qt.WidgetAttribute.WA_DeleteOnClose)
            box = W.QVBoxLayout(dialog)
            self.history_pump_note = W.QLabel()
            self.history_pump_note.setWordWrap(True)
            box.addWidget(self.history_pump_note)
            self.history_pump_plot = self.make_plot("Pump injection comparison", "Cumulative injection (mL)")
            box.addWidget(self.history_pump_plot, 1)
            box.addLayout(layout_row(
                self.button("Export PNG / SVG", "Export PNG / SVG", lambda: self.export_plots([self.history_pump_plot])),
                self.button("Export pump CSV", "Export pump CSV", self.export_history_pump_csv)))
            dialog.destroyed.connect(self.history_pump_closed)
            self.history_popup = dialog
            dialog.show()
        self.history_popup.raise_()
        self.load_history_pump(paths, start, end, self.align.currentIndex() == 1)

    def history_pump_closed(self):
        self.history_popup = None
        self.history_generation += 1

    def load_history_pump(self, paths, start, end, absolute):
        self.history_generation += 1
        generation = self.history_generation
        self.history_pump_paths = list(paths)
        self.history_pump_note.setText("Loading historical records…")
        def draw(data):
            if not self.history_popup or generation != self.history_generation:
                return
            plot = self.history_pump_plot
            plot.clear()
            plot.setAxisItems({"bottom": pg.DateAxisItem() if absolute else pg.AxisItem("bottom")})
            plot.setLabel("bottom", "Time (local)" if absolute else "Elapsed time (min)")
            plot.getAxis("bottom").enableAutoSIPrefix(False)
            notes = []
            for i, entry in enumerate(data):
                name = Path(entry["meta"]["path"]).stem
                color = pg.intColor(i, hues=max(6, len(data)), values=1, maxValue=170)
                charts.draw_pump(plot, entry, color, name, absolute)
                notes.append(f"{name}: {entry['count']} injections, {entry['estimates']} estimated, {entry['unknown']} unknown")
            self.history_pump_note.setText("\n".join(notes))
            plot.enableAutoRange()
        self.submit(lambda: [reporting.pump_series(p, start*60, end*60 if end is not None else None) for p in paths], draw)

    def export_history_pump_csv(self):
        paths = list(self.history_pump_paths)
        destination = self.root/"exports"/datetime.now().strftime("Export_%Y-%m-%d_%H-%M-%S")
        def export():
            for path in paths:
                folder, meta = reporting.output_directory(path, destination)
                reporting.export_pump_csv(path, folder/f"{reporting.safe_name(meta.get('name', Path(path).stem))}_pump injection.csv")
            return str(destination)
        self.submit(export, self.export_finished)

    def export_full_reports(self):
        for path in self.selected_history():
            self.queue_bundle(path)

    def queue_bundle(self, path, pump_only=False):
        request = (str(path), pump_only)
        if request == self.bundle_active or request in self.bundle_queue:
            return
        self.bundle_queue.append(request)
        self.next_bundle()

    def next_bundle(self):
        if self.bundle_active or not self.bundle_queue:
            return
        self.bundle_active = self.bundle_queue.popleft()
        path, pump_only = self.bundle_active
        self.notice_text = "正在导出 / Exporting: "+Path(path).name
        self.submit(lambda: reporting.prepare_bundle(path, self.root/"exports", pump_only),
                    self.render_bundle, self.bundle_failed)

    def render_bundle(self, bundle):
        pending = deque(bundle["charts"])
        def render_next():
            try:
                if pending:
                    charts.render_report(pending.popleft())
                    C.QTimer.singleShot(0, render_next)
                    return
                request = self.bundle_active
                if request in self.failed_exports:
                    self.failed_exports.remove(request)
                self.retry_export_button.setVisible(bool(self.failed_exports))
                self.notice_text = "已导出 / Exported: "+bundle["folder"]
                self.bundle_active = None
                self.next_bundle()
            except Exception as exc:
                self.bundle_failed(str(exc))
        C.QTimer.singleShot(0, render_next)

    def bundle_failed(self, error):
        if self.bundle_active and self.bundle_active not in self.failed_exports:
            self.failed_exports.append(self.bundle_active)
        self.bundle_active = None
        self.retry_export_button.setVisible(bool(self.failed_exports))
        self.notice_text = "导出失败 / Export failed: "+error
        self.footer.setText(self.notice_text)
        W.QMessageBox.warning(self, "Export failed", self.notice_text+"\n可重试或在数据管理页重新导出。 / Retry or use Export full report.")
        self.next_bundle()

    def retry_exports(self):
        for request in list(self.failed_exports):
            self.queue_bundle(*request)

    def show_pump(self):
        if self.popup:
            self.popup.raise_()
            self.popup.activateWindow()
            return
        dialog = W.QDialog(self)
        dialog.setWindowTitle("Pump status & cumulative delivered volume")
        dialog.resize(980, 640)
        dialog.setAttribute(C.Qt.WidgetAttribute.WA_DeleteOnClose)
        box = W.QVBoxLayout(dialog)
        self.pump_detail = W.QLabel()
        self.pump_detail.setWordWrap(True)
        box.addWidget(self.pump_detail)
        self.pump_plot = self.make_plot("Cumulative injection volume", "Delivered volume (mL)")
        self.pump_curve = self.pump_plot.plot(pen=pg.mkPen("#167cb5", width=2), symbol="o", symbolSize=4, name="Accounted volume")
        box.addWidget(self.pump_plot, 1)
        self.refill_lines = []
        box.addWidget(self.button("导出加液曲线", "Export volume plot", lambda: self.export_plots([self.pump_plot])))
        dialog.destroyed.connect(lambda: setattr(self, "popup", None))
        self.popup = dialog
        dialog.show()
        self.last_chart = 0

    def query_charts(self, snap):
        path = snap.get("path")
        pump_path = path or (snap.get("last_completed") or {}).get("path") or snap.get("test_path")
        if not path and not (self.popup and pump_path):
            return
        self.chart_busy = True
        window = self.window_box.currentData()
        start = max(0., snap["elapsed"]-window) if window else 0.
        show_pump = self.popup is not None
        def read():
            curves = {kind: {field: storage.series(path, kind, field, start) for field in ("raw", "filtered")} for kind in ("ph", "ec")} if path else {}
            injections = reporting.pump_series(pump_path, end=snap["elapsed"] if path else None) if show_pump and pump_path else None
            events = []
            return path, curves, injections, events
        self.submit(read, self.draw_charts)

    def draw_charts(self, data):
        self.chart_busy = False
        path, curves, injections, events = data
        if path and path != self.runtime.snapshot.get("path"):
            return
        oh = self.ph_axis.currentIndex() == 1
        self.ph_plot.setLabel("left", "Estimated OH⁻ (mmol/L)" if oh else "pH")
        for kind, fields in curves.items():
            for field, rows in fields.items():
                values = [hydroxide(r[1], r[2]) if oh and kind == "ph" else r[1] for r in rows]
                self.live_curves[kind][field].setData([r[0]/60 for r in rows], values, connect="finite")
                self.live_curves[kind][field].setVisible(self.raw_check.isChecked() if field == "raw" else self.processed_check.isChecked())
        if self.popup and injections:
            self.pump_plot.clear()
            charts.draw_pump(self.pump_plot, injections)


    def refresh(self):
        s = self.runtime.snapshot
        if not s:
            return
        zh = self.language == "zh"
        for kind, (combo, state, identity, connect, disconnect) in self.device_widgets.items():
            info = s["connections"].get(kind, {})
            bound = s["bindings"].get(kind, {})
            connected = info.get("connected", False)
            status = ("正常" if zh else "Connected") if connected else (("连接中" if zh else "Connecting") if info.get("connecting") else (("重连中" if zh else "Reconnecting") if bound else ("未连接" if zh else "Disconnected")))
            color = "#168a68" if connected else "#bb7c24" if bound else "#788b9a"
            state.setText(f"●  {status}   {info.get('endpoint', bound).get('port', '')}   {info.get('error', '') if not connected else ''}")
            state.setStyleSheet(f"color: {color}; font-weight: 600;")
            identity.setText("Device identity: "+str(bound.get("identity", "—")))
            connect.setEnabled(not bound and not s["scan_busy"])
            disconnect.setEnabled(bool(bound))
        self.start_button.setEnabled(not s["recording"] and not s.get("finishing") and not self.bundle_active and not self.bundle_queue)
        self.finish_button.setEnabled(s["recording"] and not s.get("finishing"))
        self.enable_button.setEnabled(s["recording"] and not s["enabled"] and not s["active"] and not s.get("finishing"))
        self.pause_button.setEnabled(s["enabled"] or bool(s["active"]))
        completed = s.get("last_completed") or {}
        shown_path = s["path"] or completed.get("path")
        self.filename.setText(Path(shown_path).name if shown_path else "—")
        phase = s["phase"] if s["active"] and s["phase"] != "idle" else s["state"]
        status = STATE_ZH.get(phase, phase) if zh else phase.replace("_", " ")
        detail = (f"{status}  ·  目标 {fmt(s['target'])}  ·  混合剩余 {s['mix_remaining']:.1f} s  ·  设置 v{s['settings']['version']}" if zh else
                  f"{status}  ·  Target {fmt(s['target'])}  ·  Mix {s['mix_remaining']:.1f} s  ·  Settings v{s['settings']['version']}")
        self.monitor_status.setText(detail+("  ·  "+s["pending_reason"] if s["pending_reason"] else ""))
        block = s.get("test_block_reason", "pump_disconnected")
        test_errors = []
        for stop_test, button in ((False, self.finite_test_button), (True, self.stop_test_button)):
            try:
                self.test_data(stop_test)
                button.setEnabled(True)
                button.setToolTip("")
                test_errors.append("")
            except (ValueError, TypeError) as exc:
                button.setEnabled(False)
                button.setToolTip(str(exc))
                test_errors.append(str(exc))
        self.test_state.setText((STATE_ZH.get(block, block) if zh else block.replace("_", " ")) if block else
                                (("就绪，可独立测试" if zh else "Ready for independent test") if "" in test_errors else
                                 ("请检查测试参数：" if zh else "Check test parameters: ")+test_errors[0]))
        latest = s.get("last_failure") or s.get("last_results", {}).get("experiment")
        self.dose_result_label.setText(dose_result_text(latest, zh).split("\n")[0])
        self.dose_result_label.setVisible(bool(latest))
        self.dose_result_label.setStyleSheet("color: #b83645;" if s.get("last_failure") else "")
        if s.get("last_failure"):
            self.monitor_status.setText(self.monitor_status.text()+" · "+latest["result"].get("error", ""))
        h = s["hardware"]
        self.hardware_text.setPlainText((f"注射器容量：{fmt(h.get('capacity_ml'))} mL\n有效面积：{fmt(h.get('area_mm2'))} mm²\n仪器推力：{fmt(h.get('force_percent'),0)} %\n注射器型号与校准历史：仪器未提供，可在实验设置中添加备注。" if zh else
                                        f"Syringe capacity: {fmt(h.get('capacity_ml'))} mL\nEffective area: {fmt(h.get('area_mm2'))} mm²\nInstrument force: {fmt(h.get('force_percent'),0)} %\nSyringe model and calibration history are not provided. Add a note in Settings."))
        if not self.test_force.text() and not self.test_force.reviewed and not self.test_force.hasFocus() and s["hardware"].get("force_percent"):
            self.test_force.setText(str(s["hardware"]["force_percent"]))
        for kind in ("ph", "ec"):
            sample = s["samples"].get(kind, {})
            connected = s["connections"].get(kind, {}).get("connected")
            self.cards[kind].setText(fmt(sample.get("filtered", sample.get("value")) if sample.get("filtered") is not None else sample.get("value")) if connected else "—")
        ph = s["samples"].get("ph", {})
        ec = s["samples"].get("ec", {})
        temp = ph.get("temperature") if s["connections"].get("ph", {}).get("connected") else ec.get("temperature") if s["connections"].get("ec", {}).get("connected") else None
        self.cards["temp"].setText(fmt(temp, 2))
        self.cards["used"].setText(fmt(s["load"]["used_ml"]))
        self.cards["remaining"].setText(fmt(s["remaining"]))
        charge = s.get("charge", {})
        q = charge.get("coulombs", 0.)
        scaled = abs(q) >= 1000
        self.cards["charge"].setText(fmt(q/1000 if scaled else q, 3) if charge.get("available") else "—")
        partial = (" · 部分时段估算" if zh else " · Partial coverage") if charge.get("partial") else ""
        self.charge_unit.setText((("kC" if scaled else "C")+partial) if charge.get("available") else "")
        self.cards["charge"].setToolTip("Charge estimate unavailable: calculation overflow." if charge.get("overflow") else
                                       "Q = Σ I × Δt; manual current, recording time; pump pauses do not pause charge.")
        if s["load"]["uncertain"]:
            self.cards["used"].setText("? "+fmt(s["load"]["used_ml"]))
            self.cards["remaining"].setText("?")
        elif s["load"].get("estimated"):
            self.cards["used"].setText("≈ "+fmt(s["load"]["used_ml"]))
            self.cards["remaining"].setText("≈ "+fmt(s["remaining"]))
        self.load_status.setText((f"有效额度：{fmt(s['load']['limit_ml'])} mL  ·  已用：{fmt(s['load']['used_ml'])} mL  ·  剩余：{fmt(s['remaining'])} mL" if zh else
                                 f"Effective allowance: {fmt(s['load']['limit_ml'])} mL   ·   Delivered: {fmt(s['load']['used_ml'])} mL   ·   Remaining: {fmt(s['remaining'])} mL"))
        if s["load"].get("estimated"):
            self.load_status.setText(self.load_status.text()+(" · 含估算量，详见注射记录" if zh else " · Includes estimates; see injection records"))
        pending = s.get("pending")
        self.settings_status.setText((f"当前有效 v{s['settings']['version']}"+(f" → 待生效 v{pending['version']} · 本针及完整混合结束后生效" if pending else " · 无待生效修改")) if zh else
                                    f"Effective v{s['settings']['version']}"+(f"  → Pending v{pending['version']} · applies after the current dose and full mixing wait" if pending else "  · No pending change"))
        self.settings_status.setToolTip(json.dumps({"effective": s["settings"], "pending": pending}, ensure_ascii=False, indent=2))
        def describe(cfg):
            c = cfg[cfg["control"]]
            if zh:
                return (f"单次量 {fmt(cfg['dose_ml'])} mL · 速率 {fmt(cfg['flow_ml_min'])} mL/min · 推力 {fmt(cfg['force_percent'],0)} % · 混合 {fmt(cfg['mix_s'],1)} s\n"
                        f"{cfg['control']} · 目标 {fmt(c['target'])} ± {fmt(c['tolerance'])} · 报警 [{fmt(c['lower'])}, {fmt(c['upper'])}] · {'低于目标加液' if c['direction']=='raise' else '高于目标加液'}")
            return (f"Dose {fmt(cfg['dose_ml'])} mL · Flow {fmt(cfg['flow_ml_min'])} mL/min · Force {fmt(cfg['force_percent'],0)} % · Mix {fmt(cfg['mix_s'],1)} s\n"
                    f"{cfg['control']} · Target {fmt(c['target'])} ± {fmt(c['tolerance'])} · Alarm [{fmt(c['lower'])}, {fmt(c['upper'])}] · {c['direction']}")
        self.effective_settings.setText(("当前有效 / Effective: " if zh else "Effective: ")+describe(s["settings"])+("\n"+("下一周期 / Next cycle: " if zh else "Next cycle: ")+describe(pending) if pending else ""))
        if s.get("pending_allowance") is not None:
            self.load_status.setText(self.load_status.text()+f" · Pending allowance: {s['pending_allowance']} mL")
        if s["remaining"] is not None and s["settings"].get("dose_ml") is not None and s["remaining"]+1e-10 < s["settings"]["dose_ml"]:
            self.load_status.setText(self.load_status.text()+(" · 余量不足完整一针；记录与有效设置保留。" if zh else
                                                            " · Insufficient for a full dose; recording and settings are retained."))
        times = []
        for kind, sample in (("pH", ph), ("Conductivity", ec)):
            age = max(0, time.monotonic()-sample["mono"]) if "mono" in sample else None
            times.append((f"{kind}：{fmt(age,1)} s 前接收 · {'实时读数' if sample.get('live') else '缓存 / 尚无实时读数'}" if zh else
                          f"{kind}: {fmt(age, 1)} s since last received · {'live' if sample.get('live') else 'buffered / unavailable'}"))
        self.reading_note.setText("   |   ".join(times))
        for i, kind in enumerate(("ph", "ec")):
            cfg = s["settings"][kind]
            target = cfg["target"] if cfg["target"] is not None else s["auto_target"][kind]
            tol = cfg["tolerance"]
            values = [target-tol if target is not None and tol is not None else None, target+tol if target is not None and tol is not None else None, cfg["lower"], cfg["upper"]]
            for line, value in zip(self.target_lines[i], values):
                line.setVisible(value is not None and not (i == 0 and self.ph_axis.currentIndex()))
                if value is not None:
                    line.setValue(value)
        resulttext = dose_result_text(s.get("last_results", {}).get("test"), zh)
        if self.test_results.toPlainText() != resulttext:
            self.test_results.setPlainText(resulttext)
        errors = [n for n in s["notices"] if n["kind"] == "error"]
        if errors and errors[-1]["time"] != self.last_error:
            self.last_error = errors[-1]["time"]
            self.notice_text = errors[-1]["data"].get("error", "")
        self.footer.setText(s["storage_error"] or self.notice_text or detail)
        pending_export = s.get("export_pending")
        if pending_export:
            if pending_export["path"] not in self.export_seen:
                self.export_seen.append(pending_export["path"])
                self.queue_bundle(pending_export["path"])
            self.runtime.command("export_ack", path=pending_export["path"])
        if self.popup:
            self.pump_detail.setText(f"{s['phase']}  ·  Device counter: {fmt(s['pump'].get('counter_ml'))} mL  ·  Experiment total: {fmt(s['cumulative_ml'])} mL\n"
                                    f"Pump state: {s['pump'].get('state', '—')}  ·  Live estimate: {fmt(s['progress'].get('estimate_ml'))} mL  ·  {s['pending_reason']}\n"+
                                    dose_result_text(latest or s.get("last_results", {}).get("test"), zh))
        if self.navigation.currentRow() == 4 and self.last_path != s["path"]:
            self.refresh_history()
            self.last_path = s["path"]
        if not self.chart_busy and time.monotonic()-self.last_chart >= 1 and (self.navigation.currentRow() == 3 or self.popup):
            self.last_chart = time.monotonic()
            self.query_charts(s)

    def closeEvent(self, event):
        with self.runtime.commands.mutex:
            start_queued = any(command[0] == "start_recording" for command in self.runtime.commands.queue)
        if self.runtime.thread.is_alive() and (self.runtime.engine.recording or self.runtime.finish_pending or start_queued):
            event.ignore()
            W.QMessageBox.information(self, "Experiment recording", "请先点击“结束实验”，等待保存完成后再关闭。\nEnd the experiment and wait for saving before closing.")
            return
        if self.bundle_active or self.bundle_queue:
            event.ignore()
            W.QMessageBox.information(self, "Export in progress", "正在导出，请等待完成。\nPlease wait for the export to finish.")
            return
        if not self.closing:
            self.closing = True
            self.runtime.close()
        if self.runtime.thread.is_alive():
            self.footer.setText("Stopping devices and saving data…")
            event.ignore()
            C.QTimer.singleShot(300, self.close)
            return
        self.timer.stop()
        event.accept()
