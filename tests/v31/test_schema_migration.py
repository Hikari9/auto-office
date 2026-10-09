"""runs.db migration heals existing databases whatever their recorded version.

Regression: the #200 pane-lifecycle columns were added to SHARED_COLUMNS while
SCHEMA_VERSION stayed at 2, and `migrate()` returned early for any v2 file. The
first `_set_dispatch(..., pane_closed_at=...)` after a reviewer ended raised
`no such column: pane_closed_at`, which surfaced as an UNAVAILABLE plan review.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys

from hypothesis import given, settings, strategies as st

from conftest import SRC

PANE_COLUMNS = ("session_id", "resumed_from", "keep_pane", "pane_closed_at")


def _db():
    sys.path.insert(0, str(SRC))
    from office import db, paths
    return db, paths


def _build_old_db(path, version: int, drop: dict[str, set[str]]) -> None:
    """A runs.db created by an older runtime: full DDL, SHARED_COLUMNS minus `drop`."""
    db, _ = _db()
    con = sqlite3.connect(str(path), isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    for stmt in db._statements(db.LEGACY_DDL + db.OFFICE_DDL):
        con.execute(stmt)
    for table, columns in db.SHARED_COLUMNS.items():
        have = db._columns(con, table)
        for col in columns:
            name = col.split()[0]
            if name not in have and name not in drop.get(table, set()):
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
    con.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES('office_schema', ?)", (str(version),))
    con.close()


def test_v2_db_without_pane_columns_heals_on_open(env):
    db, paths = _db()
    path = paths.runs_db()
    path.parent.mkdir(parents=True, exist_ok=True)
    _build_old_db(path, 2, {"dispatches": set(PANE_COLUMNS)})
    raw = sqlite3.connect(str(path))
    raw.execute("INSERT INTO dispatches(id, run_id, role) VALUES('d1', 'r1', 'plan_reviewer')")
    raw.commit()
    assert not set(PANE_COLUMNS) & db._columns(raw, "dispatches")
    raw.close()

    from office import dispatch
    dispatch._set_dispatch("d1", pane_closed_at="2026-01-01T00:00:00Z", keep_pane=0)

    con = db.connect()
    try:
        assert set(PANE_COLUMNS) <= db._columns(con, "dispatches")
        assert db.missing_columns(con) == []
        assert db._schema_version(con) == db.SCHEMA_VERSION
        row = con.execute("SELECT pane_closed_at FROM dispatches WHERE id='d1'").fetchone()
        assert row[0] == "2026-01-01T00:00:00Z"
    finally:
        con.close()


def _every_shared_column():
    db, _ = _db()
    return [(t, c.split()[0]) for t, cols in db.SHARED_COLUMNS.items() for c in cols]


@settings(max_examples=40)  # each example builds and heals a database
@given(dropped=st.sets(st.sampled_from(_every_shared_column()), min_size=1),
       version=st.integers(min_value=2, max_value=_db()[0].SCHEMA_VERSION + 5))
def test_any_shared_columns_missing_at_any_recorded_version_are_added_without_lowering_it(env, dropped, version):
    """Guard: a SHARED_COLUMNS addition reaches existing DBs even without a version bump, whatever
    version the file records and however many columns it lacks. A newer recorded version is kept."""
    db, paths = _db()
    path = paths.runs_db()
    path.parent.mkdir(parents=True, exist_ok=True)
    for leftover in path.parent.glob(path.name + "*"):  # each example starts from its own file
        leftover.unlink()
    drop: dict[str, set[str]] = {}
    for table, column in dropped:
        drop.setdefault(table, set()).add(column)
    _build_old_db(path, version, drop)
    raw = sqlite3.connect(str(path))
    absent = {f"{t}.{c}" for t, c in dropped if c not in db._columns(raw, t)}  # a base-DDL column cannot be absent
    assert set(db.missing_columns(raw)) == absent
    raw.close()
    con = db.connect()
    try:
        assert db.missing_columns(con) == []
        assert db._schema_version(con) == max(version, db.SCHEMA_VERSION)
    finally:
        con.close()


def test_concurrent_opens_of_an_unhealed_db_all_succeed(env):
    db, paths = _db()
    path = paths.runs_db()
    path.parent.mkdir(parents=True, exist_ok=True)
    _build_old_db(path, 2, {"dispatches": set(PANE_COLUMNS), "runs": {"landing_json"}})
    code = ("import sys; sys.path.insert(0, sys.argv[1]); from office import db; "
            "c = db.connect(); assert not db.missing_columns(c); c.close()")
    procs = [subprocess.Popen([sys.executable, "-c", code, str(SRC)], stderr=subprocess.PIPE, text=True)
             for _ in range(6)]
    errors = [p.communicate(timeout=60)[1] for p in procs if p.wait(timeout=60) != 0]
    assert not errors, errors


def test_doctor_reports_schema_columns(env):
    code, out = env.office("doctor")
    assert "schema: all columns present" in out, out
