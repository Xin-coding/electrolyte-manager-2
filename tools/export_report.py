"""Export an existing EM2 database without starting Runtime or opening ports."""
import argparse
import os
from pathlib import Path
import sys

if sys.platform != "win32":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtWidgets import QApplication
import pyqtgraph as pg
from em2.reporting import prepare_bundle
from em2.charts import render_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--pump-only", action="store_true")
    args = parser.parse_args()
    app = QApplication.instance() or QApplication([])
    pg.setConfigOptions(antialias=True, foreground="#17324d", background="white")
    report = prepare_bundle(args.database, args.destination, args.pump_only)
    for chart in report["charts"]:
        render_report(chart)
        app.processEvents()
    print(report["folder"])


if __name__ == "__main__":
    main()
