"""Capture UI fixtures without opening any physical serial port."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import serial
def forbidden(*args, **kwargs):
    raise AssertionError("UI acceptance must not access hardware")
serial.Serial = forbidden
from PySide6 import QtWidgets as W, QtCore as C, QtGui as G
from em2.runtime import Runtime
from em2.ui import Window

parser = argparse.ArgumentParser()
parser.add_argument("output", type=Path)
parser.add_argument("--scale", default="1")
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
os.environ["QT_SCALE_FACTOR"] = args.scale
app = W.QApplication([])
# Windows' offscreen plugin has an empty font database; register local fonts
# for this capture process only. The normal Windows application uses system fonts.
if not G.QFontDatabase.families():
    for font in ("segoeui.ttf", "segoeuib.ttf", "msyh.ttc", "msyhbd.ttc"):
        G.QFontDatabase.addApplicationFont(str(Path(os.environ.get("WINDIR", "C:/Windows"))/"Fonts"/font))
results = []
with tempfile.TemporaryDirectory() as temporary:
    runtime = Runtime(Path(temporary))
    window = Window(runtime)
    try:
        window.timer.stop()
        window.show()
        runtime.select_conductivity("m80")
        runtime.publish()
        for size in ((1420, 930), (1100, 760)):
            window.resize(*size)
            for language in (0, 1):
                window.switch_language(language)
                for page in (0, 3, 4):
                    window.navigation.setCurrentRow(page)
                    window.refresh()
                    for key, value in {"ph": "13.662", "ec": "71.854", "temp": "23.00", "used": "0.000", "remaining": "45.000", "charge": "37.853"}.items():
                        window.cards[key].setText(value)
                    window.charge_unit.setText("kC")
                    for kind in ("ph", "ec"):
                        window.live_curves[kind]["filtered"].setData([0, 1, 2, 3], [7, 7.01, 7.005, 7.02])
                    for _ in range(4):
                        app.processEvents(); time.sleep(.02)
                    filename = f"ui-{args.scale}-{size[0]}-{'zh' if language == 0 else 'en'}-{page}.png"
                    window.grab().save(str(args.output/filename))
                    assert window.cards["charge"].font().pointSizeF() == window.cards["ph"].font().pointSizeF()
                    results.append({"file": filename, "width": window.width(), "height": window.height(), "charge_font_pt": window.cards["charge"].font().pointSizeF()})
        window.ph_plot.range_control.edit_range()
        app.processEvents()
        window.ph_plot.range_control.dialog.grab().save(str(args.output/f"range-dialog-{args.scale}.png"))
        window.ph_plot.range_control.dialog.close()
    finally:
        runtime.close(); runtime.thread.join(10)
        window.close(); app.processEvents()
(args.output/f"ui-check-{args.scale}.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
print(f"Captured {len(results)} views at scale {args.scale}; hardware access blocked")
