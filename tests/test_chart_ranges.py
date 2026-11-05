import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time

import pytest
from PySide6 import QtCore as C, QtWidgets as W, QtTest

from em2.runtime import Runtime
from em2.ui import Window


@pytest.fixture
def window(tmp_path):
    app = W.QApplication.instance() or W.QApplication([])
    runtime = Runtime(tmp_path)
    w = Window(runtime)
    w.show()
    w.timer.stop()
    app.processEvents()
    yield w, runtime, app
    runtime.close()
    runtime.thread.join(10)
    w.close()
    app.processEvents()


def test_linked_x_independent_y_and_refresh_preserves_manual_view(window):
    w, _, app = window
    w.navigation.setCurrentRow(3)
    for kind in ("ph", "ec"):
        w.live_curves[kind]["filtered"].setData([0, 10, 20], [7, 8, 9])
    app.processEvents()
    p, e = w.ph_plot.range_control, w.ec_plot.range_control
    p.set_axis(0, True, (3., 7.))
    p.set_axis(1, True, (6., 10.))
    e.set_axis(1, True, (1., 100.))
    for i in range(5):
        w.live_curves["ph"]["filtered"].setData([0, 10, 20, 30+i], [7, 8, 9, 99])
        p.apply(); e.apply(); app.processEvents()
    assert w.ph_plot.viewRange()[0] == pytest.approx([3, 7])
    assert w.ec_plot.viewRange()[0] == pytest.approx([3, 7])
    assert w.ph_plot.viewRange()[1] == pytest.approx([6, 10])
    assert w.ec_plot.viewRange()[1] == pytest.approx([1, 100])
    e.set_axis(0, True, (8., 12.))
    assert p.x_query() == (8., 12.)
    p.auto_x = e.auto_x = (0., 60.)
    p.restore(); app.processEvents()
    assert not p.manual[0] and not e.manual[0] and not p.manual[1]
    assert e.manual[1]
    assert w.ph_plot.viewRange()[0] == pytest.approx([0, 60])


def test_native_range_menu_errors_unit_switch_and_refresh_protects_draft(window):
    w, _, app = window
    p = w.ph_plot.range_control
    w.ph_plot.setXRange(2, 4, padding=0)
    w.ph_plot.getViewBox().sigRangeChangedManually.emit([True, False])
    assert p.x_query() == (2, 4)
    p.set_axis(1, True, (6, 8))
    w.ph_axis.setCurrentIndex(1)
    assert not p.manual[1] and p.manual[0]
    assert w.ph_plot.panel is w.ph_plot
    menu = p.menu
    assert menu.isWindow() and not menu.isVisible()
    edits = [menu.ctrl[0].minText, menu.ctrl[0].maxText]
    menu.begin_edit(0)
    edits[0].setText("nan")
    before = p.x_query()
    menu.xRangeTextChanged()
    assert p.x_query() == before
    assert "请输入" in edits[0].toolTip()
    for i in range(5):
        p.auto_x = (0, 100+i)
        p.apply()
        menu.updateState()
        assert edits[0].text() == "nan"
    edits[0].setText("1")
    edits[1].setText("5")
    menu.xRangeTextChanged()
    assert p.x_query() == (1, 5)
    assert not edits[0].toolTip()
    p.reset(absolute=True)
    menu.begin_edit(0)
    edits[0].setText("1700000000")
    edits[1].setText("1700000060")
    menu.xRangeTextChanged()
    assert p.x_query() == (1700000000, 1700000060)
    assert [float(e.text()) for e in edits] == [1700000000, 1700000060]
    menu.xAutoClicked()
    assert not p.manual[0]


def test_real_menu_focus_draft_survives_refresh_and_restore_clears_it(window):
    w, _, app = window
    p = w.ph_plot.range_control
    p.auto_x = (0, 60)
    p.apply()
    submenu = p.menu.actions()[1].menu()
    submenu.popup(w.mapToGlobal(w.rect().center()))
    app.processEvents()
    low, high = p.menu.ctrl[0].minText, p.menu.ctrl[0].maxText
    QtTest.QTest.mouseClick(low, C.Qt.MouseButton.LeftButton)
    QtTest.QTest.keyClick(low, C.Qt.Key.Key_A, C.Qt.KeyboardModifier.ControlModifier)
    QtTest.QTest.keyClicks(low, "1.")
    low.setCursorPosition(1)
    for i in range(4):
        p.auto_x = (i, 60+i)
        w.live_curves["ph"]["filtered"].setData([0, 1, 100+i], [7, 8, 9])
        p.apply(); app.processEvents(); p.menu.updateState()
        assert low.text() == "1." and low.cursorPosition() == 1
    assert p.manual[0]
    high.setText("5")
    QtTest.QTest.keyClick(low, C.Qt.Key.Key_Return)
    assert p.x_query() == (1, 5)
    low.setText("invalid"); p.menu.commit_range(0)
    p.restore(); app.processEvents()
    assert not any(p.menu.editing) and not low.styleSheet()
    assert low.text() != "invalid"
    submenu.close()


def test_manual_query_reads_old_interval_and_discards_outdated_results(window, monkeypatch):
    w, runtime, app = window
    runtime.command("start_recording", name="Range test")
    end = time.monotonic()+3
    while not runtime.snapshot.get("recording") and time.monotonic() < end:
        app.processEvents(); time.sleep(.01)
    snap = dict(runtime.snapshot, elapsed=7200.)
    w.chart_path = snap["path"]
    w.ph_plot.range_control.set_axis(0, True, (1., 2.))
    calls, jobs = [], []
    monkeypatch.setattr("em2.ui.storage.series", lambda path, kind, field, start, end: calls.append((start, end)) or [(start, 7., 20.)])
    monkeypatch.setattr(w, "submit", lambda read, done: jobs.append((read, done)))
    w.query_charts(snap)
    result = jobs[-1][0]()
    assert calls == [(60., 120.)]*4
    w.ph_plot.range_control.set_axis(0, True, (4., 5.))
    jobs[-1][1](result)
    assert w.live_curves["ph"]["filtered"].xData is None
    assert not w.chart_busy
    w.full_live_range()
    assert not w.ph_plot.range_control.manual[0]
    assert w.window_box.currentData() is None


def test_font_model_labels_and_empty_constant_autorange(window):
    w, runtime, app = window
    w.navigation.setCurrentRow(3)
    app.processEvents()
    assert w.cards["charge"].font().pointSizeF() == w.cards["ph"].font().pointSizeF() == 23
    for plot in (w.ph_plot, w.ec_plot, *w.history_plots):
        plot.range_control.restore()
        plot.range_control.apply()
    w.live_curves["ph"]["filtered"].setData([0, 1], [7, 7])
    w.target_lines[0][0].setValue(1000)
    w.target_lines[0][0].show()
    app.processEvents()
    w.ph_plot.getViewBox().autoRange()
    assert w.ph_plot.viewRange()[1][0] < 7 < w.ph_plot.viewRange()[1][1] < 10
    w.ec_model.setCurrentIndex(1)
    end = time.monotonic()+2
    while runtime.ec_choice["driver"] != "m80" and time.monotonic() < end:
        app.processEvents(); time.sleep(.01)
    assert "每秒" in w.connection_note.text() and "每秒" in w.ec_interval_label.text()
    w.switch_language(1)
    assert "every second" in w.connection_note.text()
    runtime.command("start_recording", name="Locked selection")
    end = time.monotonic()+3
    while not runtime.snapshot.get("recording") and time.monotonic() < end:
        app.processEvents(); time.sleep(.01)
    w.refresh()
    assert not w.ec_model.isEnabled() and not w.ec_address.isEnabled()


def test_manual_view_export_with_empty_peer_and_hidden_raw(window, monkeypatch):
    from xml.etree import ElementTree
    import importlib
    w, runtime, app = window
    w.navigation.setCurrentRow(3)
    w.live_curves["ec"]["filtered"].setData([0, 1, 2], [.5, .6, .7])
    for kind in ("ph", "ec"):
        w.live_curves[kind]["raw"].hide()
    w.ec_plot.range_control.set_axis(0, True, (.25, 1.25))
    w.ec_plot.range_control.set_axis(1, True, (.4, .8))
    app.processEvents()
    monkeypatch.setattr(w, "export_finished", lambda _: None)
    exporter = importlib.import_module("pyqtgraph.exporters.SVGExporter")
    converter = exporter.correctCoordinates
    before = w.ec_plot.viewRange()
    w.export_plots([w.ph_plot, w.ec_plot])
    assert exporter.correctCoordinates is converter
    assert w.ec_plot.viewRange() == before
    assert len(list((runtime.root/"exports").rglob("*.png"))) == 2
    svg = list((runtime.root/"exports").rglob("*.svg"))
    assert len(svg) == 2
    for path in svg:
        root = ElementTree.parse(path).getroot()
        assert root.tag.endswith("svg")
        assert path.stat().st_size > 1000
