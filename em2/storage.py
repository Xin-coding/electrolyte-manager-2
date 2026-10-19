"""Bounded asynchronous SQLite storage and streaming, read-only analysis."""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager, closing
import csv
from dataclasses import asdict
import json
import math
from pathlib import Path
import queue
import re
import sqlite3
import threading
import time
from datetime import datetime
from uuid import uuid4
import zipfile
import tempfile

from .models import utc_now

FORMAT = "electrolyte-manager-2/sqlite/1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS samples(
 id INTEGER PRIMARY KEY, t REAL NOT NULL, kind TEXT NOT NULL, raw REAL, filtered REAL,
 temperature REAL, host_time TEXT NOT NULL, device_time TEXT, identity TEXT, sequence INTEGER,
 quality TEXT, live INTEGER);
CREATE INDEX IF NOT EXISTS samples_kind_t ON samples(kind,t);
CREATE TABLE IF NOT EXISTS events(
 id INTEGER PRIMARY KEY,t REAL NOT NULL,host_time TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_t ON events(t);
CREATE TABLE IF NOT EXISTS injections(
 id TEXT PRIMARY KEY,t REAL NOT NULL,purpose TEXT,planned_ml REAL,amount_ml REAL,cumulative_ml REAL,
 load_used_ml REAL,source TEXT,stopped INTEGER,payload TEXT);
CREATE TABLE IF NOT EXISTS settings(version INTEGER PRIMARY KEY,t REAL NOT NULL,payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS injections_t ON injections(t);
CREATE INDEX IF NOT EXISTS events_kind_t ON events(kind,t);
"""


def write_injection(db, data):
    d, r = data["dose"], data["result"]
    # Retries after a lost commit acknowledgement must not count twice.
    existing = db.execute("SELECT payload FROM injections WHERE id=?", (d["id"],)).fetchone()
    if existing:
        if json.loads(existing[0]) != data:
            raise ValueError("Conflicting saved result for injection "+d["id"])
        return
    db.execute("INSERT INTO injections VALUES (?,?,?,?,?,?,?,?,?,?)", (d["id"], data["t"], d["purpose"], d["volume_ml"], r["amount_ml"], data["cumulative_ml"], data["load"]["used_ml"], r["source"], int(r["stopped"]), compact(data)))


def recover_injection(path, data):
    """Complete a locally journaled result after a process exit; no device I/O."""
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+"?mode=rw", uri=True, timeout=.5)) as db, db:
        row = db.execute("SELECT value FROM meta WHERE key='format'").fetchone()
        if not row or json.loads(row[0]) != FORMAT:
            raise ValueError("Invalid pending-result experiment file")
        db.execute("PRAGMA synchronous=FULL")
        write_injection(db, data)


def compact(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


@contextmanager
def readonly(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro", uri=True, timeout=2)
    connection.execute("PRAGMA query_only=ON")
    connection.row_factory = sqlite3.Row
    fmt = connection.execute("SELECT value FROM meta WHERE key='format'").fetchone()
    if not fmt or json.loads(fmt[0]) != FORMAT:
        connection.close()
        raise ValueError("Only Electrolyte Manager 2 data is supported")
    try:
        yield connection
    finally:
        connection.close()


class Store:
    def __init__(self, path, metadata, clock=time.monotonic, queue_size=10000):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.queue = queue.Queue(maxsize=queue_size)
        self.stop_event = threading.Event()
        self.failure_since = None
        self.error = ""
        self.last_saved = None
        self.dropped = 0
        self.gap = None
        self.closed = False
        self.close_deadline = float("inf")
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.executescript(SCHEMA)
            data = {"format": FORMAT, "id": uuid4().hex, "started": utc_now(), "status": "recording", **metadata}
            db.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", [(k, compact(v)) for k, v in data.items()])
        self.thread = threading.Thread(target=self._run, name="em2-storage", daemon=True)
        self.thread.start()

    @classmethod
    def create(cls, directory, name, settings, **kwargs):
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name.strip()).strip(". ")[:90] or "Experiment"
        base = f"{safe}_{datetime.now().astimezone():%Y-%m-%d_%H-%M-%S}"
        path = Path(directory)/(base+".sqlite")
        i = 2
        while path.exists():
            path = Path(directory)/(base+f"_{i:02}.sqlite")
            i += 1
        return cls(path, {"name": name, "initial_settings": settings, "timezone": str(datetime.now().astimezone().tzinfo)}, **kwargs)

    @property
    def failure_age(self):
        return 0. if self.failure_since is None else max(0., self.clock()-self.failure_since)

    def put(self, kind, data, critical=False):
        if self.closed:
            raise RuntimeError("Data store is closed")
        ack = threading.Event() if critical else None
        item = {"kind": kind, "data": data, "time": self.clock(), "ack": ack, "ok": False}
        try:
            self.queue.put_nowait(item)
        except queue.Full:
            self._failure("Storage queue is full")
            self._drop(item)
            return False
        if critical:
            return ack.wait(3.) and item["ok"]
        return True

    def event(self, t, kind, data, critical=False):
        return self.put("event", (t, utc_now(), kind, compact(data)), critical)

    def sample(self, t, sample, filtered):
        return self.put("sample", (t, sample.kind, sample.value, filtered, sample.temperature,
                                  sample.host_time, sample.device_time, sample.identity,
                                  sample.sequence, sample.quality, int(sample.live)))

    def _failure(self, error):
        if self.failure_since is None:
            self.failure_since = self.clock()
        self.error = str(error)

    def _drop(self, item):
        self.dropped += 1
        data = item["data"]
        t = data[0] if isinstance(data, (list, tuple)) and data and isinstance(data[0], (int, float)) else None
        if self.gap is None:
            self.gap = {"count": 0, "first_t": t, "last_t": t}
        self.gap["count"] += 1
        self.gap["last_t"] = t
        if item["ack"]:
            item["ack"].set()

    def _write(self, db, item):
        kind, data = item["kind"], item["data"]
        if kind == "sample":
            db.execute("INSERT INTO samples(t,kind,raw,filtered,temperature,host_time,device_time,identity,sequence,quality,live) VALUES (?,?,?,?,?,?,?,?,?,?,?)", data)
        elif kind == "event":
            db.execute("INSERT INTO events(t,host_time,kind,payload) VALUES (?,?,?,?)", data)
        elif kind == "meta":
            db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (data[0], compact(data[1])))
        elif kind == "settings":
            db.execute("INSERT OR REPLACE INTO settings VALUES (?,?,?)", (data[0], data[1], compact(data[2])))
        elif kind == "injection":
            write_injection(db, data)

    def _run(self):
        db = sqlite3.connect(self.path, timeout=.5)
        db.execute("PRAGMA synchronous=FULL")
        pending = deque()
        last_flush = self.clock()
        last_checkpoint = self.clock()
        try:
            while not self.stop_event.is_set() or pending or not self.queue.empty():
                if self.clock() > self.close_deadline:
                    break
                try:
                    pending.append(self.queue.get(timeout=.05))
                except queue.Empty:
                    pass
                for _ in range(min(self.queue.qsize(), 1000)):
                    try:
                        pending.append(self.queue.get_nowait())
                    except queue.Empty:
                        break
                now = self.clock()
                while pending and (now-pending[0]["time"] > 60 or len(pending) > 10000):
                    self._drop(pending.popleft())
                urgent = any(i["ack"] for i in pending)
                if pending and (urgent or now-last_flush >= 1 or len(pending) >= 1000 or self.stop_event.is_set()):
                    try:
                        db.execute("BEGIN")
                        for item in pending:
                            self._write(db, item)
                        if self.gap:
                            db.execute("INSERT INTO events(t,host_time,kind,payload) VALUES (?,?,?,?)", (self.gap["last_t"] or 0, utc_now(), "storage_gap", compact(self.gap)))
                        db.commit()
                        for item in pending:
                            item["ok"] = True
                            if item["ack"]:
                                item["ack"].set()
                        pending.clear()
                        self.gap = None
                        self.failure_since = None
                        self.error = ""
                        self.last_saved = utc_now()
                    except Exception as exc:
                        db.rollback()
                        self._failure(exc)
                        for item in pending:
                            if item["ack"]:
                                item["ack"].set()
                        time.sleep(.2)
                    last_flush = self.clock()
                if now-last_checkpoint > 300 and not self.failure_since:
                    try:
                        db.execute("PRAGMA wal_checkpoint(PASSIVE)")
                    except sqlite3.Error:
                        pass
                    last_checkpoint = now
        finally:
            for item in pending:
                self._drop(item)
            try:
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            db.close()

    def close(self, status="completed"):
        if self.closed:
            return
        self.put("meta", ("status", status))
        self.put("meta", ("ended", utc_now()))
        self.closed = True
        self.close_deadline = self.clock()+4
        self.stop_event.set()
        self.thread.join(timeout=5)


def mark_interrupted(directory):
    for path in Path(directory).glob("*.sqlite"):
        try:
            with readonly(path) as db:
                status = json.loads(db.execute("SELECT value FROM meta WHERE key='status'").fetchone()[0])
            if status == "recording":
                with closing(sqlite3.connect(path)) as db, db:
                    db.execute("UPDATE meta SET value=? WHERE key='status'", (compact("interrupted"),))
                    db.execute("INSERT OR REPLACE INTO meta VALUES ('recovered_at',?)", (compact(utc_now()),))
        except (sqlite3.Error, ValueError):
            continue


def summary(path):
    with readonly(path) as db:
        meta = {r[0]: json.loads(r[1]) for r in db.execute("SELECT key,value FROM meta")}
        meta["duration"] = db.execute("SELECT COALESCE(MAX(t),0) FROM samples").fetchone()[0]
        meta["path"] = str(path)
        return meta


def series(path, kind, field="filtered", start=0., end=None, limit=5000):
    if field not in {"raw", "filtered"} or kind not in {"ph", "ec"}:
        raise ValueError("Unsupported series")
    with readonly(path) as db:
        if end is None:
            end = db.execute("SELECT COALESCE(MAX(t),0) FROM samples WHERE kind=?", (kind,)).fetchone()[0]
        bins = max(1, limit//2)
        width = max((end-start)/bins, 1e-9)
        result, bucket, low, high = [], None, None, None
        def flush():
            if low is not None:
                result.extend(sorted({low, high}, key=lambda row: row[0]))
        # Cursor streaming keeps memory bounded even for multi-million-point runs.
        for row in db.execute(f"SELECT t,{field},temperature FROM samples WHERE kind=? AND t>=? AND t<=? AND {field} IS NOT NULL ORDER BY t", (kind, start, end)):
            point = tuple(row)
            b = min(bins-1, int((point[0]-start)/width))
            if b != bucket:
                flush()
                bucket, low, high = b, point, point
            else:
                if point[1] < low[1]:
                    low = point
                if point[1] > high[1]:
                    high = point
        flush()
        return result


def injection_series(path, limit=5000):
    with readonly(path) as db:
        end = db.execute("SELECT COALESCE(MAX(t),0) FROM injections").fetchone()[0]
        bins = max(1, limit//2)
        width = max(end/bins, 1e-9)
        result, bucket, first, last, tests = [], None, None, None, 0.
        for row in db.execute("SELECT t,cumulative_ml,source,purpose,amount_ml FROM injections ORDER BY t"):
            tests += row[4] if row[3] != "experiment" and row[4] is not None else 0.
            point = (row[0], row[1] if row[3] == "experiment" else tests, row[2], row[3])
            b = min(bins-1, int(row[0]/width))
            if b != bucket:
                if first is not None:
                    result.extend([first] if first == last else [first, last])
                bucket, first = b, point
            last = point
        if first is not None:
            result.extend([first] if first == last else [first, last])
        return result


def refill_events(path):
    with readonly(path) as db:
        return [dict(r) for r in db.execute("SELECT t,kind,payload FROM events WHERE kind='refill' ORDER BY t DESC LIMIT 5000")]


def recent_events(path, limit=200):
    with readonly(path) as db:
        return [dict(r) for r in db.execute("SELECT t,host_time,kind,payload FROM events ORDER BY id DESC LIMIT ?", (limit,))]


def export_data(path, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with readonly(path) as db:
        for table in ("samples", "injections"):
            cursor = db.execute(f"SELECT * FROM {table} ORDER BY t")
            with (destination/f"{table}.csv").open("w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow([x[0] for x in cursor.description])
                writer.writerows(cursor)
        # JSONL is intentionally streamable; settings are small JSON snapshots.
        with (destination/"events.jsonl").open("w", encoding="utf-8") as f:
            for row in db.execute("SELECT * FROM events ORDER BY id"):
                item = dict(row)
                item["payload"] = json.loads(item["payload"])
                f.write(compact(item)+"\n")
        settings = [dict(r) for r in db.execute("SELECT * FROM settings ORDER BY version")]
        for item in settings:
            item["payload"] = json.loads(item["payload"])
        (destination/"settings.json").write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
        (destination/"metadata.json").write_text(json.dumps({r[0]: json.loads(r[1]) for r in db.execute("SELECT * FROM meta")}, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(destination)


def export_package(path, destination):
    # SQLite backup includes committed WAL records without copying a live WAL file.
    with tempfile.TemporaryDirectory() as folder:
        snapshot = Path(folder)/"experiment.sqlite"
        with readonly(path) as source, closing(sqlite3.connect(snapshot)) as target:
            source.backup(target, pages=1000, sleep=.01)
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(snapshot, "experiment.sqlite")
            z.writestr("manifest.json", compact({"format": FORMAT, "exported": utc_now()}))
    return str(destination)


def import_package(path, directory):
    with zipfile.ZipFile(path) as z:
        info = z.getinfo("experiment.sqlite")
        if info.file_size > 4*1024**3:
            raise ValueError("Experiment package exceeds 4 GiB")
        manifest = json.loads(z.read("manifest.json"))
        if manifest.get("format") != FORMAT:
            raise ValueError("Only Electrolyte Manager 2 data is supported")
        output = Path(directory)/(Path(path).stem+"_"+uuid4().hex[:8]+".sqlite")
        with z.open(info) as source, output.open("xb") as target:
            import shutil
            shutil.copyfileobj(source, target, 1024*1024)
        try:
            summary(output)
        except Exception:
            output.unlink()
            raise
    return str(output)
