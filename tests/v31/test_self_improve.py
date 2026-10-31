"""`office self-improve` is a pointer: it prints the workflow and neither writes rows nor spawns a process."""
from __future__ import annotations

import subprocess

import pytest

from conftest import GOOD_ADD, approved_run
from office import cli, version

pytestmark = pytest.mark.approved

SELF_IMPROVE_TABLES = ("self_improve_runs", "self_improve_incidents", "self_improve_seen_sources",
                       "self_improve_attempts")


def _row_counts(env) -> dict:
    con = env.con()
    try:
        return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in SELF_IMPROVE_TABLES}
    finally:
        con.close()


@pytest.fixture
def spawns(monkeypatch):
    """Every subprocess the code under test starts, recorded as its argv."""
    argvs = []
    popen = subprocess.Popen

    class Recording(popen):
        def __init__(self, args, *a, **kw):
            argvs.append([str(x) for x in args] if not isinstance(args, str) else [args])
            super().__init__(args, *a, **kw)

    monkeypatch.setattr(subprocess, "Popen", Recording)
    return argvs


def _reporter_spawns(argvs) -> list:
    return [a for a in argvs if any("self_improve" in x for x in a)]


def test_self_improve_prints_the_workflow_and_a_next_line(env):
    code, out = env.office("self-improve")
    assert code == 0, out
    assert out.startswith(cli.SELF_IMPROVE_STEPS), out
    assert "issue-only" in out and "Hikari9/auto-office" in out
    lines = out.rstrip("\n").splitlines()
    assert lines[-1].startswith("next: "), lines[-1]
    assert sum(1 for line in lines if line.startswith("next:")) == 1


def test_self_improve_json_carries_the_same_steps(env):
    code, data = env.ojson("self-improve")
    assert code == 0 and data["ok"] is True
    assert "\n".join(data["lines"]) == cli.SELF_IMPROVE_STEPS
    assert data["next"]


def test_self_improve_writes_no_rows_and_spawns_nothing(env, spawns):
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}])
    assert _row_counts(env) == dict.fromkeys(SELF_IMPROVE_TABLES, 0)
    before = len(spawns)
    for _ in range(2):
        assert env.office("self-improve", check=0)
    assert _row_counts(env) == dict.fromkeys(SELF_IMPROVE_TABLES, 0)
    # The CLI's #920 rebuild probe resolves the install identity before any command; in a source checkout that reads
    # `git rev-parse HEAD`. That is version plumbing, not self-improve work: every other process stays forbidden.
    probe = ["git", "-C", str(version.SOURCE_ROOT), "rev-parse", "HEAD"]
    assert [a for a in spawns[before:] if a != probe] == [], spawns[before:]


def test_self_improve_leaves_existing_rows_alone(env, spawns):
    """A shared runs.db may still hold rows from an older runtime. They are not read, changed or retried."""
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}])
    con = env.con()
    try:
        con.execute("INSERT INTO self_improve_runs(run_id,armed,cursor,updated_at) VALUES('old',1,7,'2030')")
        con.execute("INSERT INTO self_improve_attempts(id,run_id,command,outcome,created_at) "
                    "VALUES('a1','old','close','refused','2030')")
    finally:
        con.close()
    before = len(spawns)
    env.office("self-improve", check=0)
    env.office("status", check=0)
    con = env.con()
    try:
        assert tuple(con.execute("SELECT * FROM self_improve_runs").fetchone()) == ("old", 1, 7, "2030")
        assert con.execute("SELECT COUNT(*) FROM self_improve_attempts").fetchone()[0] == 1
    finally:
        con.close()
    assert not _reporter_spawns(spawns[before:])


def test_refused_close_prints_no_self_improve_note_and_starts_no_reporter(env, spawns):
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
                 convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    env.office("dispatch", "T1", check=0)
    code, out = env.office("close")
    assert code == 4 and "landing not recorded" in out, out
    assert "self-improve" not in out and "self_improve" not in out
    assert _row_counts(env) == dict.fromkeys(SELF_IMPROVE_TABLES, 0)
    assert not _reporter_spawns(spawns)


def test_close_and_prune_start_no_reporter_and_write_no_rows(env, spawns):
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
                 convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    env.office("dispatch", "T1", check=0)
    code, out = env.office("close", "--handoff", "https://example.test/pr/9")
    assert code == 0, out
    assert "self-improve" not in out
    assert spawns, "the recorder saw the close's own job spawns"
    code, out = env.office("prune", "-f")
    assert code == 0 and "pruned 1" in out, out
    assert _row_counts(env) == dict.fromkeys(SELF_IMPROVE_TABLES, 0)
    assert not _reporter_spawns(spawns)


def test_land_without_a_landing_path_leaves_no_reporter_trace(env, spawns):
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}])
    code, out = env.office("land")
    assert code != 0, out
    assert "self-improve" not in out and "self_improve" not in out
    assert _row_counts(env) == dict.fromkeys(SELF_IMPROVE_TABLES, 0)
    assert not _reporter_spawns(spawns)


def test_the_hidden_reporter_subcommand_is_gone(env):
    code, out = env.office("_self_improve", "--once")
    assert code == 2, out
