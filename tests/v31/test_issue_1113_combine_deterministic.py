"""#1113: `integration.combine` is deterministic. Preflight and submit each resolve a multi-dependency task's
inherited base; the merge commit must have the same SHA both times, or the proof recorded at preflight is
not found at submit."""
from __future__ import annotations

from conftest import GOOD_ADD, GOOD_MUL
from test_multi_dependency_base import (EXTERNAL, accept_on_base, base_sha, dispatch_base, edit_line, fan_in_run,
                                        run_id)


def _two_parent_run(env):
    fan_in_run(env)
    c1 = accept_on_base(env, "T1", {"calc.py": GOOD_ADD, "registry.txt": edit_line(2, "t1 was here")})
    c2 = accept_on_base(env, "T2", {"mul.py": GOOD_MUL, "registry.txt": edit_line(25, "t2 was here")})
    return c1, c2


def _at(monkeypatch, stamp: str):
    """The wall clock git reads for new commits."""
    monkeypatch.setenv("GIT_AUTHOR_DATE", stamp)
    monkeypatch.setenv("GIT_COMMITTER_DATE", stamp)


def test_inherited_base_is_the_same_sha_after_the_clock_moves(env, monkeypatch):
    from office import state, submit
    c1, c2 = _two_parent_run(env)
    code, out = env.office("dispatch", "T3", env=EXTERNAL)
    assert code == 0, out
    con = env.con()
    run = state.get_run(con, run_id(env))
    task = state.get_task(con, run["id"], "T3")
    d = {"base_commit": base_sha(env)}
    head = dispatch_base(env, "T3")
    _at(monkeypatch, "2030-01-01T00:00:00 +0000")
    first = submit.inherited_base(con, run, task, d, head)
    _at(monkeypatch, "2031-06-06T06:06:06 +0000")
    second = submit.inherited_base(con, run, task, d, head)
    assert first and first == second
    assert env.git("show", "-s", "--format=%P", first).split() == [c1, c2]


def test_attribution_agrees_between_preflight_and_submit(env, monkeypatch):
    from office import state, submit
    _two_parent_run(env)
    code, out = env.office("dispatch", "T3", env=EXTERNAL)
    assert code == 0, out
    con = env.con()
    run = state.get_run(con, run_id(env))
    task = state.get_task(con, run["id"], "T3")
    d = {"base_commit": base_sha(env)}
    head = dispatch_base(env, "T3")
    _at(monkeypatch, "2030-01-01T00:00:00 +0000")
    at_preflight = submit.attribution(con, run, task, d, head)
    _at(monkeypatch, "2032-02-02T02:02:02 +0000")
    at_submit = submit.attribution(con, run, task, d, head)
    assert at_preflight == at_submit and at_preflight[1] == []


def test_combine_pins_both_dates_to_the_newest_parent(env, monkeypatch):
    from office import integration, state
    c1, c2 = _two_parent_run(env)
    run = state.get_run(env.con(), run_id(env))
    _at(monkeypatch, "2040-01-01T00:00:00 +0000")
    merged = integration.combine(run, [("T1", c1), ("T2", c2)])
    newest = max(int(env.git("log", "-1", "--format=%ct", c)) for c in (c1, c2))
    assert env.git("show", "-s", "--format=%ct %at", merged).split() == [str(newest)] * 2
