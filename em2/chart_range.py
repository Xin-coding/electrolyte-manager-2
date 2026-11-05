"""Explicit viewport state, independent of data replacement and report exports."""
import math

from PySide6 import QtCore as C, QtWidgets as W


class RangeController(C.QObject):
    changed = C.Signal()
    full_requested = C.Signal()

    def __init__(self, plot, language=lambda: "zh"):
        super().__init__(plot)
        self.plot, self.language = plot, language
        self.manual = [False, False]
        self.bounds = [None, None]
        self.auto_x = None
        self.absolute = False
        self.revision = 0
        self.guard = False
        self.peers = []
        self.dialog = None
        self.panel = W.QWidget()
        column = W.QVBoxLayout(self.panel)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        column.addWidget(plot, 1)
        bar = W.QHBoxLayout()
        bar.setSpacing(4)
        self.modes = []
        for axis in range(2):
            bar.addWidget(W.QLabel("X" if axis == 0 else "Y"))
            combo = W.QComboBox()
            combo.setMinimumContentsLength(2)
            combo.addItems(["自动", "手动"])
            combo.currentIndexChanged.connect(lambda index, a=axis: self.change_mode(a, bool(index)))
            bar.addWidget(combo)
            self.modes.append(combo)
        self.edit_button = W.QPushButton()
        self.edit_button.clicked.connect(self.edit_range)
        self.auto_button = W.QPushButton()
        self.auto_button.clicked.connect(self.restore)
        self.full_button = W.QPushButton()
        self.full_button.clicked.connect(self.full)
        for button in (self.edit_button, self.auto_button, self.full_button):
            button.setStyleSheet("padding: 4px 7px;")
            bar.addWidget(button)
        bar.addStretch()
        column.addLayout(bar)
        self.set_language()
        plot.plotItem.setMenuEnabled(False)
        plot.plotItem.hideButtons()
        plot.getViewBox().sigRangeChangedManually.connect(self.mouse_range)

    def set_language(self):
        zh = self.language() == "zh"
        for combo in self.modes:
            combo.setItemText(0, "自动" if zh else "Auto")
            combo.setItemText(1, "手动" if zh else "Manual")
        self.edit_button.setText("范围…" if zh else "Range…")
        self.auto_button.setText("恢复自动" if zh else "Auto")
        self.full_button.setText("全图" if zh else "Full")

    def link_x(self, other):
        self.peers.append(other)
        other.peers.append(self)
        self.plot.setXLink(other.plot)

    def key(self):
        return self.revision

    def x_query(self):
        return tuple(self.bounds[0]) if self.manual[0] and self.bounds[0] else None

    def _sync_combo(self, axis):
        combo = self.modes[axis]
        with C.QSignalBlocker(combo):
            combo.setCurrentIndex(int(self.manual[axis]))

    def set_axis(self, axis, manual, bounds=None, *, propagate=True):
        if bounds is not None:
            bounds = tuple(float(v) for v in bounds)
            if not all(math.isfinite(v) for v in bounds) or bounds[0] >= bounds[1]:
                raise ValueError("Invalid axis range")
        self.manual[axis] = manual
        if manual:
            self.bounds[axis] = bounds or tuple(self.plot.viewRange()[axis])
        self._sync_combo(axis)
        self.revision += 1
        self.apply()
        if axis == 0 and propagate:
            for peer in self.peers:
                peer.set_axis(0, manual, self.bounds[0], propagate=False)
        self.changed.emit()

    def change_mode(self, axis, manual):
        if not self.guard:
            self.set_axis(axis, manual)

    def mouse_range(self, axes):
        if self.guard:
            return
        ranges = self.plot.viewRange()
        for axis in range(2):
            if axes[axis]:
                self.set_axis(axis, True, ranges[axis])

    def apply(self):
        self.guard = True
        try:
            for axis in range(2):
                bounds = self.bounds[axis] if self.manual[axis] else self.auto_x if axis == 0 else None
                if bounds is not None:
                    self.plot.enableAutoRange(axis=axis, enable=False)
                    if axis == 0:
                        self.plot.setXRange(*bounds, padding=0)
                    else:
                        self.plot.setYRange(*bounds, padding=0)
                else:
                    self.plot.enableAutoRange(axis=axis, enable=True)
        finally:
            self.guard = False

    def restore(self):
        self.set_axis(0, False)
        self.set_axis(1, False)

    def full(self):
        self.auto_x = None
        for peer in self.peers:
            peer.auto_x = None
        self.restore()
        self.full_requested.emit()

    def reset(self, *, absolute=None):
        if absolute is not None:
            self.absolute = absolute
        self.auto_x = None
        self.bounds = [None, None]
        self.restore()

    def edit_range(self):
        if self.dialog:
            self.dialog.raise_()
            return
        zh = self.language() == "zh"
        dialog = W.QDialog(self.panel)
        self.dialog = dialog
        dialog.setAttribute(C.Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle("图表范围" if zh else "Chart range")
        grid = W.QGridLayout(dialog)
        titles = ["轴", "模式", "下限", "上限"] if zh else ["Axis", "Mode", "Lower", "Upper"]
        for col, title in enumerate(titles):
            grid.addWidget(W.QLabel(title), 0, col)
        modes, editors = [], []
        for axis in range(2):
            grid.addWidget(W.QLabel(("X · 本地时间" if zh else "X · Local time") if axis == 0 and self.absolute
                                   else "X · min" if axis == 0 else "Y"), axis+1, 0)
            mode = W.QComboBox()
            mode.addItems(["自动", "手动"] if zh else ["Auto", "Manual"])
            mode.setCurrentIndex(int(self.manual[axis]))
            modes.append(mode)
            grid.addWidget(mode, axis+1, 1)
            row = []
            for col, value in enumerate(self.plot.viewRange()[axis], 2):
                if axis == 0 and self.absolute:
                    edit = W.QDateTimeEdit(C.QDateTime.fromMSecsSinceEpoch(round(value*1000)))
                    edit.setDisplayFormat("yyyy-MM-dd HH:mm:ss.zzz")
                    edit.setCalendarPopup(True)
                else:
                    edit = W.QLineEdit(format(value, ".12g"))
                    edit.setMinimumWidth(120)
                grid.addWidget(edit, axis+1, col)
                edit.setEnabled(bool(mode.currentIndex()))
                row.append(edit)
            mode.currentIndexChanged.connect(lambda i, fields=row: [e.setEnabled(bool(i)) for e in fields])
            editors.append(row)
        error = W.QLabel()
        error.setStyleSheet("color:#b83645")
        error.setWordWrap(True)
        grid.addWidget(error, 3, 0, 1, 4)
        buttons = W.QDialogButtonBox(W.QDialogButtonBox.StandardButton.Apply | W.QDialogButtonBox.StandardButton.Close)
        grid.addWidget(buttons, 4, 0, 1, 4)
        def apply():
            proposed = []
            try:
                for axis in range(2):
                    if not modes[axis].currentIndex():
                        proposed.append(None)
                        continue
                    values = tuple(e.dateTime().toMSecsSinceEpoch()/1000 if isinstance(e, W.QDateTimeEdit)
                                   else float(e.text().strip()) for e in editors[axis])
                    if not all(math.isfinite(v) for v in values) or values[0] >= values[1]:
                        raise ValueError()
                    proposed.append(values)
            except ValueError:
                error.setText("请输入有效上下限，下限必须小于上限。" if zh else "Enter finite limits; lower must be below upper.")
                return
            for axis in range(2):
                self.set_axis(axis, proposed[axis] is not None, proposed[axis])
            error.setText("已应用" if zh else "Applied")
        buttons.button(W.QDialogButtonBox.StandardButton.Apply).clicked.connect(apply)
        buttons.rejected.connect(dialog.close)
        dialog.destroyed.connect(lambda: setattr(self, "dialog", None))
        dialog.show()


def attach(plot, language):
    controller = RangeController(plot, language)
    plot.range_control = controller
    plot.panel = controller.panel
    return plot
