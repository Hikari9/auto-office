"""runs.db authority, outbox durability, version identity and packet pinning."""
from __future__ import annotations

import json
import multiprocessing
import sqlite3
import sys

import pytest

from conftest import GOOD_ADD, SRC, approved_run, start_inline


def test_version_identity_is_exact_pep440(env):
    from office import version
    v = version.current()
    assert version.is_exact(v), v
    assert v.startswith("3.3.4")
    assert not version.is_exact("dev") and not version.is_exact("3.1-dev")


@pytest.mark.approved
def test_every_run_and_packet_carries_office_version(env):
    from office import version
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    con = env.con()
    run = dict(con.execute("SELECT * FROM runs WHERE office_version IS NOT NULL").fetchone())
    assert run["office_version"]
    for row in con.execute("SELECT payload_json, office_version FROM outbox").fetchall():
        assert json.loads(row["payload_json"])["office_version"] == row["office_version"]
        assert version.same_line(row["office_version"], run["office_version"])
    for d in con.execute("SELECT packet_path, office_version FROM dispatches WHERE packet_path IS NOT NULL").fetchall():
        packet = json.loads(open(d["packet_path"]).read())
        assert packet["office_version"] == d["office_version"]
        assert version.same_line(packet["office_version"], run["office_version"])


def test_packet_version_mismatch_is_rejected(env):
    start_inline(env)
    from office import state
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    packet = state.packet_envelope(run, "executor-dispatch", {"task_id": "T1"})
    state.check_packet(run, packet)
    packet["office_version"] = "3.0.9"
    with pytest.raises(state.Refused) as err:
        state.check_packet(run, packet)
    assert err.value.category == "packet-version-mismatch"


def test_job_with_foreign_office_version_never_executes(env, monkeypatch):
    monkeypatch.setenv("OFFICE_JOBS", "manual")
    start_inline(env)
    from office import db, jobs, state
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    with db.transaction(con):
        job = state.enqueue(con, run, "integrate", {"key": "x"}, dedup_key="t1")
        con.execute("UPDATE outbox SET payload_json=? WHERE id=?", (json.dumps({"key": "x", "office_version": "9.9.9"}), job))
    assert jobs.execute(con, job) == 1
    row = con.execute("SELECT status, error FROM outbox WHERE id=?", (job,)).fetchone()
    assert row["status"] == "failed" and "9.9.9" in row["error"], dict(row)
    assert con.execute("SELECT COUNT(*) FROM gates").fetchone()[0] == 0  # the handler never ran


@pytest.mark.approved
def test_crash_after_commit_before_external_work_recovers_once(env, monkeypatch):
    """The dispatch transition commits its launch job; the process dies before
    running it. The next office command runs it exactly once."""
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", env={"OFFICE_JOBS": "manual"}, check=0)
    con = env.con()
    assert con.execute("SELECT COUNT(*) FROM outbox WHERE kind='launch_agent' AND status='queued'").fetchone()[0] == 1
    assert env.calls() == []
    # Simulate a claim left behind by a process that died mid-job.
    con.execute("UPDATE outbox SET status='claimed', claimed_pid=999999, attempts=1 WHERE kind='launch_agent'")
    code, data = env.ojson("status")
    assert data["data"]["tasks"]["T1"] == "accepted", data
    assert [c["role"] for c in env.calls()].count("executor") == 1
    assert con.execute("SELECT COUNT(*) FROM dispatches WHERE role='executor'").fetchone()[0] == 1


def _emit_many(args):
    db_path, run_id, n, tag = args
    sys.path.insert(0, str(SRC))
    import os
    os.environ["AUTO_OFFICE_RUNS_DB"] = db_path
    from office import db, state
    con = db.connect()
    run = state.get_run(con, run_id)
    for i in range(n):
        with db.transaction(con):
            state.emit(con, run, "t", f"{tag}-{i}")
            state.advance_cursor(con, run_id, "c", i + 1)
    con.close()


def test_concurrent_event_writers_neither_lose_nor_duplicate(env):
    start_inline(env)
    from office import paths
    con = env.con()
    run_id = con.execute("SELECT id FROM runs").fetchone()[0]
    before = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(4) as pool:
        # Bounded: a pool child that dies (seen under heavy load) otherwise hangs
        # pool.map, and with it the whole suite, instead of failing this test.
        pool.map_async(_emit_many, [(str(paths.runs_db()), run_id, 40, f"w{i}") for i in range(4)]).get(timeout=300)
    rows = con.execute("SELECT seq, summary FROM events WHERE kind='t'").fetchall()
    assert len(rows) == 160 == con.execute("SELECT COUNT(*) FROM events").fetchone()[0] - before
    assert len({r["seq"] for r in rows}) == 160
    assert len({r["summary"] for r in rows}) == 160
    assert con.execute("SELECT last_seq FROM cursors WHERE consumer='c'").fetchone()[0] == 40


def test_migration_keeps_legacy_recorder_writes_working(env):
    """A retained 3.0 helper inserting with its original column lists still works."""
    from office import paths
    con = env.con()
    legacy = sqlite3.connect(str(paths.runs_db()))
    legacy.execute("INSERT INTO runs(id, family_id, created_at, plugin_commit, policy_hash, catalog_hash, adapter_hash, "
                   "config_hash, status) VALUES('r','f','t','c','p','c','a','h','intake')")
    legacy.execute("INSERT INTO findings(id, dispatch_id, reviewer_dispatch_id, status, severity, summary, evidence_hash, created_at) "
                   "VALUES('f1','unknown-dispatch','r2','open','high','s','h','t')")
    legacy.commit()
    assert con.execute("SELECT office_version FROM runs WHERE id='r'").fetchone()[0] is None


def test_31_findings_must_name_a_recorded_dispatch(env):
    con = env.con()
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO findings(id, dispatch_id, run_id, summary) VALUES('x','nope','run','s')")


def test_projection_edits_have_no_effect(env):
    start_inline(env)
    from office import state
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    view = json.loads(open(f"{run['state_dir']}/view.json").read())
    assert view["_authority"] == "runs.db"
    view["phase"] = "closed"
    open(f"{run['state_dir']}/view.json", "w").write(json.dumps(view))
    code, data = env.ojson("status")
    assert data["data"]["phase"] == "planning"


def test_migration_adds_columns_to_a_db_already_at_the_current_version(env):
    """#211: #200 added pane columns without a version bump, so a runs.db
    already stamped current never got them. Drift, not the stamp, decides."""
    from office import db, paths
    env.con().close()
    raw = sqlite3.connect(str(paths.runs_db()))
    raw.execute("ALTER TABLE dispatches DROP COLUMN pane_closed_at")
    raw.execute("DROP TABLE deviations")
    raw.commit()
    raw.close()
    con = db.connect()
    assert "pane_closed_at" in {r[1] for r in con.execute("PRAGMA table_info(dispatches)")}
    assert con.execute("SELECT 1 FROM sqlite_master WHERE name='deviations'").fetchone()
    assert not db._drifted(con)
