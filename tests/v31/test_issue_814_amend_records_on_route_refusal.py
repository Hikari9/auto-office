"""#814: an amendment whose relaunch the recorded route's quota refuses stays atomic (#721), and its remedy names
the user's route choice (`office amend route`, `office rerun --fresh --as`), never `office dispatch`."""
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


def test_a_quota_refused_amendment_relaunch_names_the_users_route_choice_not_dispatch(env):
    _approve(env)
    env.office("dispatch", "T1", env=_quota(env), check=0)
    _end_round(env)
    before = _state(env)
    code, out = env.office("amend", "T1", "--add-accept", "reject nonnumeric arguments", "--", "tighten input",
                           env=_quota(env, claude=1, codex=1))
    assert code == 4, out
    assert _state(env) == before
    nxt = out.splitlines()[-1]
    assert nxt.startswith("next:") and "office amend route T1 --as" in nxt and "--quote" in nxt, nxt
    assert "office rerun T1 --fresh --as" in nxt and "office dispatch" not in nxt, nxt
