"""Render bilingual countdown fixtures at configured Windows scale, without serial I/O."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time

parser = argparse.ArgumentParser()
parser.add_argument("output", type=Path)
parser.add_argument("--scale", default="1")
args = parser.parse_args()
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["QT_SCALE_FACTOR"] = args.scale
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import serial
def forbidden(*args, **kwargs):
    raise AssertionError("No hardware access during countdown acceptance")
serial.Serial = forbidden
from PySide6 import QtWidgets as W, QtGui as G
from em2.runtime import Runtime
from em2.ui import Window

app = W.QApplication([])
if not G.QFontDatabase.families():
    for font in ("segoeui.ttf", "segoeuib.ttf", "msyh.ttc", "msyhbd.ttc"):
        G.QFontDatabase.addApplicationFont(str(Path(os.environ.get("WINDIR", "C:/Windows"))/"Fonts"/font))
args.output.mkdir(parents=True, exist_ok=True)
results = []
with tempfile.TemporaryDirectory() as temporary:
    r = Runtime(Path(temporary), autostart=False)
    r.publish()
    w = Window(r); w.timer.stop(); w.show()
    r.engine.recording = True
    w.delay_hours.setValue(3); w.delay_minutes.setValue(15)
    try:
        for size in ((1420,930),(1100,760)):
            w.resize(*size)
            for language in (0,1):
                w.switch_language(language); w.navigation.setCurrentRow(3)
                for state in ("pending", "failed"):
                    r.countdown = {"state":state,"id":1,"deadline":time.monotonic()+3*3600+15*60,
                                   "expected_at":time.time()+3*3600+15*60,"reason":"stale" if state=="failed" else ""}
                    r.publish(); w.refresh()
                    for _ in range(4): app.processEvents()
                    name=f"countdown-{args.scale}-{size[0]}-{'zh' if language==0 else 'en'}-{state}.png"
                    w.grab().save(str(args.output/name))
                    results.append({"file":name,"state":state,"language":w.language,"size":[w.width(),w.height()],
                                    "text":w.countdown_status.text(),"button_height":w.schedule_button.height()})
        r.countdown={"state":"idle"}; r.engine.recording=False; r.publish()
    finally:
        w.close(); app.processEvents()
        for handler in list(r.log.handlers): handler.close(); r.log.removeHandler(handler)
(args.output/f"countdown-ui-{args.scale}.json").write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding="utf-8")
print(f"{len(results)} countdown views at scale {args.scale}; no physical serial access")
