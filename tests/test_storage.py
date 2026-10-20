import json
import sqlite3
import time
import pytest
from em2.storage import Store, summary, series, readonly, export_data, export_package, import_package, mark_interrupted
from em2.models import Sample


def test_store_roundtrip_extrema_and_exports(tmp_path):
    s = Store.create(tmp_path, "Test/name", {})
    for i in range(1000):
        value = 100 if i == 511 else i%3
        s.sample(i, Sample("ph", value, i, i), value)
    assert s.event(1000, "durable", {}, critical=True)
    result = series(s.path, "ph", limit=50)
    assert len(result) <= 50
    assert max(x[1] for x in result) == 100
    export_data(s.path, tmp_path/"export")
    export_package(s.path, tmp_path/"test.em2zip")
    imported = import_package(tmp_path/"test.em2zip", tmp_path)
    assert summary(imported)["duration"] == 1000  # Includes the final event.
    s.close()
    assert summary(s.path)["status"] == "completed"
    assert (tmp_path/"export/samples.csv").exists()


def test_recording_recovered_as_interrupted(tmp_path):
    s = Store.create(tmp_path, "Crash", {})
    s.close()
    with sqlite3.connect(s.path) as db:
        db.execute("UPDATE meta SET value='\"recording\"' WHERE key='status'")
    mark_interrupted(tmp_path)
    assert summary(s.path)["status"] == "interrupted"


def test_failed_storage_retries_and_reports_gap(tmp_path):
    s = Store.create(tmp_path, "Failure", {})
    original = s._write
    s._write = lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError("disk full"))
    assert not s.event(1, "intent", {}, critical=True)
    assert s.failure_since is not None
    s._write = original
    assert s.event(2, "recovered", {}, critical=True)
    s.close()
    with readonly(s.path) as db:
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2


def test_other_database_rejected(tmp_path):
    path = tmp_path/"old.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE meta(key,value)")
        db.execute("INSERT INTO meta VALUES ('format','\"old\"')")
    with pytest.raises(ValueError):
        summary(path)
