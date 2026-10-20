"""Qt chart presentation. All functions here run on the GUI thread."""
import os
from pathlib import Path
import tempfile

import numpy as np
import pyqtgraph as pg
import pyqtgraph.exporters
from PySide6 import QtCore, QtWidgets


def configure(plot):
    plot.setLabel("bottom", "Elapsed time (min)")
    plot.getAxis("bottom").enableAutoSIPrefix(False)
    plot.getAxis("left").enableAutoSIPrefix(False)
    plot.showGrid(x=True, y=True, alpha=.13)
    plot.getViewBox().setAutoVisible(y=True)


def draw_pump(plot, data, color="#167cb5", name="Accounted volume", absolute=False):
    from datetime import datetime
    offset = datetime.fromisoformat(data["meta"]["started"]).timestamp() if absolute else 0.
    transform = (lambda t: t+offset) if absolute else (lambda t: t/60.)
    points = data["points"]
    curve = plot.plot([transform(p[0]) for p in points],
                      [np.nan if p[1] is None else p[1] for p in points],
                      stepMode="right", connect="finite", pen=pg.mkPen(color, width=2), name=name)
    for event in data["refills"]:
        line = pg.InfiniteLine(transform(event["t"]), angle=90,
                               pen=pg.mkPen(color, style=QtCore.Qt.PenStyle.DashLine),
                               label="Refill", labelOpts={"color": color})
        plot.addItem(line, ignoreBounds=True)
    return curve


def save_png(plot, target):
    target = Path(target)
    fd, temporary = tempfile.mkstemp(prefix=".plot-", suffix=".png", dir=target.parent)
    os.close(fd)
    try:
        exporter = pg.exporters.ImageExporter(plot.plotItem)
        exporter.parameters()["width"] = 1800
        exporter.export(temporary)
        os.replace(temporary, target)
    except Exception as exc:
        raise OSError(f"{target}: {exc}") from exc
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def render_report(chart):
    """Render an independent full-experiment chart, never the live viewport."""
    if QtCore.QThread.currentThread() != QtWidgets.QApplication.instance().thread():
        raise RuntimeError("Chart rendering must run on the GUI thread")
    plot = pg.PlotWidget(title=chart["title"], background="white")
    plot.resize(1200, 720)
    try:
        configure(plot)
        plot.setLabel("left", chart["ylabel"])
        plot.addLegend(offset=(10, 8))
        if chart["kind"] == "pump":
            data = chart["pump"]
            draw_pump(plot, data)
            if data["estimates"] or data["unknown"]:
                plot.setTitle(chart["title"]+f" · {data['estimates']} estimated / {data['unknown']} unknown")
        else:
            for field, color in (("raw", "#a9b9c8"), ("filtered", "#167cb5")):
                rows = chart["series"][field]
                plot.plot([r[0]/60 for r in rows], [r[1] for r in rows],
                          connect="finite", pen=pg.mkPen(color, width=1 if field == "raw" else 2),
                          name="Raw" if field == "raw" else "Processed")
            if not any(chart["series"].values()):
                label = pg.TextItem("No data", color="#17324d", anchor=(.5, .5))
                plot.addItem(label)
        plot.plotItem.vb.autoRange()
        # A detached widget has not received a resize event until it is shown.
        plot.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen)
        plot.show()
        QtWidgets.QApplication.processEvents(QtCore.QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
        save_png(plot, chart["target"])
    finally:
        plot.close()
        plot.deleteLater()
    return chart["target"]
