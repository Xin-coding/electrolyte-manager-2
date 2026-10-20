import csv
import hashlib
import json
import os
import sqlite3
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
from PySide6 import QtCore as C, QtGui as G, QtWidgets as W

from em2 import charts, reporting, storage
from em2.charge import Charge, current_value
from em2.models import Sample, Settings, Dose, DoseResult
from em2.runtime import Runtime
from em2.ui import Window


def experiment(folder, name="Report", purpose="experiment", amounts=(.1, .2, .3)):
    store = storage.Store.create(folder, name, {})
    total = 0.
    for i, amount in enumerate(amounts):
        total = total+amount if amount is not None and total is not None else None
        store.put("injection", {
            "dose": {"id": str(i), "purpose": purpose, "volume_ml": .1},
            "result": {"amount_ml": amount, "source": "device_counter" if i != 1 else "programmed_cap_estimate", "stopped": True},
            "t": (i+1)*10., "cumulative_ml": total if purpose == "experiment" else 999.,
            "load": {"used_ml": amount}})
    store.event(25., "refill", {})
    store.sample(1., Sample("ph", 7., 1., 1, temperature=25.), 7.)
    assert store.finalize(100., {}, "2026-10-20T01:00:00+00:00")
    assert store.close(finalized=True)
    return store.path


def pump(app, predicate, timeout=10):
    deadline = time.monotonic()+timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.01)
    assert predicate()


def test_piecewise_charge_unknown_zero_signed_and_frozen():
    charge = Charge()
    charge.begin(0., None)
    charge.change(10., 2.)
    charge.change(20., -1.)
    charge.change(30., 0.)
    charge.finish(40.)
    result = charge.snapshot(1000.)
    assert result["coulombs"] == 10.
    assert result["known_s"] == 30.
    assert result["unknown_s"] == 10.
    assert result["partial"] and result["available"]
    assert current_value({"current_a": 0}) == 0.
    assert current_value({"current_a": "1 A"}) is None


def test_charge_overflow_cannot_break_database_finalization():
    charge = Charge()
    charge.begin(0, 1e308)
    charge.finish(10)
    result = charge.snapshot(100)
    assert result["overflow"] and not result["available"]
    assert json.dumps(result, allow_nan=False)


def test_historical_pump_carry_tail_refill_and_readonly(tmp_path):
    path = experiment(tmp_path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    data = reporting.pump_series(path, 15., 90.)
    assert data["points"][0][:2] == (15., .1)
    assert data["points"][-1][:2] == (90., pytest.approx(.6))
    assert data["count"] == 3 and data["estimates"] == 1
    assert [event["t"] for event in data["refills"]] == [25.]
    assert storage.summary(path)["duration"] == 100.
    assert before == hashlib.sha256(path.read_bytes()).hexdigest()


def test_tests_do_not_use_saved_formal_total_and_unknown_is_gap(tmp_path):
    path = experiment(tmp_path, purpose="test", amounts=(.1, None, .3))
    data = reporting.pump_series(path)
    assert data["points"][1][1] == .1
    assert data["points"][-1][1] is None
    assert data["unknown"] == 2
    output = tmp_path/"test.csv"
    reporting.export_pump_csv(path, output)
    rows = list(csv.DictReader(output.open(encoding="utf-8-sig")))
    assert float(rows[0]["cumulative_ml"]) == .1
    assert rows[1]["cumulative_ml"] == ""
    assert float(rows[0]["recorded_cumulative_ml"]) == 999.


def test_bundle_full_rows_names_collision_and_retry(tmp_path):
    path = experiment(tmp_path/"one")
    bundle = reporting.prepare_bundle(path, tmp_path/"exports")
    from pathlib import Path
    folder = Path(bundle["folder"])
    assert sorted(p.name for p in folder.glob("*.csv")) == ["Report_cond.csv", "Report_pH.csv", "Report_pump injection.csv"]
    assert len(list(csv.DictReader((folder/"Report_pump injection.csv").open(encoding="utf-8-sig")))) == 3
    assert len(list(csv.DictReader((folder/"Report_cond.csv").open(encoding="utf-8-sig")))) == 0
    assert reporting.prepare_bundle(path, tmp_path/"exports")["folder"] == str(folder)
    other = experiment(tmp_path/"two")
    assert reporting.prepare_bundle(other, tmp_path/"exports")["folder"] != str(folder)
    assert len(list(csv.DictReader((folder/"Report_pump injection.csv").open(encoding="utf-8-sig")))) == 3


def test_legacy_schema_no_added_metadata_needed(tmp_path):
    path = experiment(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM meta WHERE key IN ('duration_s','charge')")
        db.execute("UPDATE meta SET value=? WHERE key='started'", (json.dumps("2026-10-20T00:58:00+00:00"),))
    assert storage.summary(path)["duration"] == 120.
    assert reporting.pump_series(path)["points"][-1][0] == 120.


def test_read_only_old_format_rejection(tmp_path):
    path = experiment(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE meta SET value='\"legacy-v1\"' WHERE key='format'")
    with pytest.raises(ValueError, match="Only Electrolyte Manager 2"):
        reporting.pump_series(path)


def test_bounded_pump_points_keep_final_total_and_gap(tmp_path):
    path = experiment(tmp_path, amounts=[.1]*10000)
    data = reporting.pump_series(path, limit=80)
    assert len(data["points"]) <= 80
    assert data["points"][-1][1] == pytest.approx(1000.)
    assert data["count"] == 10000


def test_runtime_failed_finalization_retries_without_losing_experiment(tmp_path, monkeypatch):
    r = Runtime(tmp_path)
    try:
        r.command("start_recording", name="Retry")
        deadline = time.monotonic()+5
        while not r.snapshot.get("recording") and time.monotonic() < deadline:
            time.sleep(.01)
        original = r.store.finalize
        monkeypatch.setattr(r.store, "finalize", lambda *args: False)
        r.command("finish")
        time.sleep(.2)
        assert r.snapshot["recording"] and r.snapshot["finishing"]
        assert r.snapshot["export_pending"] is None
        monkeypatch.setattr(r.store, "finalize", original)
        deadline = time.monotonic()+6
        while r.snapshot["recording"] and time.monotonic() < deadline:
            time.sleep(.02)
        assert not r.snapshot["recording"]
        assert storage.summary(r.snapshot["export_pending"]["path"])["status"] == "completed"
    finally:
        r.close()
        r.thread.join(10)


def test_monitor_layout_charge_autorange_close_guard_and_auto_bundle(tmp_path, monkeypatch):
    app = W.QApplication.instance() or W.QApplication([])
    messages = []
    monkeypatch.setattr(W.QMessageBox, "information", lambda *args: messages.append(args[2]))
    monkeypatch.setattr(W.QMessageBox, "warning", lambda *args: messages.append(args[2]))
    r = Runtime(tmp_path)
    w = Window(r)
    try:
        w.show()
        w.navigation.setCurrentRow(3)
        w.runtime.engine.settings.metadata["current_a"] = "2"
        w.start_button.click()
        pump(app, lambda: r.snapshot.get("recording"))
        w.close()
        assert messages and r.thread.is_alive() and r.engine.recording
        assert not r.quit.is_set() and not r.priority_stop
        assert "pump" not in w.cards and "charge" in w.cards
        assert not hasattr(w, "event_log")
        w.live_curves["ph"]["filtered"].setData([0, 1, 2], [7., 7.01, 7.])
        w.target_lines[0][0].setValue(100.)
        w.target_lines[0][0].show()
        app.processEvents()
        w.ph_plot.plotItem.vb.autoRange()
        assert w.ph_plot.viewRange()[1][1] < 8.
        w.meta_fields["current_a"].setText("bad A")
        w.apply_settings()
    except Exception as exc:
        from em2.forms import FormError
        assert isinstance(exc, FormError)
        assert w.meta_fields["current_a"].property("invalid")
        assert r.engine.settings.metadata["current_a"] == "2"
    finally:
        if r.engine.recording:
            r.command("finish")
            pump(app, lambda: not r.snapshot.get("recording"))
            w.refresh()
            pump(app, lambda: not w.bundle_active and not w.bundle_queue)
        files = list((tmp_path/"exports").rglob("*.png"))
        assert len(files) == 3
        assert len(list((tmp_path/"exports").rglob("*.csv"))) == 3
        assert all(not G.QImage(str(p)).isNull() for p in files)
        q = r.snapshot["charge"]["coulombs"]
        time.sleep(.1)
        assert r.snapshot["charge"]["coulombs"] == q
        r.close()
        r.thread.join(10)
        w.close()
        app.processEvents()


def test_history_popup_multi_comparison_and_png(tmp_path, monkeypatch):
    app = W.QApplication.instance() or W.QApplication([])
    paths = [experiment(tmp_path/"data", "Alpha"), experiment(tmp_path/"data", "Beta")]
    r = Runtime(tmp_path)
    w = Window(r)
    try:
        w.add_history(paths)
        for i in range(w.history_list.count()):
            w.history_list.item(i).setCheckState(C.Qt.CheckState.Checked)
        w.show_history_pump()
        pump(app, lambda: "3 injections" in w.history_pump_note.text())
        assert len(w.history_pump_plot.listDataItems()) == 2
        assert w.history_pump_plot.listDataItems()[0].yData[-1] == pytest.approx(.6)
        w.align.setCurrentIndex(1)
        w.show_history_pump()
        pump(app, lambda: w.history_pump_plot.listDataItems()[0].xData[0] > 1e9)
        charts.save_png(w.history_pump_plot, tmp_path/"compare.png")
        assert not G.QImage(str(tmp_path/"compare.png")).isNull()
        w.history_popup.close()
        C.QCoreApplication.sendPostedEvents(None, C.QEvent.Type.DeferredDelete)
        assert w.history_popup is None
    finally:
        r.close()
        r.thread.join(10)
        w.close()
        app.processEvents()


def test_export_failure_can_retry_without_duplicate_rows(tmp_path, monkeypatch):
    app = W.QApplication.instance() or W.QApplication([])
    path = experiment(tmp_path/"data")
    notices = []
    monkeypatch.setattr(W.QMessageBox, "warning", lambda *args: notices.append(args[2]))
    r, original = Runtime(tmp_path), charts.render_report
    w = Window(r)
    try:
        monkeypatch.setattr(charts, "render_report", lambda *_: (_ for _ in ()).throw(OSError("disk full: Report_pH.png")))
        w.queue_bundle(path)
        pump(app, lambda: bool(w.failed_exports))
        assert not w.bundle_active and not r.engine.enabled
        assert "Report_pH.png" in notices[-1]
        monkeypatch.setattr(charts, "render_report", original)
        w.retry_exports()
        pump(app, lambda: not w.bundle_active and not w.failed_exports)
        csv_path = next((tmp_path/"exports").rglob("*_pump injection.csv"))
        assert len(list(csv.DictReader(csv_path.open(encoding="utf-8-sig")))) == 3
        assert len(list((tmp_path/"exports").rglob("*.png"))) == 3
    finally:
        r.close()
        r.thread.join(10)
        w.close()
        app.processEvents()


def test_final_result_precedes_completion_and_controls_block_while_finishing(tmp_path):
    from dataclasses import asdict
    r = Runtime(tmp_path)
    try:
        r.dispatch("start_recording", {"name": "Last dose"})
        dose = Dose(.1, 1, 50)
        r.engine.active = dose
        r.dispatch("finish", {})
        assert r.finish_pending
        for action in ("enable", "test", "apply", "allowance"):
            with pytest.raises(ValueError, match="finalization"):
                r.dispatch(action, {})
        time.sleep(.1)
        assert r.store and not r.export_pending
        r.handle("dose_result", asdict(DoseResult(dose.id, True, time.monotonic(), .1, "device_counter", True)))
        deadline = time.monotonic()+5
        while not r.export_pending and time.monotonic() < deadline:
            time.sleep(.02)
        path = r.export_pending["path"]
        with storage.readonly(path) as db:
            assert db.execute("SELECT amount_ml FROM injections").fetchone()[0] == .1
        assert reporting.pump_series(path)["points"][-1][1] == .1
    finally:
        r.close()
        r.thread.join(10)


def test_current_metadata_applies_during_active_dose_without_control_changes(tmp_path):
    r = Runtime(tmp_path)
    try:
        r.dispatch("start_recording", {"name": "Current segments"})
        r.engine.active = Dose(.1, 1, 50)
        payload = r.engine.settings.payload()
        payload["metadata"]["current_a"] = "0.5"
        r.dispatch("apply", {"settings": payload})
        assert r.charge.current == .5
        assert r.engine.pending_settings is not None
        assert not r.engine.stop_requested
        assert r.engine.active is not None
        r.charge.advance(time.monotonic()+2)
        assert r.charge.coulombs >= 1.
        invalid = r.engine.settings.payload()
        invalid["metadata"]["current_a"] = "NaN"
        with pytest.raises(ValueError, match="Current"):
            r.dispatch("apply", {"settings": invalid})
        assert r.charge.current == .5
        r.engine.active = None
    finally:
        r.close()
        r.thread.join(10)


def test_step_geometry_changes_only_at_recorded_completion_time(tmp_path):
    import pyqtgraph as pg
    app = W.QApplication.instance() or W.QApplication([])
    plot = pg.PlotWidget()
    try:
        curve = charts.draw_pump(plot, reporting.pump_series(experiment(tmp_path)))
        geometry = curve.curve.getPath()
        elements = [geometry.elementAt(i) for i in range(geometry.elementCount())]
        def value_at(seconds):
            x = seconds/60.
            return [a.y for a, b in zip(elements, elements[1:])
                    if a.x < x < b.x and a.y == b.y]
        assert value_at(5) == [0.]
        assert value_at(15) == [.1]
        assert value_at(25) == [pytest.approx(.3)]
    finally:
        plot.close()
