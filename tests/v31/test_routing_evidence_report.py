"""scripts/routing_evidence_report.py (#528): an offline, read-only, aggregate-only report over a runs.db.

Every database here is synthetic. One scenario is inserted into three schemas: the current one plus the v12
evidence columns (T1/T2 of the routing-evidence stack, added here by hand so the report is exercised whether or not
they are in this checkout's base), the current one without them (an "old" database), and the 3.0 recorder's tables
alone. An insert drops any column its table lacks, so the same rows serve all three.

Every id and free-text value carries the sentinel `zq9x`; the aggregate-only test fails if one reaches the output.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import routing_evidence_report as rer
from office import db, route_learning

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "routing-evidence-report.md"
Z = "zq9x"

V12_COLUMNS = {
    "dispatches": ["predecessor_dispatch_id TEXT"],
    "tasks": ["first_executor_dispatch_id TEXT", "accepted_producer_dispatch_id TEXT"],
    "findings": ["attribution_basis TEXT", "attributed_task TEXT", "clone_of TEXT"],
    "gates": ["members_json TEXT"],
}
A = ("claude", "claude-sonnet-5-5", "high")
B = ("codex", "gpt-5-codex", "medium")
R1, R2 = f"run-{Z}-1", f"run-{Z}-2"
# The populated scenario read by dispatches.started_at alone. T4's first dispatch has no start time, so its order is
# unknown; T1 and T5 were accepted from their only dispatch; T2 (A -> B -> A) and T3 (A -> A) from a later one.
CHRONOLOGICAL_POPULATED = {
    "accepted_tasks": 5, "order_unknown": 1, "producer_unknown": 0, "comparable": 4, "accepted_from_first_dispatch": 2,
    "later_dispatch_same_route": 2, "first_final_mismatch": 0, "route_unknown": 0, "cross_route_handoffs": 2,
    "tasks_with_cross_route_handoff": 1, "intermediate_cross_route_handoffs": 1,
    "tasks_with_intermediate_cross_route_handoff": 1, "route_unknown_handoffs": 0}


def did(name: str) -> str:
    return f"disp-{Z}-{name}"


def _insert(con, table: str, **row):
    have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    row = {k: v for k, v in row.items() if k in have}
    con.execute(f"INSERT INTO {table}({', '.join(row)}) VALUES({', '.join('?' * len(row))})", tuple(row.values()))


def _dispatch(con, name, task, route, *, run=R1, role="executor", start=None, end=None, wall=None, money=None,
              pred=None, size=None, descriptor=None, triple=None):
    harness, model, effort = route if route else (None, None, None)
    _insert(con, "dispatches", id=did(name), run_id=run, role=role, task_id=task, harness=harness, model=model,
            effort=effort, triple=triple, started_at=start, ended_at=end, wall_clock_seconds=wall, money_actual=money,
            predecessor_dispatch_id=did(pred) if pred else None, size_class=size, descriptor_json=descriptor,
            worktree=f"/Users/{Z}/worktrees/{name}", branch=f"office/{Z}/{name}", log_path=f"/tmp/{Z}/{name}.log")


def _task(con, tid, status, *, run=R1, first=None, rev=None, producer=None, descriptor=None):
    _insert(con, "tasks", run_id=run, id=tid, title=f"title {Z}", role="executor", scope_json=json.dumps([f"src/{Z}.py"]),
            depends_json="[]", accept_json=json.dumps([f"accept {Z}"]), checks_json="[]", status=status,
            introduced_plan_version=1, contract_version=1, acceptance_version=1, created_at="2026-09-01T09:00:00+00:00",
            updated_at="2026-09-01T09:00:00+00:00", first_executor_dispatch_id=did(first) if first else None,
            accepted_revision_id=f"rev-{Z}-{rev}" if rev else None,
            accepted_producer_dispatch_id=did(producer) if producer else None, descriptor_json=descriptor)


def _revision(con, name, task, seq, disp, self_review, *, run=R1):
    _insert(con, "revisions", id=f"rev-{Z}-{name}", run_id=run, task_id=task, seq=seq, dispatch_id=did(disp),
            commit_sha=f"sha-{Z}", tree_sha=f"tree-{Z}", requirements_version=1, plan_version=1, applied_version=1,
            env_fingerprint=Z, operation_id=f"op-{Z}-{name}", status="current", created_at="2026-09-01T09:00:00+00:00",
            self_review_json=self_review)


def _gate(con, name, subject, scope, members, *, run=R1):
    _insert(con, "gates", id=f"gate-{Z}-{name}", run_id=run, subject=subject, scope=scope, kind="code_review",
            input_key=Z, status="done", created_at="2026-09-01T09:00:00+00:00", summary=f"summary {Z}",
            members_json=json.dumps(members) if members is not None else None, contract="convergence-v1")


def _finding(con, name, gate, code, basis, attributed, task, *, clone_of=None, contract="convergence-v1", run=R1):
    _insert(con, "findings", id=f"find-{Z}-{name}", run_id=run, gate_id=f"gate-{Z}-{gate}", task_id=task, code=code,
            contract=contract, state="open", summary=f"summary {Z}", location=f"src/{Z}.py:1", scope="lane",
            attribution_basis=basis, attributed_task=attributed,
            clone_of=f"find-{Z}-{clone_of}" if clone_of else None)


def _attribution(con, dispatch, route, success, attribution, *, run=R1):
    """A persisted route_attributions row, as route_learning.refresh writes it at run close."""
    harness, model, effort = route
    _insert(con, "route_attributions", dispatch_id=did(dispatch), run_id=run, route=f"{harness}/{model}@{effort}",
            success=success, attribution=attribution, confidence=0.9, provenance=Z, learn_weight=1.0,
            learner_version="1", derived_at="2026-10-01T00:00:00+00:00")


LEDGER = json.dumps({"kind": "ledger", "sha256": Z, "tree": Z})
EXEMPT = lambda kind: json.dumps({"kind": "exempt", "type": kind, "reason": f"reason {Z}"})  # noqa: E731


def populate(con) -> None:
    """The scenario. Expected numbers in the tests are counted from this by hand."""
    _insert(con, "runs", id=R1, created_at="2026-09-01T09:00:00+00:00", phase="closed", goal=f"goal {Z}",
            risk_json=json.dumps({"size_class": "L"}), pruned_at="2026-10-05T00:00:00+00:00", repo_root=f"/Users/{Z}")
    _insert(con, "runs", id=R2, created_at="2026-10-02T09:00:00+00:00", phase="active", goal=f"goal {Z}")
    t = "2026-09-01T"
    # T1: one executor, accepted first time.
    _dispatch(con, "1a", "T1", A, start=t + "10:00:00+00:00", end=t + "10:01:40+00:00", money=1.5, size="M",
              descriptor=json.dumps({"evidence_domain": "backend"}))
    _task(con, "T1", "accepted", first="1a", rev="1", producer="1a",
          descriptor=json.dumps({"task_size": "M", "evidence_domain": "backend", "intent": "fix",
                                 "difficulty_estimate": "high", "brief_shape": "checks-only"}))
    _revision(con, "1", "T1", 1, "1a", LEDGER)
    # T2: swap away and back (A -> B -> A); the accepted revision is the third.
    _dispatch(con, "2a", "T2", A, start=t + "10:00:00+00:00", end=t + "10:10:00+00:00", size="L", descriptor="{}")
    _dispatch(con, "2b", "T2", B, start=t + "10:15:00+00:00", end=t + "10:30:00+00:00", money=3.0, pred="2a", size="L")
    _dispatch(con, "2c", "T2", A, start=t + "11:00:00+00:00", end=t + "11:05:00+00:00", pred="2b")
    _task(con, "T2", "accepted", first="2a", rev="2c", producer="2c",
          descriptor=json.dumps({"task_size": "L", "evidence_domain": "backend", "intent": "feature"}))
    _revision(con, "2a", "T2", 1, "2a", EXEMPT("mechanical"))
    _revision(con, "2b", "T2", 2, "2b", None)
    _revision(con, "2c", "T2", 3, "2c", LEDGER)
    # T3: same-route retry (A -> A). The first dispatch records a wall clock that differs from its timestamps.
    _dispatch(con, "3a", "T3", A, start=t + "09:00:00+00:00", end=t + "09:01:00+00:00", wall=40.0)
    _dispatch(con, "3b", "T3", A, start=t + "09:10:00+00:00", end=t + "09:15:00+00:00", money=0.5, pred="3a")
    _task(con, "T3", "accepted", first="3a", rev="3b", producer="3b",
          descriptor=json.dumps({"task_size": f"{Z}-huge", "intent": f"{Z}-intent"}))
    _revision(con, "3a", "T3", 1, "3a", LEDGER)
    _revision(con, "3b", "T3", 2, "3b", EXEMPT("trivial"))
    # T4: first/final mismatch (A -> B), no swap back. The first dispatch has no start time.
    _dispatch(con, "4a", "T4", A, end=t + "12:00:00+00:00")
    _dispatch(con, "4b", "T4", B, start=t + "12:00:00+00:00", end=t + "12:20:00+00:00", pred="4a")
    _task(con, "T4", "accepted", first="4a", rev="4b", producer="4b",
          descriptor=json.dumps({"task_size": "S", "difficulty_estimate": "unknown"}))
    _revision(con, "4a", "T4", 1, "4a", EXEMPT("empty"))
    _revision(con, "4b", "T4", 2, "4b", LEDGER)
    # T5: recorded before any evidence column existed: no first executor, no descriptor, no receipt.
    _dispatch(con, "5a", "T5", A, start=t + "08:00:00+00:00", end=t + "07:00:00+00:00")
    _task(con, "T5", "accepted", rev="5", producer=None)
    _revision(con, "5", "T5", 1, "5a", None)
    # T6: unresolved (still submitted); its dispatch is pending. One receipt is not JSON.
    _dispatch(con, "6a", "T6", A)
    _task(con, "T6", "submitted", first="6a", descriptor="not json")
    _revision(con, "6a", "T6", 1, "6a", "not json")
    _revision(con, "6b", "T6", 2, "6a", None)
    # T7: cancelled; its relaunch has no recorded route at all.
    _dispatch(con, "7a", "T7", B, start=t + "13:00:00+00:00", end=t + "13:10:00+00:00")
    _dispatch(con, "7b", "T7", None, end=t + "14:00:00+00:00", pred="7a")
    _task(con, "T7", "cancelled", first="7a", descriptor="{}")
    # The same task id in another run must not merge with T1 above.
    _dispatch(con, "9a", "T1", A, run=R2, size="XL")
    _task(con, "T1", "running", run=R2, first="9a", descriptor=json.dumps({"task_size": "XL", "evidence_domain": "docs"}))
    # Other roles. The planner has only a triple.
    _dispatch(con, "rv", None, ("agy", "gemini-3.8-flash", "medium"), role="reviewer", start=t + "15:00:00+00:00",
              end=t + "15:01:00+00:00", size="S")
    _dispatch(con, "pl", None, None, role="planner", start=t + "07:00:00+00:00", end=t + "07:00:30+00:00",
              triple="claude@2/claude-opus-5-5@max")

    # Convergence lanes. lane-a reviewed twice (rounds are distinct gates), lane-b shares T3 with it, lane-c predates
    # frozen membership, and a task-level gate is not a lane.
    _gate(con, "a1", "lane", "lane-a", ["T2", "T3"])
    _gate(con, "a2", "lane", "lane-a", ["T2", "T3"])
    _gate(con, "b1", "lane", "lane-b", ["T3", "T4"])
    _gate(con, "c1", "lane", "lane-c", None)
    _gate(con, "k1", "task", None, None)
    # X1: nobody declared an owner, one member owns the path. Recorded once per repair owner (two rows).
    _finding(con, "x1", "a1", "X1", "unique-path", "T2", "T2")
    _finding(con, "x2", "a1", "X1", "unique-path", "T2", "T3", clone_of="x1")
    # Y1: shared by both members and unassigned. Two rows.
    _finding(con, "y1", "a1", "Y1", "unassigned", None, "T2")
    _finding(con, "y2", "a1", "Y1", "unassigned", None, "T3", clone_of="y1")
    # X1 raised again in the next round: a new finding on a new gate.
    _finding(con, "x3", "a2", "X1", "unique-path", "T2", "T2")
    _finding(con, "x4", "a2", "X1", "unique-path", "T2", "T3", clone_of="x3")
    # Z1: the reviewer named the owner.
    _finding(con, "z1", "b1", "Z1", "reviewer-declared", "T4", "T4")
    # H1: recorded before attribution and clone_of existed: two rows that only (gate, code) says are one finding.
    _finding(con, "h1", "c1", "H1", None, None, "T2")
    _finding(con, "h2", "c1", "H1", None, None, "T3")
    # A finding that is not convergence-v1 is not a lane finding.
    _finding(con, "o1", "k1", "O1", None, None, "T1", contract=None)

    # Persisted attributions: seven executor rows on two routes, plus one whose dispatch row is gone (pruned).
    for name, route, success, attribution in (("1a", A, 1, "route"), ("2a", A, 0, "mixed"), ("2b", B, 0, "route"),
                                              ("2c", A, 1, "route"), ("3b", A, 1, "route"), ("4a", A, 0, "unknown"),
                                              ("4b", B, 1, "route"), ("gone", A, 0, "environment")):
        _attribution(con, name, route, success, attribution)


def _migrated(path: Path, *, v12: bool = True):
    """An empty current-schema database. With `v12` False the v12 evidence columns are dropped again and the
    schema version rolled back, which is what a database last opened by an older runtime looks like."""
    con = db.connect(path)
    route_learning.ensure_schema(con)  # route_attributions: written by the learner at run close, not by db.connect
    for table, columns in V12_COLUMNS.items():
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        for column in columns:
            name = column.split()[0]
            if v12 and name not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
            elif not v12 and name in have:
                con.execute(f"ALTER TABLE {table} DROP COLUMN {name}")
    if not v12:
        con.execute("UPDATE schema_meta SET value=? WHERE key='office_schema'", (str(db.SCHEMA_VERSION - 1),))
    con.execute("PRAGMA foreign_keys=OFF")
    return con


def _make(path: Path, *, v12: bool = True, legacy_only: bool = False, wal: bool = False) -> Path:
    if legacy_only:
        con = sqlite3.connect(path)
        con.executescript(db.LEGACY_DDL)
    else:
        con = _migrated(path, v12=v12)
    if legacy_only:
        for name, triple, role in (("1", "claude@2/claude-sonnet-5-5@high", "executor"),
                                   ("2", "codex@1/gpt-5-codex@medium", "reviewer")):
            _dispatch(con, name, None, None, role=role, triple=triple, start="2026-09-01T10:00:00+00:00",
                      end="2026-09-01T10:05:00+00:00")
        _insert(con, "runs", id=R1, created_at="2026-08-01T09:00:00+00:00")
    else:
        populate(con)
    con.commit()
    if not wal:
        con.execute("PRAGMA journal_mode=DELETE")
    con.close()
    return path


@pytest.fixture
def new_db(tmp_path):
    return _make(tmp_path / f"{Z}-source.db")


@pytest.fixture
def report(new_db):
    return rer.build_report(new_db)


def sec(report, name):
    return report["sections"][name]


def by_route(rows, role, route, **kw):
    harness, model, effort = route
    hits = [r for r in rows if (r["role"], r["harness"], r["model"], r["effort"]) == (role, harness, model, effort)]
    assert len(hits) == 1, (role, route, rows)
    return hits[0]


# ------------------------------------------------------------------ source: read-only and provenance

def test_source_block_prints_hash_size_integrity_schema_and_run_range(new_db, report):
    src = report["source"]
    raw = new_db.read_bytes()
    assert src["sha256"] == hashlib.sha256(raw).hexdigest() == src["sha256_after"]
    assert src["hash_unchanged"] is True
    assert src["size_bytes"] == len(raw)
    assert src["integrity_check"] == "ok"
    assert src["schema_version"] == db.SCHEMA_VERSION
    assert src["run_date_range"] == {"runs": 2, "first": "2026-09-01", "last": "2026-10-02"}
    assert src["wal_file_present"] is False and src["shm_file_present"] is False
    text = " ".join(src["caveats"])
    assert "WAL" in text or "-wal" in text
    assert "copy" in text and "live" in text


def test_the_source_file_is_unchanged_by_a_report(tmp_path, monkeypatch):
    path = _make(tmp_path / f"{Z}-wal.db", wal=True)
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    for fmt in ("text", "json"):
        assert rer.main(["--db", str(path), "--format", fmt]) == 0
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert rer.build_report(path)["source"]["sha256"] == hashlib.sha256(before[0]).hexdigest()


def test_the_database_is_opened_read_only_and_never_through_office_db(new_db, monkeypatch):
    opened = []
    real = sqlite3.connect

    def spy(target, *a, **kw):
        opened.append((str(target), kw))
        return real(target, *a, **kw)

    def forbidden(*a, **kw):
        raise AssertionError("office.db.connect migrates and enters WAL: the report must not call it")

    monkeypatch.setattr(sqlite3, "connect", spy)
    monkeypatch.setattr(db, "connect", forbidden)
    rer.build_report(new_db)
    assert opened and all("mode=ro" in t and kw.get("uri") is True for t, kw in opened)

    con = rer.open_readonly(new_db)
    try:
        assert con.execute("PRAGMA query_only").fetchone()[0] == 1
        for stmt in ("CREATE TABLE zq9x_new(x)", "UPDATE runs SET phase='x'", "DELETE FROM dispatches"):
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                con.execute(stmt)
    finally:
        con.close()


def test_a_missing_or_foreign_file_is_refused_without_creating_anything(tmp_path, capsys):
    missing = tmp_path / "absent.db"
    assert rer.main(["--db", str(missing)]) == 2
    assert not missing.exists()
    junk = tmp_path / "junk.db"
    junk.write_text("this is not a database " * 50)
    assert rer.main(["--db", str(junk)]) == 2
    assert "cannot read" in capsys.readouterr().err
    assert junk.read_text() == "this is not a database " * 50


def test_a_failed_integrity_check_is_printed_and_exits_1(tmp_path, monkeypatch, capsys):
    path = _make(tmp_path / f"{Z}-broken.db")
    real = rer.open_readonly

    class Con:
        def __init__(self, con):
            self._con = con

        def execute(self, sql, *a):
            if sql == "PRAGMA integrity_check":
                return self._con.execute("SELECT 'row 1 missing from index zq9x_idx'")
            return self._con.execute(sql, *a)

        def __getattr__(self, name):
            return getattr(self._con, name)

    monkeypatch.setattr(rer, "open_readonly", lambda p: Con(real(p)))
    assert rer.main(["--db", str(path), "--format", "json"]) == 1
    printed = json.loads(capsys.readouterr().out)
    assert printed["source"]["integrity_check"] == ["row 1 missing from index zq9x_idx"]


# ------------------------------------------------------------------ aggregate only

@pytest.mark.parametrize("make", [
    lambda p: _make(p),
    lambda p: _make(p, v12=False),
    lambda p: _make(p, legacy_only=True),
], ids=["v12", "old-schema", "legacy-only"])
@pytest.mark.parametrize("fmt", ["text", "json"])
def test_the_output_names_no_id_path_title_summary_or_prompt(tmp_path, capsys, make, fmt):
    path = make(tmp_path / f"{Z}-source.db")
    assert rer.main(["--db", str(path), "--format", fmt]) == 0
    out = capsys.readouterr().out
    assert Z not in out
    assert str(tmp_path) not in out and "/Users/" not in out and "/tmp/" not in out
    if fmt == "json":
        json.loads(out)


def test_unexpected_category_values_are_clamped_not_echoed(report):
    tags = sec(report, "tags")["tasks"]
    assert tags["intent"]["values"] == {"descriptor-null": 1, "descriptor-unreadable": 1, "feature": 1, "fix": 1,
                                        "not-recorded": 3, "other": 1}
    assert "other" in sec(report, "size")["task_size"]["tasks"]
    assert Z not in json.dumps(report)


# ------------------------------------------------------------------ dispatch counts, strict successes, episodes

def test_dispatch_counts_are_exact_per_role_harness_model_effort_with_terminal_and_pending(report):
    rows = sec(report, "dispatches")["routes"]
    a = by_route(rows, "executor", A)
    assert (a["dispatches"], a["terminal"], a["pending"]) == (9, 7, 2)
    b = by_route(rows, "executor", B)
    assert (b["dispatches"], b["terminal"], b["pending"]) == (3, 3, 0)
    unknown = by_route(rows, "executor", ("unknown", "unknown", "unknown"))
    assert (unknown["dispatches"], unknown["terminal"]) == (1, 1)
    assert by_route(rows, "reviewer", ("agy", "gemini-3.8-flash", "medium"))["dispatches"] == 1
    # A dispatch with only a triple is read as route_learning splits it.
    assert by_route(rows, "planner", ("claude", "claude-opus-5-5", "max"))["dispatches"] == 1
    assert sec(report, "dispatches")["totals"] == {"dispatches": 15, "terminal": 13, "pending": 2, "strict_successes": 5}


def test_raw_strict_dispatch_successes_are_counted_apart_from_episodes(report):
    rows = sec(report, "dispatches")["routes"]
    assert by_route(rows, "executor", A)["strict_successes"] == 4
    assert by_route(rows, "executor", B)["strict_successes"] == 1
    assert by_route(rows, "reviewer", ("agy", "gemini-3.8-flash", "medium"))["strict_successes"] is None
    # Executor dispatches that never landed are 0 strict successes, not unknown.
    assert by_route(rows, "executor", ("unknown", "unknown", "unknown"))["strict_successes"] == 0

    episodes = sec(report, "episodes")
    assert episodes["available"]
    a = by_route(episodes["routes"], "executor", A)
    assert (a["settled_dispatches"], a["settled_strict_successes"]) == (7, 4)
    assert (a["episodes"], a["accepted_episodes"], a["failed_episodes"], a["multi_attempt_episodes"]) == (5, 4, 1, 2)
    b = by_route(episodes["routes"], "executor", B)
    assert (b["settled_dispatches"], b["settled_strict_successes"]) == (3, 1)
    assert (b["episodes"], b["accepted_episodes"], b["failed_episodes"]) == (3, 1, 2)
    assert b["failed_by_attribution"]["plan"] == 1 and b["failed_by_attribution"]["unknown"] == 1
    assert episodes["totals"] == {"settled_dispatches": 10, "settled_strict_successes": 5, "episodes": 8,
                                  "accepted_episodes": 5, "failed_episodes": 3}


def test_episode_derivation_writes_nothing(new_db):
    before = hashlib.sha256(new_db.read_bytes()).hexdigest()
    con = rer.open_readonly(new_db)
    try:
        assert rer.route_learning.derive_outcomes(con)
        for stmt in ("INSERT INTO route_attributions VALUES(1,2,3,4,5,6,7,8,9,10)", "CREATE TABLE zq9x_t(x)"):
            with pytest.raises(sqlite3.OperationalError):
                con.execute(stmt)
    finally:
        con.close()
    assert hashlib.sha256(new_db.read_bytes()).hexdigest() == before


# ------------------------------------------------------------------ task paths

def test_first_executor_vs_accepted_producer(report):
    paths = sec(report, "task_paths")["accepted"]
    assert paths == {
        "accepted_tasks": 5, "first_executor_unknown": 1, "producer_unknown": 0, "comparable": 4,
        "first_executor_is_producer": 1, "later_dispatch_same_route": 2, "first_final_mismatch": 1, "route_unknown": 0}


def test_same_route_retries_cross_route_handoffs_and_swap_away_and_back(report):
    handoffs = {r["task_state"]: r for r in sec(report, "task_paths")["handoffs_by_task_state"]}
    accepted = handoffs["accepted"]
    assert accepted["tasks"] == 5
    # T5 has an executor dispatch and no recorded first executor: its handoffs are unknown, the other four are known.
    assert (accepted["tasks_links_recorded"], accepted["tasks_links_unknown"]) == (4, 1)
    assert (accepted["same_route_retries"], accepted["tasks_with_same_route_retry"]) == (1, 1)
    assert (accepted["cross_route_handoffs"], accepted["tasks_with_cross_route_handoff"]) == (3, 2)
    # T2's A -> B is intermediate; its B -> A and T4's A -> B land on the accepted producer.
    assert accepted["intermediate_cross_route_handoffs"] == 1
    # T2 left A and came back: first and final routes match, yet it was handed off twice.
    assert accepted["swap_away_and_back_tasks"] == 1
    assert accepted["route_unknown_handoffs"] == 0
    unresolved = handoffs["unresolved"]
    assert unresolved["tasks"] == 3 and unresolved["tasks_links_unknown"] == 0
    assert (unresolved["cross_route_handoffs"], unresolved["swap_away_and_back_tasks"]) == (0, 0)
    assert unresolved["route_unknown_handoffs"] == 1  # T7's relaunch has no recorded route


def test_executor_dispatch_links_separate_first_linked_and_unknown(report):
    assert sec(report, "task_paths")["executor_dispatch_links"] == {
        "executor_dispatches": 13, "first": 7, "linked": 5, "unknown": 1}


def test_revisions_to_accept_over_accepted_tasks_with_unresolved_shown_apart(report):
    paths = sec(report, "task_paths")
    accepted = paths["revisions_to_accept"]
    assert accepted == {"tasks": 5, "revision_row_missing": 0, "mean": 1.8, "median": 2.0,
                        "histogram": {"1": 2, "2": 2, "3": 1}}
    unresolved = paths["unresolved_revisions_so_far"]
    assert unresolved["tasks"] == 3
    assert unresolved["histogram"] == {"0": 2, "2": 1}
    # The unresolved tasks do not move the accepted mean.
    assert accepted["mean"] == round((1 + 3 + 2 + 2 + 1) / 5, 3)


def test_a_task_id_shared_by_two_runs_is_not_merged(report):
    status = sec(report, "population")["tasks_by_status"]
    assert status == {"accepted": 5, "cancelled": 1, "running": 1, "submitted": 1}
    assert sec(report, "population")["runs_by_phase"] == {"active": 1, "closed": 1}


# ------------------------------------------------------------------ self-review

def test_self_review_receipts_are_split_into_ledger_exemption_and_missing(report):
    rows = {(r["kind"], r["exempt_type"]): r for r in sec(report, "self_review")["by_receipt"]}
    cell = lambda k, t=None: (rows[(k, t)]["revisions"], rows[(k, t)]["accepted_revisions"])  # noqa: E731
    assert cell("ledger") == (4, 3)
    assert cell("exempt", "mechanical") == (1, 0)
    assert cell("exempt", "trivial") == (1, 1)
    assert cell("exempt", "empty") == (1, 0)
    assert cell("missing") == (3, 1)
    assert cell("unreadable") == (1, 0)
    assert sec(report, "self_review")["totals"] == {"revisions": 11, "accepted_revisions": 5}


# ------------------------------------------------------------------ lanes and findings

def test_lane_exposure_counts_distinct_lane_task_pairs_and_unknown_membership(report):
    assert sec(report, "findings")["lanes"] == {
        "lane_gates": 4, "membership_unknown_gates": 1, "lanes": 3, "lane_task_pairs": 4, "exposed_tasks": 3,
        "tasks_in_several_lanes": 1}


def test_findings_count_once_per_gate_and_code_not_once_per_repair_owner(report):
    findings = sec(report, "findings")
    by = {r["basis"]: r for r in findings["by_attribution"]}
    assert by["unique-path"] == {"basis": "unique-path", "unique_findings": 2, "recorded_rows": 4, "duplicate_rows": 2,
                                 "clone_marked_rows": 2, "lane_exposure": 4, "membership_unknown": 0}
    assert by["unassigned"]["unique_findings"] == 1 and by["unassigned"]["duplicate_rows"] == 1
    assert by["reviewer-declared"]["unique_findings"] == 1 and by["reviewer-declared"]["duplicate_rows"] == 0
    # Recorded before attribution and clone_of existed: still one finding, attribution unknown, membership unknown.
    assert by["unknown"] == {"basis": "unknown", "unique_findings": 1, "recorded_rows": 2, "duplicate_rows": 1,
                             "clone_marked_rows": 0, "lane_exposure": 0, "membership_unknown": 1}
    assert findings["totals"] == {"unique_findings": 5, "recorded_rows": 9, "duplicate_rows": 4, "lane_exposure": 8}
    assert (findings["attributed_findings"], findings["unassigned_findings"], findings["attribution_unknown_findings"]) == (3, 1, 1)
    # A finding in a shared reviewed scope exposes every member; attribution names at most one.
    assert findings["totals"]["lane_exposure"] > findings["attributed_findings"]


# ------------------------------------------------------------------ latency, cost, size, tags

def test_latency_is_reported_where_recorded_and_unknown_elsewhere(report):
    rows = sec(report, "latency_and_cost")["latency"]
    a = by_route(rows, "executor", A)
    # recorded: 3a wall 40 (beats its 60s timestamps), 1a 100, 2c 300, 3b 300, 2a 600; 4a has no start, 5a ends before it starts.
    assert (a["terminal"], a["recorded"], a["unknown"]) == (7, 5, 2)
    assert (a["mean_seconds"], a["median_seconds"], a["max_seconds"]) == (268.0, 300.0, 600.0)
    b = by_route(rows, "executor", B)
    assert (b["recorded"], b["median_seconds"], b["mean_seconds"], b["max_seconds"]) == (3, 900.0, 900.0, 1200.0)
    unknown = by_route(rows, "executor", ("unknown", "unknown", "unknown"))
    assert (unknown["recorded"], unknown["unknown"], unknown["median_seconds"]) == (0, 1, "unknown")


def test_an_even_count_takes_the_mean_of_the_two_middle_latencies(tmp_path):
    con = db.connect(tmp_path / "even.db")
    for i, secs in enumerate((10, 20, 30, 100)):
        _dispatch(con, f"e{i}", None, A, start="2026-09-01T10:00:00+00:00",
                  end=f"2026-09-01T10:{secs // 60:02d}:{secs % 60:02d}+00:00")
    con.commit()
    con.close()
    row = by_route(sec(rer.build_report(tmp_path / "even.db"), "latency_and_cost")["latency"], "executor", A)
    assert (row["median_seconds"], row["mean_seconds"]) == (25.0, 40.0)


def test_missing_cost_is_unknown_never_zero(report):
    rows = sec(report, "latency_and_cost")["cost"]
    a = by_route(rows, "executor", A)
    assert (a["terminal"], a["recorded"], a["unknown"], a["total"]) == (7, 2, 5, 2.0)
    b = by_route(rows, "executor", B)
    assert (b["recorded"], b["unknown"], b["total"]) == (1, 2, 3.0)
    assert by_route(rows, "executor", ("unknown", "unknown", "unknown"))["total"] == "unknown"
    assert by_route(rows, "reviewer", ("agy", "gemini-3.8-flash", "medium"))["total"] == "unknown"


def test_task_size_run_size_and_the_launch_snapshot_are_labelled_apart(report):
    size = sec(report, "size")
    assert size["task_size"]["tasks"] == {"L": 1, "M": 1, "S": 1, "XL": 1, "other": 1, "unknown": 3}
    assert size["run_size"]["runs"] == {"L": 1, "unknown": 1}
    snap = size["dispatch_task_size_snapshot"]
    assert snap["dispatches"] == {"L": 2, "M": 1, "XL": 1, "unknown": 9}
    a = next(r for r in snap["by_route"] if (r["harness"], r["model"], r["effort"]) == A)
    assert a["sizes"] == {"L": 1, "M": 1, "XL": 1, "unknown": 6}
    assert len({size["task_size"]["label"], snap["label"], size["run_size"]["label"]}) == 3
    # The run is L but only one task is: a run size is never read as a task size.
    assert size["task_size"]["tasks"]["L"] == 1 and size["run_size"]["runs"]["L"] == 1


def test_tag_coverage_counts_declared_apart_from_unknown(report):
    tags = sec(report, "tags")
    domain = tags["tasks"]["evidence_domain"]
    assert domain == {"declared": 3, "unknown_not_recorded": 5,
                      "values": {"backend": 2, "descriptor-null": 1, "descriptor-unreadable": 1, "docs": 1,
                                 "not-recorded": 3}}
    # An explicit `unknown` is a declared value; a missing key and a NULL descriptor are not.
    difficulty = tags["tasks"]["difficulty_estimate"]
    assert difficulty["declared"] == 2 and difficulty["values"]["unknown"] == 1
    assert difficulty["values"]["descriptor-null"] == 1
    assert tags["tasks"]["brief_shape"]["declared"] == 1
    dispatch = tags["executor_dispatches"]["evidence_domain"]
    assert (dispatch["declared"], dispatch["unknown_not_recorded"]) == (1, 12)
    assert dispatch["values"] == {"backend": 1, "descriptor-null": 11, "not-recorded": 1}


# ------------------------------------------------------------------ old schema, unknowns, legacy

def test_an_old_schema_reads_every_missing_evidence_column_as_unknown(tmp_path):
    path = _make(tmp_path / f"{Z}-old.db", v12=False)
    report = rer.build_report(path)
    gaps = set(report["schema_gaps_read_as_unknown"])
    assert {"dispatches.predecessor_dispatch_id", "tasks.first_executor_dispatch_id", "findings.clone_of",
            "findings.attribution_basis", "gates.members_json"} <= gaps
    paths = sec(report, "task_paths")
    assert paths["accepted"]["accepted_tasks"] == 5 and paths["accepted"]["first_executor_unknown"] == 5
    assert paths["accepted"]["comparable"] == 0
    # No link column exists: first, linked and every handoff figure are unknown, never 0.
    assert paths["executor_dispatch_links"] == {"executor_dispatches": 13, "first": "unknown", "linked": "unknown",
                                                "unknown": 13}
    for key in rer.FIRST_EXECUTOR_COUNTS:
        assert paths["accepted"][key] == "unknown", key
    for row in paths["handoffs_by_task_state"]:
        assert (row["tasks_links_recorded"], row["tasks_links_unknown"]) == (0, row["tasks"])
        for key in rer.HANDOFF_COUNTS:
            assert row[key] == "unknown", (row["task_state"], key)
    # The recorded links stay unread; the chronological section is where an old database's paths are reported.
    chrono = paths["chronological_derived"]
    assert chrono["label"] == "chronological (derived from dispatches.started_at, not a recorded link)"
    assert {k: v for k, v in chrono.items() if k != "label"} == CHRONOLOGICAL_POPULATED
    # The accepted producer still comes from accepted_revision_id.
    assert paths["revisions_to_accept"]["histogram"] == {"1": 2, "2": 2, "3": 1}
    findings = sec(report, "findings")
    assert findings["lanes"]["membership_unknown_gates"] == 4 and findings["lanes"]["lane_task_pairs"] == 0
    assert [r["basis"] for r in findings["by_attribution"]] == ["unknown"]
    # Duplicate repair rows still count once: (gate, code) does not need clone_of.
    assert findings["totals"] == {"unique_findings": 5, "recorded_rows": 9, "duplicate_rows": 4, "lane_exposure": 0}
    assert sec(report, "dispatches")["totals"]["strict_successes"] == 5
    assert sec(report, "episodes")["totals"]["accepted_episodes"] == 5


def test_the_current_schema_without_v12_columns_matches_the_populated_one_where_both_know(new_db, tmp_path):
    old = rer.build_report(_make(tmp_path / f"{Z}-old.db", v12=False))
    new = rer.build_report(new_db)
    for name in ("dispatches", "episodes", "route_attributions", "self_review", "latency_and_cost", "tags"):
        assert sec(old, name) == sec(new, name), name
    assert new["schema_gaps_read_as_unknown"] == []


def test_a_3_0_recorder_database_reports_what_it_has_and_marks_the_rest_unavailable(tmp_path):
    report = rer.build_report(_make(tmp_path / f"{Z}-legacy.db", legacy_only=True))
    assert report["source"]["schema_version"] == "absent"
    dispatches = sec(report, "dispatches")
    assert dispatches["available"]
    assert dispatches["totals"] == {"dispatches": 2, "terminal": 2, "pending": 0, "strict_successes": None}
    assert by_route(dispatches["routes"], "executor", A)["strict_successes"] is None
    assert by_route(dispatches["routes"], "reviewer", B)["dispatches"] == 1
    for name in ("task_paths", "self_review", "findings"):
        assert sec(report, name)["available"] is False, name
    # A missing tasks table hides only what needs it: the run range, run size and dispatch tags stay readable.
    assert report["source"]["run_date_range"] == {"runs": 1, "first": "2026-08-01", "last": "2026-08-01"}
    assert sec(report, "population")["tasks_by_status"] is None
    assert sec(report, "size")["task_size"] == {"label": sec(report, "size")["task_size"]["label"], "available": False}
    assert sec(report, "size")["run_size"]["runs"] == {"unknown": 1}
    assert sec(report, "tags")["tasks"] == {"available": False}
    assert sec(report, "tags")["executor_dispatches"]["intent"]["declared"] == 0
    assert sec(report, "episodes")["available"] is False
    assert {"tasks", "revisions", "gates"} <= set(report["schema_gaps_read_as_unknown"])


def test_an_empty_migrated_database_reports_zeros_not_errors(tmp_path):
    con = db.connect(tmp_path / "empty.db")
    con.close()
    report = rer.build_report(tmp_path / "empty.db")
    assert sec(report, "dispatches")["totals"]["dispatches"] == 0
    assert sec(report, "task_paths")["accepted"]["accepted_tasks"] == 0
    assert sec(report, "task_paths")["revisions_to_accept"]["mean"] is None
    assert report["source"]["run_date_range"] == {"runs": 0, "first": None, "last": None}
    assert sec(report, "latency_and_cost")["latency"] == []


# ------------------------------------------------------------------ chronological paths and persisted attributions

def _chrono(report):
    return {k: v for k, v in sec(report, "task_paths")["chronological_derived"].items() if k != "label"}


def test_the_chronological_section_is_labelled_and_matches_the_scenario(report):
    section = sec(report, "task_paths")["chronological_derived"]
    assert section["label"] == "chronological (derived from dispatches.started_at, not a recorded link)"
    assert _chrono(report) == CHRONOLOGICAL_POPULATED


def test_chronological_paths_are_reported_where_recorded_links_are_unknown(tmp_path):
    """The old-data failure: recorded handoffs printed 0 while the dispatches showed cross-route handoffs."""
    old = rer.build_report(_make(tmp_path / f"{Z}-old.db", v12=False))
    paths = sec(old, "task_paths")
    assert all(r["cross_route_handoffs"] == "unknown" for r in paths["handoffs_by_task_state"])
    assert _chrono(old)["cross_route_handoffs"] == 2 and _chrono(old)["tasks_with_cross_route_handoff"] == 1
    # Every chronological figure is derived without the link columns, so the populated database agrees.
    assert _chrono(old) == _chrono(rer.build_report(_make(tmp_path / f"{Z}-new.db")))


def _accepted_pair(con, first, second, *, start_first, start_second, producer="b", task="T1"):
    _insert(con, "runs", id=R1, created_at="2026-09-01T09:00:00+00:00", phase="closed")
    _dispatch(con, "a", task, first, start=start_first, end="2026-09-01T12:00:00+00:00")
    _dispatch(con, "b", task, second, start=start_second, end="2026-09-01T12:00:00+00:00")
    _task(con, task, "accepted", rev=producer, producer=producer)
    _revision(con, producer, task, 1, producer, LEDGER)
    con.commit()
    con.close()


def test_a_chronological_first_final_mismatch_and_intermediate_handoff(tmp_path):
    _accepted_pair(_mini(tmp_path, v12=False), A, B, start_first="2026-09-01T10:00:00+00:00",
                   start_second="2026-09-01T11:00:00+00:00")
    chrono = _chrono(rer.build_report(tmp_path / "mini.db"))
    assert (chrono["first_final_mismatch"], chrono["accepted_from_first_dispatch"], chrono["comparable"]) == (1, 0, 1)
    # The only handoff lands on the accepted producer, so it is not intermediate.
    assert (chrono["cross_route_handoffs"], chrono["intermediate_cross_route_handoffs"]) == (1, 0)


def test_a_producer_that_ran_first_leaves_the_later_dispatch_as_an_intermediate_handoff(tmp_path):
    _accepted_pair(_mini(tmp_path, v12=False), A, B, start_first="2026-09-01T10:00:00+00:00",
                   start_second="2026-09-01T11:00:00+00:00", producer="a")
    chrono = _chrono(rer.build_report(tmp_path / "mini.db"))
    assert (chrono["accepted_from_first_dispatch"], chrono["intermediate_cross_route_handoffs"]) == (1, 1)
    assert chrono["tasks_with_intermediate_cross_route_handoff"] == 1


@pytest.mark.parametrize("second_start", [None, "2026-09-01T10:00:00+00:00", "not a time", "11:00:00", 5])
def test_an_order_that_start_times_do_not_settle_is_unknown_not_guessed(tmp_path, second_start):
    """A missing, tied or unparseable started_at: no first executor and no handoff is inferred."""
    _accepted_pair(_mini(tmp_path, v12=False), A, B, start_first="2026-09-01T10:00:00+00:00", start_second=second_start)
    chrono = _chrono(rer.build_report(tmp_path / "mini.db"))
    assert (chrono["order_unknown"], chrono["comparable"]) == (1, 0)
    assert chrono["cross_route_handoffs"] == 0 and chrono["accepted_from_first_dispatch"] == 0
    assert chrono["first_final_mismatch"] == 0 and chrono["later_dispatch_same_route"] == 0


def test_a_chronological_route_with_an_unknown_part_is_unknown_not_a_mismatch(tmp_path):
    _accepted_pair(_mini(tmp_path), ("claude", "m", None), ("claude", "m", "high"),
                   start_first="2026-09-01T10:00:00+00:00", start_second="2026-09-01T11:00:00+00:00")
    chrono = _chrono(rer.build_report(tmp_path / "mini.db"))
    assert (chrono["first_final_mismatch"], chrono["route_unknown"], chrono["route_unknown_handoffs"]) == (0, 1, 1)
    assert chrono["cross_route_handoffs"] == 0


def test_chronological_orders_do_not_cross_runs_that_share_a_task_id(tmp_path):
    con = _mini(tmp_path, v12=False)
    _accepted_pair(con, A, A, start_first="2026-09-01T10:00:00+00:00", start_second="2026-09-01T11:00:00+00:00")
    con = _mini(tmp_path, name="other.db", v12=False)
    _insert(con, "runs", id=R1, created_at="2026-09-01T09:00:00+00:00", phase="closed")
    _insert(con, "runs", id=R2, created_at="2026-09-01T09:00:00+00:00", phase="closed")
    _dispatch(con, "a", "T1", A, start="2026-09-01T10:00:00+00:00", end="2026-09-01T10:30:00+00:00")
    _dispatch(con, "b", "T1", B, run=R2, start="2026-09-01T10:10:00+00:00", end="2026-09-01T10:30:00+00:00")
    _task(con, "T1", "accepted", rev="a", producer="a")
    _revision(con, "a", "T1", 1, "a", LEDGER)
    con.commit()
    con.close()
    chrono = _chrono(rer.build_report(tmp_path / "other.db"))
    assert (chrono["accepted_from_first_dispatch"], chrono["cross_route_handoffs"]) == (1, 0)


def test_a_task_with_no_executor_dispatch_has_recorded_links_and_no_handoffs(tmp_path):
    con = _mini(tmp_path)
    _insert(con, "runs", id=R1, created_at="2026-09-01T09:00:00+00:00", phase="active")
    _task(con, "T1", "pending")
    con.commit()
    con.close()
    row = {r["task_state"]: r for r in sec(rer.build_report(tmp_path / "mini.db"), "task_paths")["handoffs_by_task_state"]}
    assert (row["unresolved"]["tasks_links_recorded"], row["unresolved"]["cross_route_handoffs"]) == (1, 0)


def test_persisted_route_attributions_are_reported_per_exact_route_and_apart_from_strict_success(report):
    section = sec(report, "route_attributions")
    assert section["available"] and "not the revision-derived strict success" in section["label"]
    rows = {(r["role"], r["harness"], r["model"], r["effort"]): r for r in section["routes"]}
    executor_a = rows[("executor", *A)]
    assert (executor_a["attributions"], executor_a["successes"]) == (5, 3)
    assert executor_a["by_attribution"] == {"route": 3, "mixed": 1, "unknown": 1, "plan": 0, "environment": 0, "reviewer": 0}
    executor_b = rows[("executor", *B)]
    assert (executor_b["attributions"], executor_b["successes"]) == (2, 1)
    assert executor_b["by_attribution"]["route"] == 2
    # The dispatch row of this attribution is gone, so its role is unknown; it is not folded into the executor route.
    gone = rows[("unknown", *A)]
    assert (gone["attributions"], gone["successes"], gone["by_attribution"]["environment"]) == (1, 0, 1)
    assert section["totals"] == {"attributions": 8, "successes": 4}
    # The two measures are different: persisted success is 3 for executor route A, revision-derived strict success is 4.
    strict = by_route(sec(report, "dispatches")["routes"], "executor", A)["strict_successes"]
    assert strict == 4 != executor_a["successes"]


def test_route_attributions_merge_clamped_routes_and_never_echo_free_text(tmp_path):
    con = _mini(tmp_path)
    for name, route, attribution in (("x", f"/Users/{Z}/key", f"sk-ant-{Z}"), ("w", f"/Users/{Z}/other", "route"),
                                     ("y", f"tok-{Z}\n/m@high", "mixed"), ("z", "", "plan")):
        _insert(con, "route_attributions", dispatch_id=did(name), run_id=R1, route=route, success=1, attribution=attribution,
                confidence=1.0, learn_weight=1.0, learner_version="1", derived_at="2026-10-01T00:00:00+00:00")
    con.commit()
    con.close()
    text, as_json = _printed(tmp_path / "mini.db")
    assert Z not in text and Z not in as_json
    section = sec(rer.build_report(tmp_path / "mini.db"), "route_attributions")
    rows = {(r["harness"], r["model"], r["effort"]): r for r in section["routes"]}
    # Two different free-text routes clamp to the same key and are one row; an unrecognised class is `other`.
    merged = rows[("other", "other", "other")]
    assert (merged["attributions"], merged["successes"]) == (2, 2)
    assert merged["by_attribution"]["other"] == 1 and merged["by_attribution"]["route"] == 1
    assert rows[("other", "m", "high")]["by_attribution"]["mixed"] == 1
    assert rows[("other", "other", "unknown")]["by_attribution"]["plan"] == 1


def test_an_absent_route_attributions_table_makes_only_that_section_unavailable(tmp_path):
    con = _mini(tmp_path)
    con.execute("DROP TABLE route_attributions")
    con.commit()
    con.close()
    report = rer.build_report(tmp_path / "mini.db")
    assert sec(report, "route_attributions") == {"available": False, "missing_tables": ["route_attributions"]}
    assert sec(report, "dispatches")["available"] and "route_attributions" in report["schema_gaps_read_as_unknown"]


# ------------------------------------------------------------------ command line

def test_cli_prints_text_and_json(new_db, capsys):
    assert rer.main(["--db", str(new_db)]) == 0
    text = capsys.readouterr().out
    assert "sha256:" in text and "integrity_check: ok" in text and "dispatches" in text and "unknown" in text
    assert rer.main(["--db", str(new_db), "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["report"] == "routing-evidence"


def test_the_script_runs_as_a_program(new_db):
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "routing_evidence_report.py"), "--db", str(new_db),
                           "--format", "json"], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["source"]["hash_unchanged"] is True


# ------------------------------------------------------------------ the documentation lists what runs

def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


FULL_SCHEMA = {**{t: set(c) | set(rer.EXTRA_COLUMNS.get(t, ())) for t, c in rer.VIEWS.items()},
               "schema_meta": {"key", "value"}, "outcome_labels": {"dispatch_id"}}


def test_the_docs_list_every_metric_sql_and_denominator_verbatim():
    doc = _norm(DOC.read_text())
    assert _norm(rer.prelude(FULL_SCHEMA)) in doc
    for metric_id, metric in rer.METRICS.items():
        assert f"`{metric_id}`" in doc, metric_id
        assert _norm(metric["sql"]) in doc, metric_id
        assert _norm(metric["denominator"]) in doc, metric_id
    for ident in re.findall(r"^### `([\w.]+)`", DOC.read_text(), re.M):
        assert ident in rer.METRICS or ident in ("provenance", "episodes"), ident


def test_every_metric_runs_on_a_full_schema_and_each_section_uses_only_documented_metrics(new_db):
    con = rer.open_readonly(new_db)
    try:
        schema = rer.read_schema(con)
        assert set(rer.VIEWS) <= set(schema)
        for metric_id in rer.METRICS:
            assert isinstance(rer._rows(con, schema, metric_id), list), metric_id
    finally:
        con.close()


# ------------------------------------------------------------------ findings from the self-review round

def _mini(tmp_path, name="mini.db", **kw) -> sqlite3.Connection:
    return _migrated(tmp_path / name, **kw)


def _printed(path) -> tuple[str, str]:
    """The report as text and as JSON, for one database."""
    report = rer.build_report(path)
    return rer.render_text(report), json.dumps(report)


def test_free_text_in_route_phase_and_status_columns_never_reaches_the_output(tmp_path):
    con = _mini(tmp_path)
    _insert(con, "runs", id=R1, created_at="2026-09-01T09:00:00+00:00", phase=f"Tok {Z}\n\x1b[31m")
    _insert(con, "dispatches", id=did("x"), run_id=R1, role=f"/etc/{Z}", harness=f"/Users/{Z}/.ssh/id_rsa",
            model=f"sk-ant-{Z}/Users/rico/x", effort=f"\x1b]0;{Z}\x07", task_id="T1",
            started_at="2026-09-01T10:00:00+00:00", ended_at="2026-09-01T10:01:00+00:00")
    _insert(con, "dispatches", id=did("y"), run_id=R1, role="executor", harness="claude", model="claude-sonnet-5-5",
            effort="high", ended_at="2026-09-01T10:01:00+00:00")
    _insert(con, "tasks", run_id=R1, id="T1", title="t", role="executor", scope_json="[]", depends_json="[]",
            accept_json="[]", checks_json="[]", status=f"{Z} done", introduced_plan_version=1, contract_version=1,
            acceptance_version=1, created_at="x", updated_at="x")
    con.execute("UPDATE runs SET risk_json=?", (b"\xff\x00",))  # a BLOB where text belongs
    con.commit()
    con.close()
    text, as_json = _printed(tmp_path / "mini.db")
    for out in (text, as_json):
        assert Z not in out and "\x1b" not in out and "/Users/" not in out
    rows = sec(rer.build_report(tmp_path / "mini.db"), "dispatches")["routes"]
    assert by_route(rows, "other", ("other", "other", "other"))["dispatches"] == 1
    assert by_route(rows, "executor", ("claude", "claude-sonnet-5-5", "high"))["dispatches"] == 1
    assert sec(rer.build_report(tmp_path / "mini.db"), "population")["runs_by_phase"] == {"other": 1}
    assert sec(rer.build_report(tmp_path / "mini.db"), "population")["tasks_by_status"] == {"other": 1}


def test_a_malformed_row_degrades_one_section_and_never_the_exit_code(tmp_path, capsys):
    con = _mini(tmp_path)
    # Valid JSON that is not an object: route_learning's size lookup raises AttributeError on it.
    _insert(con, "runs", id=R1, created_at="2026-09-01T09:00:00+00:00", phase="closed", risk_json="[1]")
    _dispatch(con, "a", "T1", A, start="2026-09-01T10:00:00+00:00", end="2026-09-01T10:01:00+00:00")
    # A start time that is an integer next to text ones: comparing them raises TypeError in the learner.
    _dispatch(con, "b", "T1", A, start=5, end="2026-09-01T10:02:00+00:00")
    con.commit()
    con.close()
    report = rer.build_report(tmp_path / "mini.db")
    episodes = sec(report, "episodes")
    assert episodes["available"] is False and episodes["reason"].endswith("while deriving outcomes")
    assert Z not in json.dumps(episodes)
    assert sec(report, "dispatches")["totals"]["dispatches"] == 2  # the other sections are untouched
    assert rer.main(["--db", str(tmp_path / "mini.db"), "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["sections"]["episodes"]["available"] is False


def test_an_unexpected_failure_exits_2_without_a_traceback(tmp_path, monkeypatch, capsys):
    path = _make(tmp_path / f"{Z}-boom.db")

    def boom(*a, **kw):
        raise ZeroDivisionError(f"quotes a value {Z}")
    monkeypatch.setattr(rer, "read_schema", boom)
    assert rer.main(["--db", str(path)]) == 2
    err = capsys.readouterr().err
    assert err.strip() == "routing_evidence_report: internal error (ZeroDivisionError)"


def test_lane_membership_counts_only_arrays_of_text_ids_and_the_two_lane_metrics_agree(tmp_path):
    con = _mini(tmp_path)
    for name, scope, members in (("g1", "s1", '{"a": "T1", "b": "T2"}'), ("g2", "s2", '"T9"'),
                                 ("g3", "s3", '["T1", "T1", "T3"]'), ("g4", "s4", '["T4", 5, null, ["x"]]'),
                                 ("g5", None, '["T1"]'), ("g6", "s6", "{not json")):
        _insert(con, "gates", id=f"gate-{Z}-{name}", run_id=R1, subject="lane", scope=scope, kind="code_review",
                input_key=Z, status="done", created_at="x", members_json=members)
        _finding(con, name, name, "C1", "unassigned", None, "T1")
    con.commit()
    con.close()
    findings = sec(rer.build_report(tmp_path / "mini.db"), "findings")
    # Objects, scalars and malformed JSON are unknown membership; duplicates and non-text entries do not count.
    assert findings["lanes"] == {"lane_gates": 6, "membership_unknown_gates": 3, "lanes": 6, "lane_task_pairs": 4,
                                 "exposed_tasks": 3, "tasks_in_several_lanes": 1}
    only = findings["by_attribution"][0]
    assert (only["unique_findings"], only["lane_exposure"], only["membership_unknown"]) == (6, 4, 3)


def test_a_route_with_an_unknown_effort_is_unknown_not_a_mismatch(tmp_path):
    con = _mini(tmp_path)
    _insert(con, "runs", id=R1, created_at="2026-09-01T09:00:00+00:00", phase="closed")
    _dispatch(con, "a", "T1", ("claude", "m", None), start="2026-09-01T10:00:00+00:00", end="2026-09-01T10:01:00+00:00")
    _dispatch(con, "b", "T1", ("claude", "m", "high"), start="2026-09-01T11:00:00+00:00",
              end="2026-09-01T11:01:00+00:00", pred="a")
    _task(con, "T1", "accepted", first="a", rev="b", producer="b")
    _revision(con, "b", "T1", 1, "b", LEDGER)
    con.commit()
    con.close()
    paths = sec(rer.build_report(tmp_path / "mini.db"), "task_paths")
    assert paths["accepted"]["first_final_mismatch"] == 0 and paths["accepted"]["route_unknown"] == 1
    accepted = {r["task_state"]: r for r in paths["handoffs_by_task_state"]}["accepted"]
    assert (accepted["cross_route_handoffs"], accepted["same_route_retries"], accepted["route_unknown_handoffs"]) == (0, 0, 1)


def test_a_dispatch_is_never_its_own_predecessor(tmp_path):
    con = _mini(tmp_path)
    _insert(con, "runs", id=R1, created_at="2026-09-01T09:00:00+00:00", phase="closed")
    _dispatch(con, "a", "T1", A, start="2026-09-01T10:00:00+00:00", end="2026-09-01T10:01:00+00:00")
    _dispatch(con, "b", "T1", A, start="2026-09-01T11:00:00+00:00", end="2026-09-01T11:01:00+00:00", pred="b")
    _task(con, "T1", "submitted", first="a")
    con.commit()
    con.close()
    paths = sec(rer.build_report(tmp_path / "mini.db"), "task_paths")
    assert paths["executor_dispatch_links"] == {"executor_dispatches": 2, "first": 1, "linked": 0, "unknown": 1}
    unresolved = {r["task_state"]: r for r in paths["handoffs_by_task_state"]}["unresolved"]
    assert (unresolved["tasks_links_recorded"], unresolved["same_route_retries"]) == (1, 0)


def test_an_accepted_revision_that_belongs_to_another_task_is_not_credited(tmp_path):
    con = _mini(tmp_path)
    _insert(con, "runs", id=R1, created_at="2026-09-01T09:00:00+00:00", phase="closed")
    _dispatch(con, "a", "T2", A, start="2026-09-01T10:00:00+00:00", end="2026-09-01T10:01:00+00:00")
    _task(con, "T1", "accepted", rev="x")       # points at T2's revision
    _task(con, "T2", "submitted", first="a")
    _revision(con, "x", "T2", 1, "a", LEDGER)
    con.commit()
    con.close()
    report = rer.build_report(tmp_path / "mini.db")
    paths = sec(report, "task_paths")
    assert paths["accepted"]["accepted_tasks"] == 1 and paths["accepted"]["producer_unknown"] == 1
    assert paths["revisions_to_accept"]["revision_row_missing"] == 1
    assert sec(report, "dispatches")["totals"]["strict_successes"] == 0


def test_a_revision_and_its_dispatch_must_both_belong_to_the_accepted_task(tmp_path):
    con = _mini(tmp_path)
    _insert(con, "runs", id=R1, created_at="2026-09-01T09:00:00+00:00", phase="closed")
    for name in ("a", "b"):
        _dispatch(con, name, name.upper(), A, start="2026-09-01T10:00:00+00:00", end="2026-09-01T10:01:00+00:00")
    # T-A is accepted on a revision filed under T-B (its dispatch is T-A's); T-B on a revision filed under T-B
    # whose dispatch belongs to T-A.
    _task(con, "A", "accepted", first="a", rev="ra")
    _task(con, "B", "accepted", first="b", rev="rb")
    _revision(con, "ra", "B", 1, "a", LEDGER)
    _revision(con, "rb", "B", 1, "a", LEDGER)
    con.commit()
    con.close()
    report = rer.build_report(tmp_path / "mini.db")
    assert sec(report, "dispatches")["totals"]["strict_successes"] == 0
    assert sec(report, "task_paths")["accepted"]["producer_unknown"] == 2


def test_a_section_that_fails_is_unavailable_and_the_rest_of_the_report_stands(new_db, monkeypatch):
    def boom(con, schema):
        raise ValueError(f"quotes a database value {Z}")
    monkeypatch.setattr(rer, "_latency_cost_section", boom)
    report = rer.build_report(new_db)
    assert sec(report, "latency_and_cost") == {"available": False, "reason": "ValueError while reading this section"}
    assert Z not in json.dumps(report)
    assert sec(report, "dispatches")["totals"]["dispatches"] == 15


def test_a_database_without_the_receipt_column_reports_unknown_not_missing(tmp_path):
    con = _mini(tmp_path)
    _insert(con, "runs", id=R1, created_at="2026-09-01T09:00:00+00:00", phase="closed")
    _dispatch(con, "a", "T1", A)
    _task(con, "T1", "accepted", rev="a", first="a", producer="a")
    _revision(con, "a", "T1", 1, "a", None)
    con.execute("ALTER TABLE revisions DROP COLUMN self_review_json")
    con.commit()
    con.close()
    receipts = sec(rer.build_report(tmp_path / "mini.db"), "self_review")["by_receipt"]
    assert receipts == [{"kind": "unknown", "exempt_type": None, "revisions": 1, "accepted_revisions": 1}]


def test_a_path_with_a_leading_double_slash_opens(new_db):
    report = rer.build_report(Path("/" + str(new_db)))
    assert report["source"]["hash_unchanged"] is True and report["source"]["integrity_check"] == "ok"


def test_side_files_are_reported_as_found_not_as_the_report_left_them(tmp_path):
    path = _make(tmp_path / f"{Z}-wal.db", wal=True)
    assert not Path(f"{path}-wal").exists() and not Path(f"{path}-shm").exists()
    report = rer.build_report(path)
    assert report["source"]["wal_file_present"] is False and report["source"]["shm_file_present"] is False
    left = Path(f"{path}-wal").exists() or Path(f"{path}-shm").exists()
    assert report["source"]["side_files_created_by_report"] is left


def test_a_sqlite_without_json1_or_window_functions_is_named(tmp_path, monkeypatch, capsys):
    class Old:
        def execute(self, sql, *a):
            raise sqlite3.OperationalError("no such function: json_valid")
    with pytest.raises(RuntimeError, match=r"3\.25"):
        rer.require_features(Old())
    path = _make(tmp_path / f"{Z}-old-sqlite.db")
    real = rer.require_features
    monkeypatch.setattr(rer, "require_features", lambda con: real(Old()))
    assert rer.main(["--db", str(path)]) == 2
    assert "needs SQLite 3.25 or later" in capsys.readouterr().err


@pytest.mark.skipif(not hasattr(__import__("os"), "geteuid") or __import__("os").geteuid() == 0,
                    reason="root can write to a read-only directory")
def test_a_wal_database_in_a_read_only_directory_gets_an_actionable_message(tmp_path, capsys):
    import os
    sub = tmp_path / "ro"
    sub.mkdir()
    path = _make(sub / "wal.db", wal=True)
    os.chmod(sub, 0o555)
    try:
        assert rer.main(["--db", str(path)]) == 2
    finally:
        os.chmod(sub, 0o755)
    assert "writable directory" in capsys.readouterr().err


# ------------------------------------------------------------------ test-strength round

def _accepted(con, tid, route, revisions, *, run=R1, descriptor=None, role="executor", missing_revision=False):
    """An accepted task with `revisions` revisions, the last of which is accepted and produced by one dispatch."""
    _dispatch(con, tid, tid, route, run=run, role=role, start="2026-09-01T10:00:00+00:00",
              end="2026-09-01T10:01:00+00:00")
    for n in range(1, revisions + 1):
        _revision(con, f"{tid}-{n}", tid, n, tid, LEDGER, run=run)
    _task(con, tid, "accepted", run=run, first=tid, rev="missing" if missing_revision else f"{tid}-{revisions}",
          descriptor=descriptor)


def test_a_file_that_changes_during_the_report_is_flagged(new_db, monkeypatch):
    real, calls = rer.sha256_file, []

    def spy(path):
        if calls:
            Path(path).write_bytes(Path(path).read_bytes() + b"x")
        calls.append(path)
        return real(path)
    monkeypatch.setattr(rer, "sha256_file", spy)
    src = rer.build_report(new_db)["source"]
    assert src["hash_unchanged"] is False and src["sha256"] != src["sha256_after"]
    assert src["sha256_after"] == hashlib.sha256(new_db.read_bytes()).hexdigest()


def test_a_live_wal_is_read_and_reported(tmp_path):
    path = _make(tmp_path / f"{Z}-live.db", wal=True)
    writer = db.connect(path)
    try:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        _insert(writer, "runs", id=f"run-{Z}-live", created_at="2026-10-09T09:00:00+00:00", phase="active")
        writer.commit()
        report = rer.build_report(path)
    finally:
        writer.close()
    src = report["source"]
    assert (src["wal_file_present"], src["shm_file_present"], src["journal_mode"]) == (True, True, "wal")
    assert src["side_files_created_by_report"] is False
    assert src["run_date_range"]["last"] == "2026-10-09"  # the committed frame in the -wal was read


def test_self_review_values_outside_the_known_sets_are_clamped(tmp_path):
    con = _mini(tmp_path)
    for name, receipt in (("a", json.dumps({"kind": f"{Z}-kind"})), ("b", json.dumps({"kind": "exempt", "type": f"{Z}-type"})),
                          ("c", "{}"), ("d", "5"), ("e", json.dumps({"kind": "exempt", "type": "empty"}))):
        _revision(con, name, "T1", 1, "a", receipt)
    con.commit()
    con.close()
    text, as_json = _printed(tmp_path / "mini.db")
    assert Z not in text and Z not in as_json
    rows = {(r["kind"], r["exempt_type"]): r["revisions"] for r in sec(rer.build_report(tmp_path / "mini.db"), "self_review")["by_receipt"]}
    assert rows == {("exempt", "empty"): 1, ("exempt", "other"): 1, ("unreadable", None): 3}


def test_an_unexpected_attribution_basis_is_clamped(tmp_path):
    con = _mini(tmp_path)
    _gate(con, "a", "lane", "s", ["T1"])
    _finding(con, "a", "a", "C1", f"{Z}-basis", "T1", "T1")
    con.commit()
    con.close()
    report = rer.build_report(tmp_path / "mini.db")
    assert [r["basis"] for r in sec(report, "findings")["by_attribution"]] == ["other"]
    assert Z not in json.dumps(report)


def test_worker_dispatches_count_and_a_reviewer_that_shares_a_revision_does_not(tmp_path):
    con = _mini(tmp_path)
    _insert(con, "runs", id=R1, created_at="2026-09-01T09:00:00+00:00", phase="closed")
    _accepted(con, "T1", A, 1, role="worker")
    _dispatch(con, "rv", None, B, role="reviewer", end="2026-09-01T10:02:00+00:00")
    _accepted(con, "T2", A, 1)
    con.execute("UPDATE revisions SET dispatch_id=? WHERE id=?", (did("rv"), f"rev-{Z}-T2-1"))
    con.commit()
    con.close()
    rows = sec(rer.build_report(tmp_path / "mini.db"), "dispatches")["routes"]
    assert by_route(rows, "worker", A)["strict_successes"] == 1
    assert by_route(rows, "reviewer", B)["strict_successes"] is None
    assert by_route(rows, "executor", A)["strict_successes"] == 0


def test_revisions_to_accept_uses_the_even_median_and_sets_a_missing_revision_apart(tmp_path):
    con = _mini(tmp_path)
    _insert(con, "runs", id=R1, created_at="2026-09-01T09:00:00+00:00", phase="closed")
    for tid, n in (("T1", 1), ("T2", 1), ("T3", 2), ("T4", 6)):
        _accepted(con, tid, A, n)
    _accepted(con, "T5", A, 1, missing_revision=True)
    con.commit()
    con.close()
    accepted = sec(rer.build_report(tmp_path / "mini.db"), "task_paths")["revisions_to_accept"]
    assert accepted == {"tasks": 5, "revision_row_missing": 1, "mean": 2.5, "median": 1.5,
                        "histogram": {"1": 2, "2": 1, "6": 1}}


def test_a_route_with_no_recorded_effort_is_one_unknown_route_everywhere(tmp_path):
    con = _mini(tmp_path)
    _insert(con, "runs", id=R1, created_at="2026-09-01T09:00:00+00:00", phase="closed")
    _accepted(con, "T1", ("claude", "m", None), 1)
    con.commit()
    con.close()
    report = rer.build_report(tmp_path / "mini.db")
    assert by_route(sec(report, "dispatches")["routes"], "executor", ("claude", "m", "unknown"))["dispatches"] == 1
    assert by_route(sec(report, "episodes")["routes"], "executor", ("claude", "m", "unknown"))["accepted_episodes"] == 1


def test_a_path_with_spaces_hashes_and_percent_signs_opens(new_db, tmp_path):
    odd = tmp_path / "a b#c%d.db"
    odd.write_bytes(new_db.read_bytes())
    report = rer.build_report(odd)
    assert report["source"]["sha256"] == hashlib.sha256(odd.read_bytes()).hexdigest()
    assert report["source"]["integrity_check"] == "ok" and report["source"]["run_date_range"]["runs"] == 2


def test_an_explicit_unknown_is_a_declared_value_for_every_tag(tmp_path):
    con = _mini(tmp_path)
    _insert(con, "runs", id=R1, created_at="2026-09-01T09:00:00+00:00", phase="closed")
    _accepted(con, "T1", A, 1, descriptor=json.dumps({"brief_shape": "unknown"}))
    _accepted(con, "T2", A, 1, descriptor=json.dumps({"brief_shape": "checks-only"}))
    con.commit()
    con.close()
    shape = sec(rer.build_report(tmp_path / "mini.db"), "tags")["tasks"]["brief_shape"]
    assert shape == {"declared": 2, "unknown_not_recorded": 0, "values": {"checks-only": 1, "unknown": 1}}


def test_missing_values_print_as_unknown_in_text_and_costs_are_never_zero(new_db, tmp_path):
    text = rer.render_text(rer.build_report(new_db))
    assert "total=0" not in text and "total=unknown" in text
    legacy = rer.render_text(rer.build_report(_make(tmp_path / "legacy.db", legacy_only=True)))
    assert "strict_successes: unknown" in legacy


def test_latency_and_cost_keep_their_precision(tmp_path):
    con = _mini(tmp_path)
    _dispatch(con, "a", None, A, start="2026-09-01T10:00:00+00:00", end="2026-09-01T10:01:00+00:00", wall=12.3456,
              money=0.0123456)
    con.commit()
    con.close()
    report = sec(rer.build_report(tmp_path / "mini.db"), "latency_and_cost")
    assert by_route(report["latency"], "executor", A)["mean_seconds"] == 12.346
    assert by_route(report["cost"], "executor", A)["total"] == 0.012346


# ------------------------------------------------------------------ round 2

def test_a_created_at_that_is_not_a_date_gives_no_day(tmp_path):
    con = _mini(tmp_path)
    _insert(con, "runs", id=R1, created_at=f"SECRET\n{Z}!!xyz", phase="closed")
    _insert(con, "runs", id=R2, created_at="2026-09-01T09:00:00+00:00", phase="closed")
    con.commit()
    con.close()
    report = rer.build_report(tmp_path / "mini.db")
    assert report["source"]["run_date_range"] == {"runs": 2, "first": "2026-09-01", "last": "2026-09-01"}
    assert Z not in json.dumps(report)
    con = sqlite3.connect(tmp_path / "mini.db")
    con.execute("UPDATE runs SET created_at=?", (f"SECRET\n{Z}!!xyz",))
    con.commit()
    con.close()
    assert rer.build_report(tmp_path / "mini.db")["source"]["run_date_range"] == {"runs": 2, "first": None, "last": None}


def test_dispatch_rows_whose_labels_clamp_to_the_same_key_merge(tmp_path):
    con = _mini(tmp_path)
    _insert(con, "runs", id=R1, created_at="2026-09-01T09:00:00+00:00", phase="closed")
    for tid, harness, model, secs in (("T1", "A B", "m 1", 60), ("T2", "C D", "m 2", 120)):
        _dispatch(con, tid, tid, (harness, model, "high"), start="2026-09-01T10:00:00+00:00",
                  end=f"2026-09-01T10:{secs // 60:02d}:00+00:00", money=1.0)
        _revision(con, tid, tid, 1, tid, LEDGER)
        _task(con, tid, "accepted", first=tid, rev=tid)
    con.commit()
    con.close()
    report = rer.build_report(tmp_path / "mini.db")
    routes = sec(report, "dispatches")["routes"]
    assert [(r["harness"], r["model"], r["dispatches"], r["strict_successes"]) for r in routes] == [("other", "other", 2, 2)]
    assert sec(report, "dispatches")["totals"]["strict_successes"] == 2
    (latency,) = sec(report, "latency_and_cost")["latency"]
    assert (latency["terminal"], latency["recorded"]) == (2, 2)
    assert (latency["mean_seconds"], latency["median_seconds"], latency["max_seconds"]) == ("unknown",) * 3
    (cost,) = sec(report, "latency_and_cost")["cost"]
    assert (cost["recorded"], cost["total"]) == (2, 2.0)


def test_a_database_error_names_no_schema_object(tmp_path, capsys):
    path = tmp_path / "bad.db"
    con = sqlite3.connect(path)
    con.execute(f"CREATE TABLE secret_{Z}(a)")
    con.execute("PRAGMA writable_schema=ON")
    con.execute("UPDATE sqlite_master SET sql=? WHERE name=?", (f"create table secret_{Z}(", f"secret_{Z}"))
    con.commit()
    con.close()
    assert rer.main(["--db", str(path)]) == 2
    err = capsys.readouterr().err
    assert Z not in err and "DatabaseError" in err


# ------------------------------------------------------------------ round 2 test strength

def test_free_text_routes_are_clamped_in_the_episodes_and_merge_with_clamped_phases(tmp_path):
    con = _mini(tmp_path)
    for name in (R1, R2):
        _insert(con, "runs", id=name, created_at="2026-09-01T09:00:00+00:00", phase=f"Free text {name}")
    _accepted(con, "T1", (f"/Users/{Z}/secret", f"model {Z}/leak", f"hi {Z}"), 1)
    _accepted(con, "T2", (f"/etc/{Z}", f"other {Z}", f"lo {Z}"), 1)
    con.execute("UPDATE tasks SET status=? || id", (f"Bad {Z} ",))
    con.commit()
    con.close()
    report = rer.build_report(tmp_path / "mini.db")
    assert Z not in json.dumps(report)
    (row,) = sec(report, "episodes")["routes"]
    assert (row["harness"], row["model"], row["effort"], row["episodes"], row["accepted_episodes"]) == (
        "other", "other", "other", 2, 2)
    assert sec(report, "population")["runs_by_phase"] == {"other": 2}
    assert sec(report, "population")["tasks_by_status"] == {"other": 2}
    assert sec(report, "dispatches")["totals"]["strict_successes"] == 2  # a strict success does not read the status


def test_a_blob_in_a_text_column_is_other(tmp_path):
    con = _mini(tmp_path)
    _dispatch(con, "a", None, ("claude", b"\xff\xfe", b"x"), end="2026-09-01T10:01:00+00:00")
    con.commit()
    con.close()
    (row,) = sec(rer.build_report(tmp_path / "mini.db"), "dispatches")["routes"]
    assert (row["harness"], row["model"], row["effort"]) == ("claude", "other", "other")


SECTIONS = ("_population_section", "_dispatch_section", "_episode_section", "_task_path_section", "_self_review_section",
            "_finding_section", "_latency_cost_section", "_size_section", "_tag_section")


@pytest.mark.parametrize("fn", SECTIONS)
def test_every_section_fails_alone(new_db, monkeypatch, fn):
    def boom(con, schema):
        raise KeyError(Z)
    monkeypatch.setattr(rer, fn, boom)
    report = rer.build_report(new_db)
    failed = [n for n, v in report["sections"].items() if v.get("reason") == "KeyError while reading this section"]
    assert len(failed) == 1 and Z not in json.dumps(report)
    assert sum(1 for v in report["sections"].values() if v["available"]) == len(report["sections"]) - 1


class _Probe:
    """A connection that lacks one SQLite feature: it fails any probe that uses it."""
    def __init__(self, missing, message):
        self.missing, self.message = missing, message

    def execute(self, sql, *a):
        if self.missing in sql:
            raise sqlite3.OperationalError(self.message)
        return sqlite3.connect(":memory:").execute("SELECT 1")


@pytest.mark.parametrize("missing,message", [("json_valid", "no such function: json_valid"),
                                             ("json_type", "no such function: json_type"),
                                             ("OVER", 'near "(": syntax error')])
def test_each_part_of_the_feature_probe_is_exercised(missing, message):
    with pytest.raises(RuntimeError, match=r"3\.25"):
        rer.require_features(_Probe(missing, message))
    rer.require_features(_Probe("zzz-absent", "x"))  # a connection that has everything passes


def test_another_operational_error_is_not_mistaken_for_a_missing_feature():
    with pytest.raises(sqlite3.OperationalError, match="unable to open"):
        rer.require_features(_Probe("json_valid", "unable to open database file"))


@pytest.mark.parametrize("error,hint", [(sqlite3.OperationalError("attempt to write a readonly database"), True),
                                        (sqlite3.OperationalError("unable to open database file"), True),
                                        (sqlite3.DatabaseError("file is not a database"), False)])
def test_the_writable_directory_hint_follows_the_error(tmp_path, monkeypatch, capsys, error, hint):
    path = tmp_path / "x.db"
    path.write_bytes(b"x")
    monkeypatch.setattr(rer, "build_report", lambda p: (_ for _ in ()).throw(error))
    assert rer.main(["--db", str(path)]) == 2
    assert ("writable directory" in capsys.readouterr().err) is hint


def test_the_package_import_falls_back_to_the_checkout_and_beats_a_stale_office(new_db, tmp_path):
    stale = tmp_path / "stale" / "office"
    stale.mkdir(parents=True)
    (stale / "__init__.py").write_text("")
    script = str(ROOT / "scripts" / "routing_evidence_report.py")
    for env in ({}, {"PYTHONPATH": str(stale.parent)}):
        # -S: no site-packages, so the editable install cannot supply `office`; only the src/ fallback can.
        proc = subprocess.run([sys.executable, "-S", script, "--db", str(new_db), "--format", "json"], env=env,
                              capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout)["sections"]["episodes"]["available"] is True, env


def test_clamped_rows_merge_whether_or_not_each_recorded_a_statistic(tmp_path):
    con = _mini(tmp_path)
    _dispatch(con, "a", None, ("A B", "m", "high"), end="2026-09-01T10:01:00+00:00")  # no start, no cost
    _dispatch(con, "b", None, ("C D", "m", "high"), start="2026-09-01T10:00:00+00:00",
              end="2026-09-01T10:01:00+00:00", money=1.5)
    con.commit()
    con.close()
    report = sec(rer.build_report(tmp_path / "mini.db"), "latency_and_cost")
    (cost,) = report["cost"]
    assert (cost["terminal"], cost["recorded"], cost["unknown"], cost["total"]) == (2, 1, 1, 1.5)
    (latency,) = report["latency"]
    assert (latency["terminal"], latency["recorded"], latency["unknown"]) == (2, 1, 1)
    assert latency["mean_seconds"] == latency["median_seconds"] == latency["max_seconds"] == "unknown"


def test_a_blob_created_at_gives_no_day_and_the_report_still_prints(tmp_path, capsys):
    con = _mini(tmp_path)
    _insert(con, "runs", id=R1, created_at=b"2026-09-01\xff\xfe", phase="closed")
    con.commit()
    con.close()
    assert rer.main(["--db", str(tmp_path / "mini.db"), "--format", "json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["source"]["run_date_range"] == {"runs": 1, "first": None, "last": None}
