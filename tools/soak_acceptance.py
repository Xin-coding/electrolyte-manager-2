"""Real acquisition + real Qt UI soak. No pump connection or pump commands."""
import argparse
import json
import sys
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import psutil
from PySide6 import QtCore as C, QtWidgets as W
from em2.runtime import Runtime
from em2.ui import Window
from em2.devices.sensors import ports
from em2.paths import atomic_json, root_dir
from em2 import storage

parser = argparse.ArgumentParser()
parser.add_argument("--hours",type=float,default=24.)
args = parser.parse_args()
app = W.QApplication([])
root = root_dir()
lock = C.QLockFile(str(root/"data/application.lock"))
lock.setStaleLockTime(0)
if not lock.tryLock(100):
    raise RuntimeError("Another EM2 instance is running")
runtime = Runtime(root)
window = Window(runtime)
window.setWindowTitle("Electrolyte Manager 2.0 — acquisition endurance acceptance")
window.navigation.setCurrentRow(3)
window.show()
inventory = ports()
for kind,port in (("ph","COM8"),("ec","COM3")):
    endpoint = next(p for p in inventory if p["port"]==port)
    runtime.command("connect",kind=kind,port=port,identity=endpoint["identity"])
runtime.command("start_recording",name="Acceptance_24h_buffer_acquisition")
process = psutil.Process()
started = time.monotonic()
peak = process.memory_info().rss
last_check = started
max_timer_delay = 0.
report_path = root/"exports/endurance-status.json"
database = ""
def report():
    global peak,last_check,max_timer_delay,database
    now = time.monotonic()
    max_timer_delay = max(max_timer_delay,now-last_check-10.)
    last_check = now
    peak = max(peak,process.memory_info().rss)
    snap = runtime.snapshot
    database = snap.get("path") or database
    data = {"status":"running", "requested_hours":args.hours, "elapsed_hours":(now-started)/3600,
            "pid":process.pid,"memory_MiB":process.memory_info().rss/1024**2,"peak_MiB":peak/1024**2,
            "max_timer_delay_s":max_timer_delay,"database":database,
            "connections":snap.get("connections",{}),"sample_counts":runtime.sequence,
            "storage_error":snap.get("storage_error",""),"pump_connected":False}
    with (root/"exports/endurance-metrics.jsonl").open("a",encoding="utf-8") as f:
        f.write(json.dumps(data)+"\n")
    if now-started>=args.hours*3600:
        adequate = data["sample_counts"].get("ph",0)>=args.hours*1800*.95 and data["sample_counts"].get("ec",0)>=args.hours*60*.95
        data["status"] = "completed" if not data["storage_error"] and adequate and snap.get("recording") and peak<512*1024**2 else "needs_review"
        atomic_json(report_path,data)
        timer.stop()
        window.close()
    else:
        atomic_json(report_path,data)
timer=C.QTimer()
timer.timeout.connect(report)
timer.start(10000)
report()
try:
    code=app.exec()
finally:
    runtime.close()
    runtime.thread.join(15)
    lock.unlock()
    if time.monotonic()-started<args.hours*3600:
        atomic_json(report_path,{"status":"interrupted","elapsed_hours":(time.monotonic()-started)/3600,"database":database,"sample_counts":runtime.sequence})
