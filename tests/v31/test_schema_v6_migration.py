"""Schema v6 adds the scheduler and command-receipt tables additively."""
from __future__ import annotations

import re
import sqlite3
import sys

from conftest import SRC

NEW_TABLES = ("commands", "sched_items", "sched_state")


def _db():
    sys.path.insert(0, str(SRC))
    from office import db
    return db


def _tables(con) -> set[str]:
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _snapshot(con) -> list:
    return sorted(tuple(r) for r in con.execute("SELECT type, name, sql FROM sqlite_master"))


def _remigrate_twice(db, con) -> None:
    """Force the full DDL pass (not the fast path) twice."""
    for _ in range(2):
        con.execute("UPDATE schema_meta SET value='5' WHERE key='office_schema'")
        db.migrate(con)


def _v5_ddl(db) -> str:
    """The DDL a v5 runtime shipped: this one minus the v6 tables."""
    return "".join(f"{stmt};" for stmt in db._statements(db.LEGACY_DDL + db.OFFICE_DDL)
                   if not any(f"EXISTS {t}(" in stmt for t in NEW_TABLES))


def _build_v5(db, path) -> None:
    con = sqlite3.connect(str(path), isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    for stmt in db._statements(_v5_ddl(db)):
        con.execute(stmt)
    for table, columns in db.SHARED_COLUMNS.items():
        for col in columns:
            if col.split()[0] not in db._columns(con, table):
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
    con.execute(db.TRIGGERS)
    con.execute("INSERT INTO schema_meta(key, value) VALUES('office_schema', '5')")
    con.execute("INSERT INTO runs(id, phase) VALUES('r-old', 'executing')")
    con.close()


def test_new_tables_are_create_if_not_exists_only():
    db = _db()
    assert db.SCHEMA_VERSION == 6
    for table in NEW_TABLES:
        assert re.search(rf"CREATE TABLE IF NOT EXISTS {table}\(", db.OFFICE_DDL)
    assert "DROP " not in db.OFFICE_DDL.upper()


def test_v5_database_upgrades_and_keeps_rows(tmp_path):
    db = _db()
    path = tmp_path / "runs.db"
    _build_v5(db, path)
    con = db.connect(path)
    try:
        assert set(NEW_TABLES) <= _tables(con)
        assert db._schema_version(con) == 6
        assert con.execute("SELECT phase FROM runs WHERE id='r-old'").fetchone()[0] == "executing"
        before = _snapshot(con)
        _remigrate_twice(db, con)
        assert _snapshot(con) == before
        assert db._schema_version(con) == 6
    finally:
        con.close()


def test_legacy_only_database_upgrades(tmp_path):
    db = _db()
    path = tmp_path / "runs.db"
    raw = sqlite3.connect(str(path), isolation_level=None)
    for stmt in db._statements(db.LEGACY_DDL):
        raw.execute(stmt)
    raw.execute("INSERT INTO runs(id, status) VALUES('legacy', 'running')")
    raw.close()
    con = db.connect(path)
    try:
        assert set(NEW_TABLES) <= _tables(con)
        assert db.missing_columns(con) == []
        assert con.execute("SELECT status FROM runs WHERE id='legacy'").fetchone()[0] == "running"
        before = _snapshot(con)
        _remigrate_twice(db, con)
        assert _snapshot(con) == before
    finally:
        con.close()


def test_older_runtime_drift_check_passes_on_a_v6_file(tmp_path, monkeypatch):
    """A v5 runtime checks only its own tables; a v6 file is a superset, so it neither
    re-migrates nor lowers the stamp."""
    db = _db()
    path = tmp_path / "runs.db"
    db.connect(path).close()
    v5_tables = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", _v5_ddl(db))
    assert not set(NEW_TABLES) & set(v5_tables)
    monkeypatch.setattr(db, "_TABLES", v5_tables)
    monkeypatch.setattr(db, "SCHEMA_VERSION", 5)
    old = db.connect(path)
    try:
        assert not db._drifted(old)
        assert db._schema_version(old) == 6
    finally:
        old.close()
