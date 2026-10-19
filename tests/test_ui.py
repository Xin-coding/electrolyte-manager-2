import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import time
import pytest
from PySide6 import QtWidgets as W, QtCore as C
from em2.runtime import Runtime
from em2.ui import Window
from em2.storage import Store
from em2.models import Sample


def test_five_pages_language_record_history_and_plot_export(tmp_path):
    app = W.QApplication.instance() or W.QApplication([])
    r = Runtime(tmp_path)
    w = Window(r)
    try:
        assert w.pages.count() == 5
        for index in range(5):
            w.navigation.setCurrentRow(index)
            app.processEvents()
        w.switch_language(1)
        assert w.navigation.item(0).text() == "Connections"
        w.switch_language(0)
        assert w.navigation.item(3).text() == "实时监控"
        w.experiment_name.setText("UI bench")
        w.start_button.click()
        end = time.monotonic()+3
        while not r.snapshot.get("recording") and time.monotonic()<end:
            app.processEvents()
            time.sleep(.02)
        assert r.snapshot["recording"]
        path = r.snapshot["path"]
        r.emit("sample", {"kind":"ph","value":7.,"mono":time.monotonic(),"temperature":25.})
        time.sleep(.2)
        w.refresh()
        assert "UI bench" in w.filename.text()
        r.command("finish")
        end = time.monotonic()+3
        while r.snapshot.get("recording") and time.monotonic()<end:
            app.processEvents()
            time.sleep(.02)
        from em2 import storage
        data = [(storage.summary(path), [storage.series(path,"ph"),storage.series(path,"ec")], [])]
        w.draw_history(data, False)
        w.draw_history(data, True)
        import pyqtgraph.exporters
        pg = pyqtgraph.exporters.ImageExporter(w.history_plots[0].plotItem)
        pg.export(str(tmp_path/"chart.png"))
        assert (tmp_path/"chart.png").stat().st_size > 100
        w.show_pump()
        w.popup.close()
        C.QCoreApplication.sendPostedEvents(None,C.QEvent.Type.DeferredDelete)
        w.switch_language(1)
        assert w.popup is None
    finally:
        r.close()
        r.thread.join(10)
        w.close()
        app.processEvents()
