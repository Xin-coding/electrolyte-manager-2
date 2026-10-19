"""300 h equivalent fixture in the real schema; no production simulation mode."""
import json
import math
import sqlite3
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import psutil
from em2.storage import Store, series, summary, compact
from em2.models import Settings
from em2.paths import atomic_json

root = Path(__file__).resolve().parents[2]
folder = root/"exports/scale-validation"
folder.mkdir(parents=True, exist_ok=True)
s = Store.create(folder, "300h_equivalent", Settings().payload())
s.close()
start = time.monotonic()
process = psutil.Process()
peak = process.memory_info().rss
with sqlite3.connect(s.path) as db:
    for kind, count, dt in (("ph", 540000, 2), ("ec", 18000, 60)):
        for offset in range(0, count, 1000):
            rows = []
            for i in range(offset, min(count, offset+1000)):
                value = 7+.01*math.sin(i/50) if kind == "ph" else 100+math.sin(i/10)
                rows.append((i*dt, kind, value, value, 25., "2026-10-19T00:00:00.000+00:00", None if kind == "ph" else "2026-10-19T00:00:00", "benchmark-device-identity", i, "valid", 1))
            db.executemany("INSERT INTO samples(t,kind,raw,filtered,temperature,host_time,device_time,identity,sequence,quality,live) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
            db.commit()
            peak = max(peak, process.memory_info().rss)
    for i in range(3600):
        db.execute("INSERT INTO injections VALUES (?,?,?,?,?,?,?,?,?,?)", (f"bench-{i}", i*300., "experiment", .1, .1, (i+1)*.1, ((i%500)+1)*.1, "device_counter", 1, compact({"benchmark": True, "command_id": f"bench-{i}"})))
        for event in ("intent", "running", "stopped", "accounted", "mixing"):
            db.execute("INSERT INTO events(t,host_time,kind,payload) VALUES (?,?,?,?)", (i*300., "2026-10-19T00:00:00+00:00", event, compact({"id": f"bench-{i}", "benchmark": True})))
    db.commit()
query_start = time.monotonic()
points = {k: len(series(s.path, k)) for k in ("ph", "ec")}
query_seconds = time.monotonic()-query_start
peak = max(peak, process.memory_info().rss)
with sqlite3.connect(s.path) as db:
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
report = {"status": "passed", "ph_samples": 540000, "ec_samples": 18000, "pump_events": 18000, "injections": 3600,
          "database_MiB": s.path.stat().st_size/1024**2, "process_peak_MiB": peak/1024**2,
          "plot_points": points, "query_seconds": query_seconds, "total_seconds": time.monotonic()-start,
          "integrity": integrity, "database": str(s.path)}
assert report["database_MiB"] <= 512 and report["process_peak_MiB"] <= 512
assert integrity == "ok" and max(points.values()) <= 5000
atomic_json(root/"exports/scale-acceptance.json", report)
print(json.dumps(report, indent=2))
