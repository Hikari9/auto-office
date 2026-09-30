"""office prune: dry run never mutates; force never touches resumable runs."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from conftest import GOOD_ADD, approved_run


def _snapshot(env) -> tuple:
    con = env.con()
    rows = []
    for table in ("runs", "tasks", "outbox", "events", "session_bindings", "revisions", "gates"):
        rows.append(tuple(tuple(r) for r in con.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()))
    files = []
    for base in (env.state, env.repo / ".office"):
        for root, _d, fs in os.walk(base):
            for f in fs:
                p = Path(root) / f
                files.append((str(p), hashlib.sha256(p.read_bytes()).hexdigest()))
    objects = env.git("count-objects", "-v")
    refs = env.git("for-each-ref")
    return tuple(rows), tuple(sorted(files)), objects, refs


def _finished_run(env) -> str:
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    env.office("close", "--handoff", "https://example.test/pr/9", check=0)
    con = env.con()
    return con.execute("SELECT id FROM runs WHERE phase='closed'").fetchone()[0]


@pytest.mark.approved
def test_dry_run_mutates_nothing(env):
    run_id = _finished_run(env)
    env.office("start", "still active", "--planner", "inline", check=0)
    before = _snapshot(env)
    code, out = env.office("prune")
    assert code == 0 and "would prune 1" in out and run_id[:8] in out, out
    assert _snapshot(env) == before


@pytest.mark.approved
def test_force_prunes_terminal_only_and_keeps_tombstone(env):
    run_id = _finished_run(env)
    env.office("start", "still active", "--planner", "inline", check=0)
    con = env.con()
    active_id = con.execute("SELECT id FROM runs WHERE phase NOT IN ('closed','abandoned')").fetchone()[0]
    run = dict(con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
    worktree_root = env.state / "worktrees" / run_id[:8]
    assert worktree_root.exists() and Path(run["state_dir"]).exists()
    code, out = env.office("prune", "-f")
    assert code == 0 and "pruned 1" in out, out
    assert not worktree_root.exists() and not Path(run["state_dir"]).exists()
    tomb = dict(con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
    assert tomb["phase"] == "closed" and tomb["office_version"] == run["office_version"]
    assert tomb["pruned_at"] and tomb["prune_status"] == "complete" and tomb["archive_digest"]
    assert tomb["terminal_at"] and "tombstone" in tomb["landing_json"]
    assert con.execute("SELECT COUNT(*) FROM tasks WHERE run_id=?", (run_id,)).fetchone()[0] == 0
    # Learning evidence survives pruning.
    assert con.execute("SELECT COUNT(*) FROM dispatches WHERE run_id=?", (run_id,)).fetchone()[0] >= 1
    # The active run is untouched.
    assert Path(con.execute("SELECT state_dir FROM runs WHERE id=?", (active_id,)).fetchone()[0]).exists()
    assert con.execute("SELECT pruned_at FROM runs WHERE id=?", (active_id,)).fetchone()[0] is None
    code, out = env.office("prune", "-f")
    assert code == 0 and "pruned 0" in out  # idempotent


def test_force_rechecks_eligibility_at_deletion(env, monkeypatch):
    """A stale candidate list (as if a run became resumable after planning)
    never deletes it: the check under the write lock wins."""
    from office import prune, state
    env.office("start", "active", "--planner", "inline", check=0)
    con = env.con()
    active = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    stale = [{"run_id": active["id"], "phase": "closed", "goal": "", "eligible": True, "reason": None,
              "worktrees": [], "bindings": [], "run_dir": active["state_dir"], "bytes": 0}]
    monkeypatch.setattr(prune, "plan", lambda con, **k: stale)
    res = prune.force(con)
    assert "skipped 1" in res.lines[0], res.lines
    assert Path(active["state_dir"]).exists()
    assert state.get_run(con, active["id"])["pruned_at"] is None


def test_abandoned_run_is_prunable_but_paused_is_not(env):
    env.office("start", "to abandon", "--planner", "inline", check=0)
    con = env.con()
    rid = con.execute("SELECT id FROM runs").fetchone()[0]
    code, out = env.office("close", "--abandon", "superseded by another approach", "--run", rid)
    assert code == 0, out
    env.office("start", "paused one", "--planner", "inline", check=0)
    code, out = env.office("prune")
    assert rid[:8] in out and "would prune 1" in out


@pytest.mark.approved
def test_run_flag_restricts_prune_to_that_run(env):
    """`--run` names one run; it must never widen to every finished run."""
    first = _finished_run(env)
    env.office("start", "second", "--planner", "inline", check=0)
    con = env.con()
    second = con.execute("SELECT id FROM runs WHERE id<>? ORDER BY created_at DESC", (first,)).fetchone()[0]
    code, out = env.office("close", "--abandon", "not needed", "--run", second)
    assert code == 0, out
    code, out = env.office("prune")
    assert "would prune 2" in out, out
    before = _snapshot(env)
    code, out = env.office("prune", "--run", second[:8])
    assert code == 0 and "would prune 1" in out and second[:8] in out and first[:8] not in out, out
    assert f"--run {second[:8]}" in out, out
    assert _snapshot(env) == before
    code, out = env.office("prune", "-f", "--run", second[:8])
    assert code == 0 and "pruned 1" in out, out
    assert con.execute("SELECT pruned_at FROM runs WHERE id=?", (second,)).fetchone()[0]
    assert con.execute("SELECT pruned_at FROM runs WHERE id=?", (first,)).fetchone()[0] is None
    code, out = env.office("prune", "--run", second[:8])
    assert code != 0 and "already-pruned" in out, out


@pytest.mark.approved
def test_run_flag_refuses_unknown_and_unfinished_runs(env):
    _finished_run(env)
    env.office("start", "still active", "--planner", "inline", check=0)
    con = env.con()
    active = con.execute("SELECT id FROM runs WHERE phase NOT IN ('closed','abandoned')").fetchone()[0]
    before = _snapshot(env)
    code, out = env.office("prune", "-f", "--run", active[:8])
    assert code != 0 and "run-not-finished" in out, out
    code, out = env.office("prune", "-f", "--run", "ffffffff0000")
    assert code != 0 and "unknown-run" in out, out
    assert _snapshot(env) == before
