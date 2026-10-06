"""A STALE checks outcome (worktree changed after submit, or while checks ran) is
recorded on the gate, not a KeyError crash; the self-review ledger Office consumes
at submit is not a change to the submitted revision."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from office import briefs, contract, db, gates, state

RUN = {"id": "r1"}


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_STATE_HOME", str(tmp_path / "state"))
    return db.connect(tmp_path / "runs.db")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def worktree(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    _git(wt, "init", "-q")
    (wt / "calc.py").write_text("x = 1\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "submitted")
    return wt


def _gate(con, status="running") -> dict:
    con.execute("INSERT INTO gates(id, run_id, subject, task_id, revision_id, kind, input_key, status, created_at) "
                "VALUES('G1', 'r1', 'T1', 'T1', 'R1', 'checks', 'k', ?, '2026-01-01T00:00:00Z')", (status,))
    return dict(con.execute("SELECT * FROM gates WHERE id='G1'").fetchone())


def _rev(worktree: Path) -> dict:
    return {"id": "R1", "commit_sha": _git(worktree, "rev-parse", "HEAD"), "dispatch_id": "D1"}


@pytest.mark.parametrize("summary", ["worktree changed after submit; checks cannot bind to the revision",
                                     "worktree changed while checks ran"])
def test_stale_outcome_marks_the_gate_stale_without_raising(con, summary):
    gate = _gate(con)
    task = {"id": "T1", "current_revision_id": "R1"}
    with db.transaction(con):
        gates._ingest_checks_convergence(con, RUN, gate, task, {"verdict": "STALE", "summary": summary})
    row = dict(con.execute("SELECT status, stale_reason, finished_at, verdict FROM gates WHERE id='G1'").fetchone())
    assert row["status"] == "stale" and row["stale_reason"] == summary
    assert row["finished_at"] and row["verdict"] is None


def test_stale_outcome_through_ingest_task_gate_on_a_convergence_run(con, monkeypatch):
    _gate(con)
    monkeypatch.setattr(state, "get_task", lambda *a: {"id": "T1", "current_revision_id": "R1"})
    run = {**RUN, "gates": {"review_contract": contract.CONVERGENCE}}
    assert contract.is_convergence(run)
    with db.transaction(con):
        gates.ingest_task_gate(con, run, "G1", {"verdict": "STALE", "summary": "worktree changed while checks ran"})
    row = con.execute("SELECT status, stale_reason FROM gates WHERE id='G1'").fetchone()
    assert (row["status"], row["stale_reason"]) == ("stale", "worktree changed while checks ran")


def test_ledger_written_before_checks_is_not_a_change(con, worktree):
    (worktree / briefs.LEDGER_FILE).write_text("COMMIT abc\nROUND 1\n")
    out = gates._run_commands(con, RUN, ["true"], worktree, _rev(worktree), _gate(con), {})
    assert out["verdict"] == "PASS", out


def test_ledger_written_while_checks_run_is_not_a_change(con, worktree):
    out = gates._run_commands(con, RUN, [f"echo ledger > {briefs.LEDGER_FILE}"], worktree, _rev(worktree),
                              _gate(con), {})
    assert (worktree / briefs.LEDGER_FILE).exists()
    assert out["verdict"] == "PASS", out


def test_staged_ledger_is_not_a_change(con, worktree):
    (worktree / briefs.LEDGER_FILE).write_text("COMMIT abc\n")
    _git(worktree, "add", briefs.LEDGER_FILE)
    out = gates._run_commands(con, RUN, ["true"], worktree, _rev(worktree), _gate(con), {})
    assert out["verdict"] == "PASS", out


def test_any_other_edit_before_checks_is_stale(con, worktree):
    (worktree / "calc.py").write_text("x = 2\n")
    (worktree / briefs.LEDGER_FILE).write_text("COMMIT abc\n")
    out = gates._run_commands(con, RUN, ["true"], worktree, _rev(worktree), _gate(con), {})
    assert out["verdict"] == "STALE" and "after submit" in out["summary"], out


def test_any_other_edit_while_checks_run_is_stale(con, worktree):
    out = gates._run_commands(con, RUN, ["echo 'x = 3' > calc.py"], worktree, _rev(worktree), _gate(con), {})
    assert out["verdict"] == "STALE" and "while checks ran" in out["summary"], out
