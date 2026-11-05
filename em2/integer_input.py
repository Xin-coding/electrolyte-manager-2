"""Visible integer drafts with explicit, reliably clickable step buttons."""
import re

from PySide6 import QtCore as C, QtWidgets as W


def unsigned_integer(text, maximum):
    # Do not strip, round, accept scientific notation, or silently repair drafts.
    if not re.fullmatch(r"[0-9]+", text) or len(text) > 12:
        raise ValueError("Use digits 0–9 only")
    value = int(text)
    if value > maximum:
        raise ValueError(f"Enter 0–{maximum}")
    return value


class IntegerInput(W.QWidget):
    changed = C.Signal()

    def __init__(self, maximum, value=0):
        super().__init__()
        self.maximum = maximum
        self.edit = W.QLineEdit(str(value))
        self.edit.setMinimumWidth(42)
        self.edit.setMaxLength(32)
        self.edit.installEventFilter(self)
        self.up = W.QToolButton()
        self.down = W.QToolButton()
        self.up.setText("▲")
        self.down.setText("▼")
        self.up.setAccessibleName("Increase")
        self.down.setAccessibleName("Decrease")
        buttons = W.QVBoxLayout()
        buttons.setSpacing(1)
        for button in (self.up, self.down):
            button.setFixedSize(25, 18)
            button.setStyleSheet("QToolButton { padding: 0; border: 1px solid #becfdf; border-radius: 2px; background: white; }")
            button.setAutoRepeat(True)
            buttons.addWidget(button)
        row = W.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        row.addWidget(self.edit)
        row.addLayout(buttons)
        self.up.clicked.connect(lambda: self.step(1))
        self.down.clicked.connect(lambda: self.step(-1))
        self.edit.textChanged.connect(self.changed)
        self.setFocusProxy(self.edit)

    def value(self):
        return unsigned_integer(self.edit.text(), self.maximum)

    def setValue(self, value):
        if type(value) is not int or not 0 <= value <= self.maximum:
            raise ValueError("Invalid integer value")
        self.edit.setText(str(value))

    def step(self, amount):
        try:
            value = self.value()
        except ValueError:
            self.changed.emit()
            return
        self.setValue(max(0, min(self.maximum, value+amount)))

    def mark_invalid(self, invalid):
        if self.edit.property("invalid") != invalid:
            self.edit.setProperty("invalid", invalid)
            self.edit.style().unpolish(self.edit)
            self.edit.style().polish(self.edit)
            self.edit.update()

    def eventFilter(self, obj, event):
        if obj is self.edit and event.type() == C.QEvent.Type.KeyPress:
            if event.key() in (C.Qt.Key.Key_Up, C.Qt.Key.Key_Down):
                self.step(1 if event.key() == C.Qt.Key.Key_Up else -1)
                return True
        return super().eventFilter(obj, event)
