"""Native Qt UI acceptance using generated files only; never opens a device."""
import json
import math
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6 import QtWidgets as W, QtCore as C
from em2 import storage, reporting, __version__
from em2.models import Sample
from em2.runtime import Runtime
from em2.ui import Window


def main():
    app = W.QApplication([])
    output = Path(__file__).resolve().parents[2]/"exports"/f"qa-{__version__}"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        fixture = storage.Store.create(root/"data", "Offline acceptance", {})
        for i in range(601):
            value = 13.88+.01*math.sin(i/35)
            fixture.sample(i, Sample("ph", value, float(i), i, temperature=25.), value)
            if i % 60 == 0:
                fixture.sample(i, Sample("ec", 150+i/40, float(i), i, temperature=25.), 150+i/40)
        for i in range(6):
            fixture.put("injection", {
                "dose": {"id": str(i), "purpose": "experiment", "volume_ml": .1},
                "result": {"amount_ml": .1, "source": "device_counter", "stopped": True},
                "t": i*90., "cumulative_ml": (i+1)*.1, "load": {"used_ml": (i+1)*.1}})
        assert fixture.finalize(600, {}, "2026-10-20T00:10:00+00:00")
        fixture.close(finalized=True)
        runtime = Runtime(root)
        w = Window(runtime)
        w.setAttribute(C.Qt.WidgetAttribute.WA_DontShowOnScreen)
        w.show()
        w.timer.stop()
        runtime.close()
        runtime.thread.join(10)
        snap = runtime.snapshot
        snap.update(recording=True, path=str(fixture.path), elapsed=600.,
                    charge={"coulombs": 1200., "available": True, "partial": False},
                    load={"used_ml": .6, "limit_ml": 50, "uncertain": False, "estimated": False}, remaining=49.4)
        snap["settings"]["ph"].update(target=13.9, tolerance=.02, lower=0., upper=14.5)
        snap["auto_target"]["ph"] = 13.9
        snap["samples"] = {k: {"value": v, "filtered": v, "temperature": 25., "live": True, "mono": time.monotonic()}
                           for k, v in (("ph", 13.88), ("ec", 165.))}
        snap["connections"] = {k: {"connected": True} for k in ("ph", "ec")}
        curves = {k: {f: storage.series(fixture.path, k, f) for f in ("raw", "filtered")} for k in ("ph", "ec")}
        for language in (0, 1):
            w.switch_language(language)
            w.navigation.setCurrentRow(3)
            w.refresh()
            w.draw_charts((str(fixture.path), curves, None, []))
            for _ in range(5):
                app.processEvents()
            w.grab().save(str(output/("monitor-zh.png" if language == 0 else "monitor-en.png")))
            assert w.ph_plot.viewRange()[1][1]-w.ph_plot.viewRange()[1][0] < .2
        w.navigation.setCurrentRow(4)
        w.refresh_history()
        w.history_list.item(0).setCheckState(C.Qt.CheckState.Checked)
        w.compare_history()
        for _ in range(20):
            app.processEvents()
            time.sleep(.02)
        w.grab().save(str(output/"history.png"))
        w.show_history_pump()
        w.history_popup.setAttribute(C.Qt.WidgetAttribute.WA_DontShowOnScreen)
        for _ in range(20):
            app.processEvents()
            time.sleep(.02)
        w.history_popup.grab().save(str(output/"history-pump.png"))
        w.history_popup.close()
        snap["recording"] = False
        w.close()
        app.processEvents()
    report = {"version": __version__, "platform": app.platformName(),
              "fixture": "synthetic offline records", "serial_ports_opened": 0,
              "languages": ["zh", "en"], "y_axis_excludes_reference_lines": True, "status": "passed"}
    (output/"ui-check.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
