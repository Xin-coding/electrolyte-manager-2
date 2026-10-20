"""Read-only experiment analysis and streaming CSV exports; no device access."""
from contextlib import contextmanager
import csv
import math
import os
from pathlib import Path
import re
import tempfile

from . import storage
from .paths import atomic_json, read_json


def safe_name(name):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name.strip()).strip(". ")[:90] or "Experiment"


def valid_number(value):
    return isinstance(value, (float, int)) and math.isfinite(value)


def pump_rows(db):
    """Keep saved formal totals; test-only files accumulate their test doses."""
    formal = bool(db.execute("SELECT 1 FROM injections WHERE purpose='experiment' LIMIT 1").fetchone())
    cumulative = 0.
    for row in db.execute("SELECT * FROM injections ORDER BY t,id"):
        if (row["purpose"] == "experiment") != formal:
            continue
        item = dict(row)
        item["recorded_cumulative_ml"] = item["cumulative_ml"]
        if formal:
            cumulative = item["cumulative_ml"]
        elif valid_number(cumulative) and valid_number(item["amount_ml"]):
            cumulative += item["amount_ml"]
        else:
            cumulative = None
        item["cumulative_ml"] = cumulative if valid_number(cumulative) else None
        yield item


def pump_series(path, start=0., end=None, limit=5000):
    meta = storage.summary(path)
    end = max(start, meta["duration"] if end is None else min(end, meta["duration"]))
    limit = max(8, limit)
    width = max((end-start)/max(1, (limit-2)//3), 1e-9)
    result, first, last, gap, bucket = [], None, None, None, None
    previous = (0., 0., "baseline")
    count, estimates, unknown = 0, 0, 0

    def flush():
        if first is not None:
            # A gap is retained even if its two neighbours fall in the same bin.
            for point in sorted(set(p for p in (first, gap, last) if p is not None), key=lambda p: p[0]):
                result.append(point)

    with storage.readonly(path) as db:
        for row in pump_rows(db):
            count += 1
            estimates += int("estimate" in (row["source"] or ""))
            unknown += int(row["cumulative_ml"] is None)
            point = (row["t"], row["cumulative_ml"], row["source"] or "unknown")
            if point[0] < start:
                previous = point
                continue
            if point[0] > end:
                continue
            if not result and first is None:
                result.append((start, previous[1], previous[2]))
            b = int((point[0]-start)/width)
            if b != bucket:
                flush()
                bucket, first, gap = b, point, None
            last = point
            if point[1] is None:
                gap = point
        flush()
    if not result:
        result.append((start, previous[1], previous[2]))
    result.append((end, result[-1][1], result[-1][2]))
    refills = [e for e in storage.refill_events(path) if start <= e["t"] <= end]
    return {"meta": meta, "points": result, "refills": refills, "count": count,
            "estimates": estimates, "unknown": unknown}


@contextmanager
def atomic_csv(path):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".csv-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as output:
            yield csv.writer(output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except Exception as exc:
        raise OSError(f"{path}: {exc}") from exc
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def export_pump_csv(path, target):
    fields = ["id", "t", "purpose", "planned_ml", "amount_ml", "cumulative_ml",
              "load_used_ml", "source", "stopped", "recorded_cumulative_ml", "payload"]
    with storage.readonly(path) as db, atomic_csv(target) as writer:
        writer.writerow(fields)
        for row in pump_rows(db):
            writer.writerow([row[k] for k in fields])
    return str(target)


def output_directory(path, destination):
    meta = storage.summary(path)
    destination = Path(destination)/Path(path).stem
    identity = str(meta.get("id") or Path(path).resolve())
    marker = read_json(destination/".source.json", {})
    if destination.exists() and marker.get("identity") != identity:
        # Never overwrite another experiment or a pre-existing user folder.
        import hashlib
        suffix = hashlib.sha256(identity.encode()).hexdigest()[:8]
        destination = destination.with_name(destination.name+"_"+suffix)
        other = read_json(destination/".source.json", {})
        if destination.exists() and other.get("identity") != identity:
            raise ValueError(f"Export directory belongs to another experiment: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    atomic_json(destination/".source.json", {"identity": identity, "source": str(Path(path).resolve())})
    return destination, meta


def prepare_bundle(path, destination, pump_only=False):
    """Full data CSVs and bounded plot data, safe to execute in a worker."""
    if storage.summary(path).get("status") == "recording":
        raise ValueError(f"End the experiment before exporting a full report: {path}")
    folder, meta = output_directory(path, destination)
    name = safe_name(str(meta.get("name", Path(path).stem)))
    charts = []
    if not pump_only:
        with storage.readonly(path) as db:
            for kind, suffix, label in (("ph", "pH", "pH"), ("ec", "cond", "Conductivity (mS/cm)")):
                target = folder/f"{name}_{suffix}.csv"
                cursor = db.execute("SELECT * FROM samples WHERE kind=? ORDER BY t,id", (kind,))
                with atomic_csv(target) as writer:
                    writer.writerow([c[0] for c in cursor.description])
                    writer.writerows(cursor)
                charts.append({"kind": kind, "title": f"{meta.get('name', name)} — {suffix}",
                               "ylabel": label, "target": str(folder/f"{name}_{suffix}.png"),
                               "series": {field: storage.series(path, kind, field) for field in ("raw", "filtered")}})
    export_pump_csv(path, folder/f"{name}_pump injection.csv")
    charts.append({"kind": "pump", "title": f"{meta.get('name', name)} — Pump injection",
                   "ylabel": "Cumulative injection (mL)",
                   "target": str(folder/f"{name}_pump injection.png"), "pump": pump_series(path)})
    return {"path": str(path), "folder": str(folder), "charts": charts}
