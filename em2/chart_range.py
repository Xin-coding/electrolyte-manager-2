"""Explicit viewport state, independent of data replacement and report exports."""
import math

from PySide6 import QtCore as C, QtGui as G, QtWidgets as W
from pyqtgraph.graphicsItems.ViewBox.ViewBoxMenu import ViewBoxMenu


class StableRangeMenu(ViewBoxMenu):
    """The 2.1 plot context menu, with range drafts protected from live refresh."""
    def __init__(self, view, controller):
        self.controller = controller
        self.editing = [False, False]
        self.submenus = []
        super().__init__(view)
        # PySide 6.11 may destroy a submenu when its temporary QAction wrapper
        # is collected. Keep both the menus and their actions alive together.
        self.action_refs = self.actions() + [a for menu in self.submenus for a in menu.actions()]
        for axis, ui in enumerate(self.ctrl):
            for edit in (ui.minText, ui.maxText):
                edit.setValidator(None)
                edit.installEventFilter(self)
                edit.textEdited.connect(lambda _, a=axis: self.begin_edit(a))

    def addMenu(self, *args):
        # Retain the submenu wrappers for the lifetime of this menu.
        menu = super().addMenu(*args)
        self.submenus.append(menu)
        return menu

    def begin_edit(self, axis):
        self.editing[axis] = True
        if not self.controller.manual[axis]:
            self.controller.set_axis(axis, True)

    def eventFilter(self, obj, event):
        if event.type() == C.QEvent.Type.FocusIn:
            for axis, ui in enumerate(self.ctrl):
                if obj in (ui.minText, ui.maxText):
                    self.begin_edit(axis)
        return super().eventFilter(obj, event)

    def updateState(self):
        held = []
        blockers = []
        for axis, ui in enumerate(self.ctrl):
            for name in ("manualRadio", "autoRadio", "autoPercentSpin", "mouseCheck", "linkCombo",
                         "autoPanCheck", "visibleOnlyCheck", "invertCheck"):
                blockers.append(C.QSignalBlocker(getattr(ui, name)))
            if self.editing[axis]:
                for edit in (ui.minText, ui.maxText):
                    held.append((edit, edit.text(), edit.cursorPosition(), edit.selectionStart(), len(edit.selectedText())))
        super().updateState()
        # Retain second-level precision on absolute-time axes as well.
        for axis, ui in enumerate(self.ctrl):
            if not self.editing[axis]:
                for edit, value in zip((ui.minText, ui.maxText), self.view().getState(copy=False)["targetRange"][axis]):
                    edit.setText(format(value, ".12g"))
        for edit, text, cursor, start, length in held:
            if edit.text() != text:
                edit.setText(text)
            if start >= 0:
                edit.setSelection(start, length)
            else:
                edit.setCursorPosition(cursor)
        for axis, ui in enumerate(self.ctrl):
            ui.manualRadio.setChecked(self.controller.manual[axis])
            ui.autoRadio.setChecked(not self.controller.manual[axis])
        del blockers

    def set_language(self):
        zh = self.controller.language() == "zh"
        self.viewAll.setText("查看全图" if zh else "View All")
        for ui in self.ctrl:
            ui.manualRadio.setText("手动" if zh else "Manual")
            ui.autoRadio.setText("自动" if zh else "Auto")

    def commit_range(self, axis):
        ui = self.ctrl[axis]
        try:
            values = tuple(float(e.text()) for e in (ui.minText, ui.maxText))
            if not all(math.isfinite(v) for v in values) or values[0] >= values[1]:
                raise ValueError()
        except ValueError:
            self.editing[axis] = True
            message = "请输入有效上下限，且下限小于上限" if self.controller.language() == "zh" else "Enter finite limits; lower must be below upper"
            for edit in (ui.minText, ui.maxText):
                edit.setStyleSheet("border: 2px solid #b83645; background: #fff5f5;")
                edit.setToolTip(message)
            return
        self.editing[axis] = False
        for edit in (ui.minText, ui.maxText):
            edit.setStyleSheet("")
            edit.setToolTip("")
        self.controller.set_axis(axis, True, values)

    def automatic(self, axis, fraction=None):
        self.clear_draft(axis)
        self.controller.auto_fraction[axis] = fraction if fraction is not None else self.ctrl[axis].autoPercentSpin.value()*.01
        self.controller.set_axis(axis, False)

    def autoRange(self):
        self.controller.full()

    def clear_draft(self, axis):
        self.editing[axis] = False
        for edit in (self.ctrl[axis].minText, self.ctrl[axis].maxText):
            edit.setStyleSheet("")
            edit.setToolTip("")

    def xManualClicked(self): self.controller.set_axis(0, True)
    def yManualClicked(self): self.controller.set_axis(1, True)
    def xRangeTextChanged(self): self.commit_range(0)
    def yRangeTextChanged(self): self.commit_range(1)
    def xAutoClicked(self): self.automatic(0)
    def yAutoClicked(self): self.automatic(1)
    def xAutoSpinChanged(self, value): self.automatic(0, value*.01)
    def yAutoSpinChanged(self, value): self.automatic(1, value*.01)


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
        self.auto_fraction = [True, True]
        self.panel = plot
        view = plot.getViewBox()
        previous = view.menu
        self.menu = StableRangeMenu(view, self)
        view.menu = self.menu
        self.menu.setParent(plot)
        if previous:
            previous.deleteLater()
        plot.plotItem.setMenuEnabled(True)
        plot.plotItem.showButtons()
        plot.plotItem.autoBtn.clicked.disconnect(plot.plotItem.autoBtnClicked)
        plot.plotItem.autoBtn.clicked.connect(self.restore)
        view.sigRangeChangedManually.connect(self.mouse_range)
        self.set_language()

    def set_language(self):
        self.menu.set_language()

    def link_x(self, other):
        self.peers.append(other)
        other.peers.append(self)
        self.plot.setXLink(other.plot)

    def key(self):
        return self.revision

    def x_query(self):
        return tuple(self.bounds[0]) if self.manual[0] and self.bounds[0] else None

    def set_axis(self, axis, manual, bounds=None, *, propagate=True):
        if bounds is not None:
            bounds = tuple(float(v) for v in bounds)
            if not all(math.isfinite(v) for v in bounds) or bounds[0] >= bounds[1]:
                raise ValueError("Invalid axis range")
        self.manual[axis] = manual
        if not manual:
            self.menu.clear_draft(axis)
        if manual:
            self.bounds[axis] = bounds or tuple(self.plot.viewRange()[axis])
        self.revision += 1
        self.apply()
        self.menu.updateState()
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
                    self.plot.enableAutoRange(axis=axis, enable=self.auto_fraction[axis])
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
        self.menu.popup(G.QCursor.pos())


def attach(plot, language):
    controller = RangeController(plot, language)
    plot.range_control = controller
    plot.panel = controller.panel
    return plot
