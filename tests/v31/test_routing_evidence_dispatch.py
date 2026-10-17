"""Dispatch evidence for routing: the task size snapshot, the predecessor link, the task's
first executor dispatch, and the accepted producer. Agents never run for the route tests:
dispatches are launched external."""
from __future__ import annotations

import json
import sqlite3
import sys

import pytest

from conftest import GOOD_ADD, SRC, approved_run, task_row

from test_adaptive_dispatch import EXTERNAL, PLANNED, _approve, _quota

CLAUDE = "claude@1/claude-opus-5-5@medium"
CODEX = "codex@1/gpt-6-astra@low"
V12_COLUMNS = {"predecessor_dispatch_id", "first_executor_dispatch_id", "accepted_producer_dispatch_id"}


def _dispatches(env, role="executor"):
    return [dict(r) for r in env.con().execute(
        "SELECT * FROM dispatches WHERE task_id='T1' AND role=? ORDER BY started_at, rowid", (role,))]


def _revoke_and_redispatch(env, *args):
    env.office("revoke", "T1", env=EXTERNAL, check=0)
    code, out = env.office("dispatch", "T1", *args, env=_quota(env))
    assert code == 0, out


# ------------------------------------------------------------------ schema

def _db():
    sys.path.insert(0, str(SRC))
    from office import db
    return db


def _build_v11(db, path) -> None:
    """A runs.db as the v11 runtime left it: no v12 column, stamped 11, with rows in it."""
    con = sqlite3.connect(str(path), isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    for stmt in db._statements(db.LEGACY_DDL + db.OFFICE_DDL):
        con.execute(stmt)
    for table, columns in db.SHARED_COLUMNS.items():
        for col in columns:
            if col.split()[0] not in V12_COLUMNS | db._columns(con, table):
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
    con.execute(db.TRIGGERS)
    for trigger in db.DISCOVERY_TRIGGERS:
        con.execute(trigger)
    con.execute("INSERT INTO schema_meta(key, value) VALUES('office_schema', '11')")
    con.execute("INSERT INTO runs(id, phase) VALUES('r-old', 'executing')")
    con.execute("INSERT INTO tasks(run_id, id, title, role, scope_json, depends_json, accept_json, checks_json, status, "
                "introduced_plan_version, contract_version, acceptance_version, created_at, updated_at) "
                "VALUES('r-old','T1','t','executor','[]','[]','[]','[]','accepted',1,1,1,'x','x')")
    con.execute("INSERT INTO dispatches(id, run_id, role, task_id, size_class) VALUES('Dold','r-old','executor','T1','M')")
    assert not V12_COLUMNS & (db._columns(con, "tasks") | db._columns(con, "dispatches"))
    con.close()


def test_a_v11_database_gains_the_nullable_columns_and_reopens_idempotently(tmp_path):
    db = _db()
    assert db.SCHEMA_VERSION >= 12
    path = tmp_path / "runs.db"
    _build_v11(db, path)
    con = db.connect(path)
    try:
        assert db._schema_version(con) == db.SCHEMA_VERSION and db.missing_columns(con) == []
        old_task = con.execute("SELECT * FROM tasks WHERE id='T1'").fetchone()
        assert old_task["first_executor_dispatch_id"] is None and old_task["accepted_producer_dispatch_id"] is None
        old = con.execute("SELECT * FROM dispatches WHERE id='Dold'").fetchone()
        assert old["predecessor_dispatch_id"] is None and old["size_class"] == "M"
        assert con.execute("SELECT phase FROM runs WHERE id='r-old'").fetchone()[0] == "executing"
        before = sorted(tuple(r) for r in con.execute("SELECT type, name, sql FROM sqlite_master"))
        for _ in range(2):  # force the full migration pass, not the read-only fast path
            con.execute("UPDATE schema_meta SET value='11' WHERE key='office_schema'")
            db.migrate(con)
        assert sorted(tuple(r) for r in con.execute("SELECT type, name, sql FROM sqlite_master")) == before
        assert db._schema_version(con) == db.SCHEMA_VERSION
    finally:
        con.close()
    again = db.connect(path)  # a plain reopen of the migrated file changes nothing
    try:
        assert sorted(tuple(r) for r in again.execute("SELECT type, name, sql FROM sqlite_master")) == before
        assert again.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0] == 1
    finally:
        again.close()


# ------------------------------------------------------------------ size snapshot

def _sized(plan: str, size: str | None, run_size: str | None = None) -> str:
    plan = plan.replace("visual: none", "visual: none" + (f"\ntask_size: {size}" if size else ""), 1)
    return plan.replace("blast_radius: repo", "blast_radius: repo" + (f"\nsize_class: {run_size}" if run_size else ""), 1)


@pytest.mark.parametrize("task_size,run_size,expected", [
    ("M", None, "M"),
    ("XL", "S", "XL"),   # the task's own size wins over the run's
    (None, "L", None),   # a task without task_size gets NULL even when the run has size_class
    (None, None, None),
])
def test_an_executor_dispatch_copies_the_tasks_size_not_the_runs(env, task_size, run_size, expected):
    _approve(env, plan=_sized(PLANNED, task_size, run_size))
    run_risk = json.loads(env.con().execute("SELECT risk_json FROM runs").fetchone()[0])
    assert run_risk.get("size_class") == run_size
    code, out = env.office("dispatch", "T1", env=_quota(env))
    assert code == 0, out
    (d,) = _dispatches(env)
    assert d["size_class"] == expected


def test_a_relaunch_snapshots_the_size_again(env):
    _approve(env, plan=_sized(PLANNED, "S"))
    env.office("dispatch", "T1", env=_quota(env), check=0)
    _revoke_and_redispatch(env)
    assert [d["size_class"] for d in _dispatches(env)] == ["S", "S"]


# ------------------------------------------------------------------ predecessor and first executor

def test_the_first_dispatch_has_no_predecessor_and_is_the_first_executor(env):
    _approve(env)
    env.office("dispatch", "T1", env=_quota(env), check=0)
    (d1,) = _dispatches(env)
    assert d1["predecessor_dispatch_id"] is None
    assert task_row(env)["first_executor_dispatch_id"] == d1["id"]


def test_a_same_route_retry_links_its_predecessor_and_keeps_the_first_executor(env):
    _approve(env)
    env.office("dispatch", "T1", env=_quota(env), check=0)
    _revoke_and_redispatch(env)
    _revoke_and_redispatch(env)
    d1, d2, d3 = _dispatches(env)
    assert {d["triple"] for d in (d1, d2, d3)} == {CLAUDE}
    assert [d["predecessor_dispatch_id"] for d in (d1, d2, d3)] == [None, d1["id"], d2["id"]]
    assert task_row(env)["first_executor_dispatch_id"] == d1["id"]


def test_a_cross_route_swap_and_swap_back_chain_every_dispatch(env):
    _approve(env)
    env.office("dispatch", "T1", env=_quota(env), check=0)
    env.office("revoke", "T1", env=EXTERNAL, check=0)
    code, out = env.office("dispatch", "T1", "--as", "codex/gpt-6-astra@low", env=EXTERNAL)
    assert code == 0, out
    env.office("revoke", "T1", env=EXTERNAL, check=0)
    code, out = env.office("dispatch", "T1", "--as", "claude/claude-opus-5-5@medium", env=EXTERNAL)
    assert code == 0, out
    d1, d2, d3 = _dispatches(env)
    assert [d["triple"] for d in (d1, d2, d3)] == [CLAUDE, CODEX, CLAUDE]
    assert [d["predecessor_dispatch_id"] for d in (d1, d2, d3)] == [None, d1["id"], d2["id"]]
    assert task_row(env)["first_executor_dispatch_id"] == d1["id"], "a swap never rewrites the first executor"


def test_a_fresh_rerun_links_its_predecessor(env):
    _approve(env)
    env.office("dispatch", "T1", env=_quota(env), check=0)
    con = env.con()
    con.execute("UPDATE dispatches SET status='ended', ended_at='2026-10-09T00:00:00+00:00' WHERE task_id='T1'")
    con.execute("UPDATE tasks SET status='changes_required' WHERE id='T1'")
    con.commit()
    code, out = env.office("rerun", "T1", "--fresh", env=EXTERNAL)
    assert code == 0, out
    d1, d2 = _dispatches(env)
    assert d2["predecessor_dispatch_id"] == d1["id"]
    assert task_row(env)["first_executor_dispatch_id"] == d1["id"]


def test_a_task_whose_earlier_executors_predate_the_column_stays_null(env):
    _approve(env)
    env.office("dispatch", "T1", env=_quota(env), check=0)
    con = env.con()
    con.execute("UPDATE tasks SET first_executor_dispatch_id=NULL WHERE id='T1'")  # as a pre-v12 task has it
    con.commit()
    _revoke_and_redispatch(env)
    assert task_row(env)["first_executor_dispatch_id"] is None, "the first executor is not provable, so it is not guessed"
    d1, d2 = _dispatches(env)
    assert d2["predecessor_dispatch_id"] == d1["id"]


def test_a_predecessor_from_another_task_is_not_linked(env):
    _approve(env)
    env.office("dispatch", "T1", env=_quota(env), check=0)
    (d1,) = _dispatches(env)
    con = env.con()
    con.execute("UPDATE dispatches SET task_id='T9' WHERE id=?", (d1["id"],))  # the task points at a foreign dispatch
    con.commit()
    env.office("revoke", "T1", env=EXTERNAL, check=0)
    env.office("dispatch", "T1", env=_quota(env), check=0)
    (d2,) = _dispatches(env)
    assert d2["predecessor_dispatch_id"] is None


# ------------------------------------------------------------------ accepted producer

@pytest.mark.review_contract("v3.1")
def test_the_accepted_producer_is_the_dispatch_of_the_accepted_revision(env):
    finding = "VERDICT: CHANGES_REQUIRED\nFINDING F1 | material | calc.py:2 | add ignores overflow | clamp the result"
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True},
                                {"write": {"calc.py": GOOD_ADD + "# fixed\n"}, "submit": True}],
                 code_reviewer=[{"reply": finding}, {"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    t = task_row(env)
    assert t["status"] == "changes_required" and t["accepted_producer_dispatch_id"] is None
    env.office("rerun", "T1", "--fresh", check=0)
    t = task_row(env)
    assert t["status"] == "accepted", t
    con = env.con()
    producer = con.execute("SELECT dispatch_id FROM revisions WHERE id=?", (t["accepted_revision_id"],)).fetchone()[0]
    d1, d2 = _dispatches(env)
    assert producer == d2["id"] and t["accepted_producer_dispatch_id"] == producer
    assert t["first_executor_dispatch_id"] == d1["id"] != producer, "the first executor is not the accepted producer"
    assert d2["predecessor_dispatch_id"] == d1["id"]


@pytest.mark.approved
def test_a_task_accepted_on_its_first_dispatch_names_it_as_producer(env):
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
                 code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    t = task_row(env)
    assert t["status"] == "accepted"
    assert t["accepted_producer_dispatch_id"] == t["first_executor_dispatch_id"] == t["current_dispatch_id"]
