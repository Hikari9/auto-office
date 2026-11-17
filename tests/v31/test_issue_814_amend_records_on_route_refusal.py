"""#814: an amendment whose relaunch the recorded route's quota refuses is still recorded. The task is held with
the reason, and `next:` names the user's route choice, never `office dispatch`."""
from datetime import datetime, timezone

from test_adaptive_dispatch import _approve, _quota


def _end_round(env):
    con = env.con()
    con.execute("UPDATE dispatches SET status='ended', ended_at=? WHERE task_id='T1' AND ended_at IS NULL",
                (datetime.now(timezone.utc).isoformat(),))
    con.execute("UPDATE tasks SET status='changes_required' WHERE id='T1'")
    con.commit()


def _state(env):
    con = env.con()
    return (con.execute("SELECT COUNT(*) FROM amendments").fetchone()[0],
            con.execute("SELECT COUNT(*) FROM dispatches WHERE task_id='T1'").fetchone()[0],
            dict(con.execute("SELECT status, pause_reason FROM tasks WHERE id='T1'").fetchone()))


def test_an_amendment_is_recorded_and_the_task_held_when_quota_refuses_the_relaunch(env):
    _approve(env)
    env.office("dispatch", "T1", env=_quota(env), check=0)
    _end_round(env)
    code, out = env.office("amend", "T1", "--add-accept", "reject nonnumeric arguments", "--", "tighten input",
                           env=_quota(env, claude=1, codex=1))
    assert code == 0, out
    amendments, dispatches, task = _state(env)
    assert amendments == 1 and dispatches == 1, (amendments, dispatches)
    assert task["status"] == "changes_required" and "relaunch held" in task["pause_reason"], task
    assert "reject nonnumeric arguments" in (env.con().execute("SELECT body FROM plans ORDER BY version DESC").fetchone()[0])
    nxt = out.splitlines()[-1]
    assert nxt.startswith("next:") and "office amend route T1 --as" in nxt and "--quote" in nxt, nxt
    assert "office rerun T1 --fresh --as" in nxt and "office dispatch" not in nxt, nxt


def test_the_held_amendment_is_delivered_by_the_rerun_once_quota_returns(env):
    _approve(env)
    env.office("dispatch", "T1", env=_quota(env), check=0)
    _end_round(env)
    env.office("amend", "T1", "--add-accept", "reject nonnumeric arguments", "--", "tighten input",
               env=_quota(env, claude=1, codex=1), check=0)
    code, out = env.office("rerun", "T1", "--fresh", env=_quota(env))
    assert code == 0, out
    assert _state(env)[1] == 2
