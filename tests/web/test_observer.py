"""The observer reads runs.db without ever writing to it."""
from __future__ import annotations

import sqlite3
import threading

import pytest

from office import db
from office.web import observer, synthetic
from office.web.observer import Observer, open_readonly, read_snapshot

WRITE_PREFIXES = ("INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "ALTER", "DROP")


def _probe_columns(con):
    return {
        "cursors": con.execute("SELECT * FROM cursors ORDER BY run_id, consumer").fetchall(),
        "events": con.execute("SELECT COUNT(*), MAX(seq) FROM events").fetchone(),
        "schema_meta": con.execute("SELECT * FROM schema_meta ORDER BY key").fetchall(),
        "runs": con.execute("SELECT id, updated_at FROM runs ORDER BY id").fetchall(),
        "dispatches": con.execute("SELECT id, last_seen_at FROM dispatches ORDER BY id").fetchall(),
    }


def test_open_readonly_uses_ro_uri_and_query_only(mini, monkeypatch):
    seen = {}
    real = sqlite3.connect

    def spy(target, *args, **kwargs):
        seen["target"], seen["uri"] = target, kwargs.get("uri")
        return real(target, *args, **kwargs)

    monkeypatch.setattr(observer.sqlite3, "connect", spy)
    con = open_readonly(mini["path"])
    try:
        assert seen["target"].startswith("file:") and seen["target"].endswith("?mode=ro")
        assert seen["uri"] is True
        assert con.execute("PRAGMA query_only").fetchone()[0] == 1
    finally:
        con.close()


def test_never_uses_the_writer_path(mini, monkeypatch, make_observer):
    def boom(*_a, **_k):
        raise AssertionError("observer must not call db.connect or db.migrate")

    monkeypatch.setattr(db, "connect", boom)
    monkeypatch.setattr(db, "migrate", boom)
    obs = make_observer(mini["path"])
    assert obs.workspace()["runs"]
    assert obs.run("R1")["run_id"] == "R1"


def test_missing_database_is_refused_without_creating_directories(tmp_path):
    target = tmp_path / "nope" / "deeper" / "runs.db"
    with pytest.raises(FileNotFoundError):
        open_readonly(target)
    assert not (tmp_path / "nope").exists()


def test_repeated_projection_leaves_the_database_unchanged(mini, make_observer, dump_hash):
    before_hash = dump_hash(mini["path"])
    before = _probe_columns(mini["con"])
    obs = make_observer(mini["path"], runs_dir=mini["runs_dir"])
    statements = []
    obs.con.set_trace_callback(statements.append)
    for _ in range(5):
        obs.workspace()
        obs.run("R1")
        obs.activity("R1")
    assert dump_hash(mini["path"]) == before_hash
    assert _probe_columns(mini["con"]) == before
    assert not [s for s in statements if s.lstrip().upper().startswith(WRITE_PREFIXES)]


@pytest.mark.parametrize("sql", [
    "INSERT INTO cursors(run_id, consumer, last_seq, updated_at) VALUES('R1','web',1,'t')",
    "UPDATE runs SET updated_at='x'",
    "DELETE FROM events",
    "CREATE TABLE web_cache(x)",
    "ALTER TABLE runs ADD COLUMN web TEXT",
])
def test_any_write_through_the_observer_connection_raises(mini, make_observer, sql):
    obs = make_observer(mini["path"])
    with pytest.raises(sqlite3.OperationalError):
        obs.con.execute(sql)
    # The file itself is opened read-only too: lifting query_only does not make it writable.
    obs.con.execute("PRAGMA query_only=OFF")
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        obs.con.execute(sql)


def _v1_database(path):
    """A runs.db as an early 3.1 runtime left it: no landing, PR, stall, binding, gate or route tables/columns."""
    con = sqlite3.connect(path)
    con.executescript(db.LEGACY_DDL)
    con.executescript("""
        ALTER TABLE runs ADD COLUMN office_version TEXT; ALTER TABLE runs ADD COLUMN git_common_dir TEXT;
        ALTER TABLE runs ADD COLUMN goal TEXT; ALTER TABLE runs ADD COLUMN phase TEXT;
        ALTER TABLE runs ADD COLUMN gear TEXT; ALTER TABLE runs ADD COLUMN updated_at TEXT;
        ALTER TABLE dispatches ADD COLUMN task_id TEXT; ALTER TABLE dispatches ADD COLUMN kind TEXT;
        ALTER TABLE dispatches ADD COLUMN status TEXT; ALTER TABLE dispatches ADD COLUMN harness TEXT;
        ALTER TABLE dispatches ADD COLUMN model TEXT; ALTER TABLE dispatches ADD COLUMN effort TEXT;
        CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO schema_meta VALUES('office_schema', '1');
        CREATE TABLE tasks(run_id TEXT NOT NULL, id TEXT NOT NULL, title TEXT NOT NULL, role TEXT NOT NULL,
            status TEXT NOT NULL, depends_json TEXT, PRIMARY KEY(run_id, id));
        INSERT INTO runs(id, office_version, git_common_dir, goal, phase, gear, created_at)
            VALUES('OLD', '3.1.0', '/src/old/.git', 'old goal', 'executing', 'S', '2026-01-01');
        INSERT INTO runs(id, status, created_at) VALUES('V3', 'running', '2025-12-01');
        INSERT INTO tasks VALUES('OLD', 'T1', 'one', 'executor', 'accepted', '[]');
        INSERT INTO tasks VALUES('OLD', 'T2', 'two', 'executor', 'running', '[]');
        INSERT INTO dispatches(id, run_id, role, task_id, kind, status, harness, model, effort, started_at)
            VALUES('DX', 'OLD', 'executor', 'T2', 'executor', 'running', 'codex', 'm', 'high', '2026-01-01');
    """)
    con.commit()
    con.close()


def test_v1_schema_reads_without_migration_and_reports_missing_capabilities(tmp_path, make_observer, dump_hash):
    path = tmp_path / "runs.db"
    _v1_database(path)
    before = dump_hash(path)
    obs = make_observer(path)
    statements = []
    obs.con.set_trace_callback(statements.append)
    ws = obs.workspace()
    run = obs.run("OLD")
    assert dump_hash(path) == before
    assert not [s for s in statements if s.lstrip().upper().startswith(WRITE_PREFIXES)]
    caps = run["capabilities"]
    for missing in ("issue_link", "pr_links", "gates", "route_audit", "session_bindings", "quota_wait", "activity",
                    "idle_tracking"):
        assert caps[missing] is False, missing
    assert caps["tasks"] is True and caps["legacy_runtime"] is False
    assert run["progress"]["accepted_weight"] == 1 and run["progress"]["total_weight"] == 2
    assert run["issue"] is None and run["prs"] == [] and run["owner"] == {"kind": "none"}
    assert run["tasks"][0]["route"]["available"] is False
    assert run["agents"]["columns"]["executors"][0]["state"]["quota_wait"]["active"] is False
    assert obs.activity("OLD")["available"] is False
    legacy = next(r for r in ws["runs"] if r["run_id"] == "V3")
    assert legacy["capabilities"]["read_only"] is True and legacy["phase"] == "running"
    assert ws["schema"]["office_schema"] == 1


def test_pruned_run_disappears_cleanly_between_reads(mini, make_observer):
    obs = make_observer(mini["path"])
    assert obs.run("R1") is not None
    con = mini["con"]
    with db.transaction(con):
        for table in ("tasks", "dispatches", "events", "gates", "route_audit", "session_bindings", "requirements",
                      "cursors"):
            con.execute(f"DELETE FROM {table} WHERE run_id='R1'")
        con.execute("DELETE FROM runs WHERE id='R1'")
    assert obs.run("R1") is None
    assert [r["run_id"] for r in obs.workspace()["runs"]] == ["R2"]
    assert obs.activity("R1")["items"] == []


def test_snapshot_is_stable_while_a_writer_commits(mini, make_observer):
    obs = make_observer(mini["path"])
    con = mini["con"]

    def read(snap):
        first = snap.rows("SELECT COUNT(*) AS n FROM runs")[0]["n"]
        with db.transaction(con):
            synthetic.insert_run(con, "R-mid", git_common_dir="/src/mid/.git")
        second = snap.rows("SELECT COUNT(*) AS n FROM runs")[0]["n"]
        return first, second

    first, second = obs.read(read)
    assert first == second == 2
    assert len(obs.workspace()["runs"]) == 3


def test_concurrent_writer_never_tears_a_projection(writer, make_observer):
    path, con = writer
    stop = threading.Event()
    errors: list[BaseException] = []

    def write():
        wcon = db.connect(path)
        try:
            n = 0
            while not stop.is_set() and n < 400:
                with db.transaction(wcon):
                    rid = f"W{n:04d}"
                    synthetic.insert_run(wcon, rid, git_common_dir=f"/src/w{n % 4}/.git")
                    for k in range(3):
                        synthetic.insert_task(wcon, rid, f"T{k}", status="accepted" if k else "running")
                        synthetic.insert_event(wcon, rid, "note", f"{rid} {k}")
                    if n % 5 == 4:  # prune an earlier run in the same transition
                        old = f"W{n - 4:04d}"
                        wcon.execute("DELETE FROM tasks WHERE run_id=?", (old,))
                        wcon.execute("DELETE FROM requirements WHERE run_id=?", (old,))
                        wcon.execute("DELETE FROM runs WHERE id=?", (old,))
                n += 1
        except BaseException as exc:  # surfaced below
            errors.append(exc)
        finally:
            wcon.close()

    obs = make_observer(path)
    thread = threading.Thread(target=write)
    thread.start()
    snapshots = 0
    try:
        while thread.is_alive() or snapshots < 5:
            def check(snap):
                runs = {r["id"] for r in snap.table("runs")}
                orphans = [t for t in snap.table("tasks") if t["run_id"] not in runs]
                return runs, orphans

            runs, orphans = obs.read(check)
            assert orphans == []
            ws = obs.workspace()
            for r in ws["runs"]:
                assert len(r["tasks"]) == 3, r["run_id"]
                assert r["progress"]["total_weight"] == 3
            snapshots += 1
    finally:
        stop.set()
        thread.join()
    assert not errors
    assert snapshots >= 5


def test_read_snapshot_ends_its_transaction(mini):
    con = open_readonly(mini["path"])
    try:
        with read_snapshot(con) as snap:
            assert con.in_transaction and snap.has("runs", "goal")
        assert not con.in_transaction
    finally:
        con.close()


def test_observer_with_context(workspace, make_observer):
    obs = Observer(workspace["db"], host_id="abc", runs_dir=workspace["runs_dir"])
    try:
        assert obs.workspace()["host"] == "host:abc"
    finally:
        obs.close()
