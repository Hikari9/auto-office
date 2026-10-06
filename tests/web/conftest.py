"""Shared fixtures for the read-only web observer tests."""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from office import db
from office.web import synthetic
from office.web.observer import Observer


def dump_hash(path: Path, replace: tuple[str, str] | None = None) -> str:
    """Hash of the logical database content (schema and rows)."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        text = "\n".join(con.iterdump())
        if replace:
            text = text.replace(*replace)
        return hashlib.sha256(text.encode()).hexdigest()
    finally:
        con.close()


@pytest.fixture(name="dump_hash")
def dump_hash_fixture():
    return dump_hash


@pytest.fixture
def writer(tmp_path):
    """A runs.db opened through the normal writer, and its path."""
    path = tmp_path / "data" / "runs.db"
    con = db.connect(path)
    yield path, con
    con.close()


@pytest.fixture
def workspace(tmp_path):
    return synthetic.build_workspace(tmp_path / "ws", "small")


@pytest.fixture
def make_observer():
    made = []

    def make(path, **ctx):
        obs = Observer(path, **ctx)
        made.append(obs)
        return obs

    yield make
    for obs in made:
        obs.close()


@pytest.fixture
def mini(writer, tmp_path):
    """One hand-built run with every agent state the projection distinguishes.

    R1 (live): T1 executor with two attempts (D1 old, D2 current, quota wait),
    T2 paused, T3 blocked, T4 accepted with a PR, T5 cancelled; a code reviewer
    D5 still running whose reply.txt exists; a planner dispatch (other column);
    one active and one ended session binding. R2 has no tasks.
    """
    path, con = writer
    w = synthetic
    runs_dir = tmp_path / "state" / "runs"
    with db.transaction(con):
        w.insert_run(con, "R1", git_common_dir="/src/alpha/.git", goal="Fix #99 on branch office/x/T9", phase="executing",
                     gear="M", landing={"issue": 42}, state_dir=str(runs_dir / "R1"), end_state="merged")
        w.insert_run(con, "R2", git_common_dir="/src/beta/.git", phase="planning")
        w.insert_task(con, "R1", "T1", status="running")
        w.insert_task(con, "R1", "T2", status="paused", pause_reason="user")
        w.insert_task(con, "R1", "T3", status="blocked")
        w.insert_task(con, "R1", "T4", status="accepted",
                      pr={"number": 7, "url": "https://github.com/acme/alpha/pull/7", "base": "main",
                          "branch": "office/R1/T4"})
        w.insert_task(con, "R1", "T5", status="cancelled")
        w.insert_task(con, "R1", "T6", status="queued", stack_after="T4",
                      pr={"number": 8, "url": "https://github.com/acme/alpha/pull/8", "base": "office/R1/T4",
                          "branch": "office/R1/T6"})
        w.insert_dispatch(con, "D1", "R1", task_id="T1", status="exited", ended=True, at=0)
        w.insert_dispatch(con, "D2", "R1", task_id="T1", status="running", at=10,
                          fallbacks_taken=[{"route": "codex/gpt-6-luna/high", "reason": "usage limit"}],
                          stall_kind="usage_limit", resets_at="2026-10-07T17:00:00Z", limit_label="resets 5pm")
        w.insert_dispatch(con, "D3", "R1", task_id="T2", status="running", at=11, idle_since="2026-09-01T00:12:00Z")
        w.insert_dispatch(con, "D4", "R1", task_id="T4", status="done", ended=True, at=12)
        w.insert_dispatch(con, "D5", "R1", role="code_reviewer", task_id="T4", status="running", at=13)
        w.insert_dispatch(con, "D6", "R1", role="planner", status="done", ended=True, at=1)
        w.insert_dispatch(con, "D7", "R1", role="visual_reviewer", task_id="T4", status="failed", ended=True, at=14)
        w.insert_dispatch(con, "D8", "R1", role="plan_reviewer", status="stale", at=2)
        w.insert_binding(con, "R1", "claude", "S-active", at=0)
        w.insert_binding(con, "R1", "codex", "S-ended", ended=True, at=0)
        w.insert_route_audit(con, "RA1", "R1", "T1", primary="claude/claude-opus-5-5/high",
                             fallbacks=["codex/gpt-6-luna/high"], planner_why="opus handles this shape", at=0)
        w.insert_gate(con, "G1", "R1", "T4", "code_review", "done", "PASS")
        w.insert_gate(con, "G2", "R1", "T4", "checks", "done", "PASS")
        con.execute("INSERT INTO cursors(run_id, consumer, last_seq, updated_at) VALUES('R1','orchestrator',0,'t0')")
        for n in range(5):
            w.insert_event(con, "R1", "note", f"event {n}", at=n)
    return {"path": path, "con": con, "runs_dir": runs_dir}
