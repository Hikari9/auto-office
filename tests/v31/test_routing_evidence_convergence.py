"""Convergence evidence for routing: the member tasks frozen onto a lane or shared-scope gate, and
evidence-only attribution on findings. Repair routing (`convergence._owners`) is unchanged by both.
Reviewers are scripted fake harnesses run in process; no real agent runs."""
from __future__ import annotations

import json
import sqlite3
import sys

import pytest

from conftest import GOOD_ADD, GOOD_MUL, SRC, approved_run, task_row

from test_convergence_contract import (APPROVED, GOOD_ADD2, GOOD_MUL2, PLAN_LANE, PLAN_SHARED, _gates, _q, _run_row,
                                       _start, _status, finding, recheck)

T2_COLUMNS = {"members_json", "attribution_basis", "attributed_task", "clone_of"}
WORK = {"T1": {"calc.py": GOOD_ADD}, "T2": {"mul.py": GOOD_MUL}}
FIX = {"T1": {"calc.py": GOOD_ADD2}, "T2": {"mul.py": GOOD_MUL2}}


def _modules():
    sys.path.insert(0, str(SRC))
    from office import convergence, db, route_learning
    return convergence, db, route_learning


def _ownerless(code, where, what="needs work"):
    """A blocking finding with no `owner:` field: the reviewer did not say who owns it."""
    return f"FINDING {code} | medium | blocking | {where} | {what} | fix it"


def _lane_run(env, *replies, executors=2):
    _start(env, plan=PLAN_LANE,
           executor=[{"write_by_task": WORK, "submit": True}] * executors + [{"write_by_task": FIX, "submit": True}],
           convergence_reviewer=[{"reply": r} for r in replies])
    env.office("dispatch", "T1", "T2", "--parallel", check=0)


def _members(env):
    return {g["id"]: g["members_json"] for g in _q(env, "SELECT id, members_json FROM gates WHERE scope IS NOT NULL")}


def _findings(env, code):
    return _q(env, "SELECT * FROM findings WHERE code=? ORDER BY rowid", (code,))


# ------------------------------------------------------------------ schema

def _build_without_t2_columns(db, path) -> None:
    """A runs.db stamped with the current version whose gates and findings lack the T2 columns."""
    con = sqlite3.connect(str(path), isolation_level=None)
    con.execute("PRAGMA journal_mode=WAL")
    for stmt in db._statements(db.LEGACY_DDL + db.OFFICE_DDL):
        con.execute(stmt)
    for table, columns in db.SHARED_COLUMNS.items():
        for col in columns:
            if col.split()[0] not in T2_COLUMNS | db._columns(con, table):
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
    con.execute(db.TRIGGERS)
    for trigger in db.DISCOVERY_TRIGGERS:
        con.execute(trigger)
    con.execute("INSERT INTO schema_meta(key, value) VALUES('office_schema', ?)", (str(db.SCHEMA_VERSION),))
    con.execute("INSERT INTO gates(id, run_id, subject, kind, input_key, status, created_at, scope) "
                "VALUES('Gold','r-old','lane','convergence_review','L-T1:abc','done','x','L-T1')")
    con.execute("INSERT INTO findings(id, run_id, task_id, code, scope, state) VALUES('Fold','r-old','T1','F1','L-T1','open')")
    assert not T2_COLUMNS & (db._columns(con, "gates") | db._columns(con, "findings"))
    con.close()


def test_the_additive_column_pass_adds_the_t2_columns_without_a_version_bump(tmp_path):
    _, db, _ = _modules()
    registered = {c.split()[0] for cols in db.SHARED_COLUMNS.values() for c in cols}
    assert T2_COLUMNS <= registered
    path = tmp_path / "runs.db"
    _build_without_t2_columns(db, path)
    con = db.connect(path)
    try:
        assert db._schema_version(con) == db.SCHEMA_VERSION  # stamped current: only the drift pass can add them
        assert db.missing_columns(con) == []
        assert T2_COLUMNS - {"members_json"} <= db._columns(con, "findings") and "members_json" in db._columns(con, "gates")
        gate = con.execute("SELECT * FROM gates WHERE id='Gold'").fetchone()
        old = con.execute("SELECT * FROM findings WHERE id='Fold'").fetchone()
        assert gate["members_json"] is None and old["attribution_basis"] is None and old["clone_of"] is None
        assert _modules()[0].gate_members(dict(gate)) is None  # NULL is unknown
        before = sorted(tuple(r) for r in con.execute("SELECT type, name, sql FROM sqlite_master"))
    finally:
        con.close()
    again = db.connect(path)
    try:
        assert sorted(tuple(r) for r in again.execute("SELECT type, name, sql FROM sqlite_master")) == before
        assert again.execute("SELECT COUNT(*) FROM findings").fetchone()[0] == 1
    finally:
        again.close()


# ------------------------------------------------------------------ frozen membership

def test_a_lane_gate_freezes_its_member_tasks(env):
    _lane_run(env, APPROVED)
    (gate,) = _gates(env, "convergence_review")
    assert gate["scope"] == "L-core" and json.loads(gate["members_json"]) == ["T1", "T2"]
    assert (gate["subject"], gate["task_id"], gate["status"], gate["verdict"]) == ("lane", None, "done", "APPROVED")
    assert gate["input_key"].startswith("L-core:") and gate["revision_id"].startswith("L-core@")


def test_shared_scope_and_lane_gates_each_freeze_their_own_members(env):
    _start(env, plan=PLAN_SHARED, executor=[{"write_by_task": WORK, "submit": True}],
           convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", "T2", "--parallel", check=0)
    members = {g["scope"]: json.loads(g["members_json"]) for g in _gates(env, "convergence_review")}
    assert members == {"L-T1": ["T1"], "L-T2": ["T2"], "S-T1+T2": ["T1", "T2"]}


def test_the_member_list_is_sorted_by_task_number(env):
    convergence, _, _ = _modules()
    approved_run(env)
    con = env.con()
    run = dict(con.execute("SELECT * FROM runs").fetchone())
    gid, _ = convergence._new_gate(con, run, {"id": "L-x", "tasks": ["T10", "T2", "T1"]},
                                   {"id": "L-x@abc", "commit_sha": "abc"}, "convergence_review", 1, 1)
    assert json.loads(con.execute("SELECT members_json FROM gates WHERE id=?", (gid,)).fetchone()[0]) == ["T1", "T2", "T10"]


def test_a_populated_membership_never_changes_through_recheck_and_recompose(env):
    """Round 2 recomposes the lane under a new gate; the round-1 gate keeps the list it was created with,
    through ingest, settle, repair routing and the recompose."""
    _lane_run(env, recheck(finding("F1", "T1"), finding("F2", "T2", where="mul.py:1")), APPROVED)
    frozen = _members(env)
    assert len(frozen) == 1 and all(json.loads(v) == ["T1", "T2"] for v in frozen.values())
    env.office("rerun", "T1", "--fresh", check=0)
    env.office("rerun", "T2", "--fresh", check=0)
    after = _members(env)
    assert len(after) == 2 and {k: after[k] for k in frozen} == frozen
    assert all(json.loads(v) == ["T1", "T2"] for v in after.values())
    assert _run_row(env)["landing"]["convergence"]["L-core"]["status"] == "approved"


def test_no_code_path_rewrites_a_populated_membership():
    """Detection: the only SQL that names the column is the gate INSERT, so no UPDATE can move it."""
    import re
    from pathlib import Path
    src = Path(_modules()[0].__file__).parent
    for py in src.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        assert not re.search(r"UPDATE\s+gates\s+SET[^\"]*members_json", text), py
    assert "members_json" in (src / "convergence.py").read_text(encoding="utf-8")


def test_a_gate_with_no_membership_reads_as_unknown_and_attributes_nothing():
    convergence, _, _ = _modules()
    assert convergence.gate_members({"members_json": None}) is None
    assert convergence.gate_members({}) is None
    assert convergence.gate_members({"members_json": '["T1","T2"]'}) == ["T1", "T2"]
    assert convergence._attribution(None, {}, None, {"location": "calc.py:1", "owners": ["T1"]}, {}) == \
        (convergence.UNASSIGNED, None)


# ------------------------------------------------------------------ attribution

def test_a_reviewer_naming_the_member_that_owns_the_path_is_reviewer_declared(env):
    _lane_run(env, recheck(finding("F1", "T1", where="calc.py:1")), APPROVED)
    (row,) = _findings(env, "F1")
    assert (row["attribution_basis"], row["attributed_task"], row["task_id"], row["clone_of"]) == \
        ("reviewer-declared", "T1", "T1", None)


def test_a_declared_owner_that_does_not_own_the_path_is_not_reviewer_declared(env):
    """The reviewer says T1 but the path is T2's scope alone: the repair still goes to T1 (unchanged),
    the evidence says T2 by unique path."""
    _lane_run(env, recheck(finding("F1", "T1", where="mul.py:1")), APPROVED)
    (row,) = _findings(env, "F1")
    assert (row["attribution_basis"], row["attributed_task"], row["task_id"]) == ("unique-path", "T2", "T1")
    assert task_row(env, "T1")["status"] == "changes_required" and task_row(env, "T2")["status"] == "accepted"


def test_a_path_in_exactly_one_members_scope_is_unique_path(env):
    _lane_run(env, recheck(_ownerless("F1", "mul.py:3")), APPROVED)
    (row,) = _findings(env, "F1")  # `_owners` also finds one owner by path, so one row
    assert (row["attribution_basis"], row["attributed_task"], row["task_id"], row["clone_of"]) == \
        ("unique-path", "T2", "T2", None)


def test_an_ambiguous_recheck_stays_unassigned_and_counts_once(env):
    """No owner and no member owns the path: repair goes to every member (unchanged), attribution is
    `unassigned`, and the repair-owner copies are marked so a report counts the finding once."""
    _lane_run(env, recheck(_ownerless("F1", "README.md:1")), APPROVED)
    rows = _findings(env, "F1")
    assert [r["task_id"] for r in rows] == ["T1", "T2"]
    assert {(r["attribution_basis"], r["attributed_task"]) for r in rows} == {("unassigned", None)}
    assert rows[0]["clone_of"] is None and rows[1]["clone_of"] == rows[0]["id"]
    assert _q(env, "SELECT COUNT(*) AS n FROM findings WHERE code='F1' AND clone_of IS NULL")[0]["n"] == 1
    tasks = _status(env)["data"]["tasks"]
    assert tasks["T1"] == "changes_required" and tasks["T2"] == "changes_required", tasks


def test_a_path_two_members_own_is_unassigned_even_when_one_is_declared_twice(env):
    """Two declared owners both owning the path is not a unique reading either."""
    convergence, _, _ = _modules()
    owned = {"T1": (["shared/**"], set()), "T2": (["shared/**"], set())}
    both = {"location": "shared/a.py:1", "owners": ["T1", "T2"]}
    assert convergence._attribution(None, {}, ["T1", "T2"], both, owned) == (convergence.UNASSIGNED, None)
    one = {"location": "shared/a.py:1", "owners": ["T2"]}
    assert convergence._attribution(None, {}, ["T1", "T2"], one, owned) == ("reviewer-declared", "T2")


def test_a_path_in_a_members_accepted_revision_counts_as_owned(env, monkeypatch):
    convergence, _, _ = _modules()
    monkeypatch.setattr(convergence, "_changed", lambda con, run, task: {"generated/out.txt"} if task["id"] == "T2" else set())
    approved_run(env)
    con = env.con()
    run = dict(con.execute("SELECT * FROM runs").fetchone())
    f = {"location": "generated/out.txt:4"}
    assert convergence._attribution(con, run, ["T1"], f, {}) == (convergence.UNASSIGNED, None)
    monkeypatch.setattr(convergence, "_changed", lambda con, run, task: {"generated/out.txt"})
    assert convergence._attribution(con, run, ["T1"], f, {}) == ("unique-path", "T1")


def test_text_named_owners_route_the_repair_but_are_not_reviewer_declared(env):
    """`_owners` falls back to a `T2` mentioned in the summary; that is routing, not a reviewer-declared owner."""
    _lane_run(env, recheck(_ownerless("F1", "README.md:1", what="T2 forgot the docs")), APPROVED)
    (row,) = _findings(env, "F1")
    assert (row["task_id"], row["attribution_basis"], row["attributed_task"]) == ("T2", "unassigned", None)


# ------------------------------------------------------------------ repair routing unchanged

def test_repair_routing_is_the_same_for_every_owner_shape(env):
    """`_owners` is unchanged: each shape routes to the same task set as before this change."""
    convergence, _, _ = _modules()
    _lane_run(env, APPROVED)
    con = env.con()
    run = dict(con.execute("SELECT * FROM runs").fetchone())
    scope = {"id": "L-core", "tasks": ["T1", "T2"]}
    cases = [
        ({"owners": ["T2"], "location": "calc.py:1"}, ["T2"]),                     # named wins over the path
        ({"owners": ["T9"], "location": "calc.py:1"}, ["T1"]),                     # unknown owner: the path decides
        ({"location": "mul.py:2", "summary": "x"}, ["T2"]),                       # the path
        ({"location": "README.md:1", "summary": "T1 and T2 both"}, ["T1", "T2"]),  # ids in the text
        ({"location": "README.md:1", "summary": "nothing"}, ["T1", "T2"]),         # nobody can tell: everyone
        ({}, ["T1", "T2"]),
    ]
    for f, expected in cases:
        assert convergence._owners(con, run, scope, f) == expected, f


def test_recheck_repair_actions_are_unchanged(env):
    _lane_run(env, recheck(finding("F1", "T1"), _ownerless("F2", "README.md:1")), APPROVED)
    tasks = _status(env)["data"]["tasks"]
    assert tasks["T1"] == "changes_required" and tasks["T2"] == "changes_required", tasks
    nxt = _status(env)["next"]
    assert "office rerun T1" in nxt and "office rerun T2" in nxt, nxt
    assert "L-core RECHECK: repair F1" in task_row(env, "T1")["pause_reason"]
    assert "F2" in task_row(env, "T2")["pause_reason"] and "F1" not in task_row(env, "T2")["pause_reason"]
    events = _q(env, "SELECT task_id, summary FROM events WHERE kind='gate.recheck' ORDER BY seq")
    assert [e["task_id"] for e in events] == ["T1", "T2"]


# ------------------------------------------------------------------ route learning unchanged

def test_route_learning_reads_the_same_findings_with_or_without_attribution(env):
    """Route learning keys findings by the producing dispatch of each repair-owner row, as before; the
    attribution columns and the clone marks never reach it."""
    _, _, route_learning = _modules()
    _lane_run(env, recheck(finding("F1", "T1"), _ownerless("F2", "README.md:1")), APPROVED)
    con = env.con()
    with_marks = route_learning.derive_outcomes(con)
    assert con.execute("SELECT COUNT(*) FROM findings WHERE attribution_basis IS NOT NULL").fetchone()[0] == 3
    con.execute("UPDATE findings SET attribution_basis=NULL, attributed_task=NULL, clone_of=NULL")
    assert route_learning.derive_outcomes(con) == with_marks
    assert with_marks, "the run produced executor outcomes"
    rows = con.execute("SELECT task_id, dispatch_id, revision_id, category FROM findings WHERE code IN ('F1','F2') "
                       "ORDER BY code, task_id").fetchall()
    assert [(r["task_id"], r["category"]) for r in rows] == [("T1", "convergence")] * 2 + [("T2", "convergence")]
    assert all(r["dispatch_id"] for r in rows)


# ------------------------------------------------------------------ contracts

def test_convergence_v1_run_records_membership_and_attribution(env):
    _lane_run(env, recheck(finding("F1", "T1")), APPROVED)
    assert _run_row(env)["gates"]["review_contract"] == "convergence-v1"
    assert all(json.loads(g["members_json"]) == ["T1", "T2"] for g in _gates(env, "convergence_review"))
    assert _findings(env, "F1")[0]["attribution_basis"] == "reviewer-declared"


@pytest.mark.review_contract("v3.1")
def test_a_pinned_v31_run_has_no_convergence_membership_or_attribution(env):
    """A v3.1 run keeps rolling task review: no lane or shared-scope gate, so nothing is frozen and its
    findings carry no attribution."""
    finding_reply = "VERDICT: CHANGES_REQUIRED\nFINDING F1 | material | calc.py:2 | add ignores overflow | clamp the result"
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True},
                                {"write": {"calc.py": GOOD_ADD2}, "submit": True}],
                 code_reviewer=[{"reply": finding_reply}, {"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    assert _run_row(env)["gates"]["review_contract"] == "v3.1"
    assert _q(env, "SELECT COUNT(*) AS n FROM gates WHERE kind='convergence_review' OR scope IS NOT NULL")[0]["n"] == 0
    assert _q(env, "SELECT COUNT(*) AS n FROM gates WHERE members_json IS NOT NULL")[0]["n"] == 0
    rows = _q(env, "SELECT * FROM findings WHERE code='F1'")
    assert rows, "the v3.1 review recorded its finding"
    assert all(r["attribution_basis"] is None and r["attributed_task"] is None and r["clone_of"] is None for r in rows)
