import json
import sys
import threading
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from em2.devices.sensors import SensorWorker, ports
from em2.models import Binding

workers = []
output = []
lock = threading.Lock()
def emit(name, data):
    with lock:
        output.append({"event": name, **data})
    print(json.dumps({"event": name, **data}), flush=True)
try:
    for kind, port in (("ph", "COM8"), ("ec", "COM3")):
        endpoint = next(p for p in ports() if p["port"] == port)
        w = SensorWorker(Binding(kind, port, endpoint["identity"]), emit)
        workers.append(w)
        w.start()
    time.sleep(125)
finally:
    for w in workers:
        w.close()
    for w in workers:
        w.thread.join(4)
    path = Path(__file__).resolve().parents[2]/"exports/sensor-check.json"
    path.write_text(json.dumps(output, indent=2), encoding="utf-8")
