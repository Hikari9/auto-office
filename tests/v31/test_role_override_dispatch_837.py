"""#837 end to end: a denied executor route runs once with a user receipt, and never from a quote or a second try."""
import pytest

from test_adaptive_dispatch import FB1, _approve, _quota
from office import db, route_override, routecmd

ROUTE = FB1  # codex/gpt-6-astra@low in the adaptive fixtures


def _receipt(env):
    con = env.con()
    run_id = con.execute("SELECT id FROM runs").fetchone()[0]
    return route_override.issue(con, run_id=run_id, role="executor", route=ROUTE, reason="Rico asked for astra",
                                ttl_s=600, confirm=lambda _s: True, terminal_check=lambda: None)


def _count(env):
    return env.con().execute("SELECT COUNT(*) FROM dispatches WHERE task_id='T1'").fetchone()[0]


def test_a_quote_cannot_lift_a_role_deny_but_a_receipt_runs_it_exactly_once(env):
    _approve(env)
    routecmd.change("deny", role="executor", route=ROUTE)
    code, out = env.office("dispatch", "T1", "--as", ROUTE, "--quote", "Rico says run astra", env=_quota(env))
    assert code != 0 and "deny" in out.lower() and _count(env) == 0, out
    code, out = env.office("dispatch", "T1", "--as", ROUTE, env=_quota(env))
    assert code != 0 and _count(env) == 0, out
    receipt = _receipt(env)
    code, out = env.office("dispatch", "T1", "--as", ROUTE, env=_quota(env))
    assert code == 0 and _count(env) == 1, out
    assert receipt["id"] in env.con().execute("SELECT route_json FROM dispatches WHERE task_id='T1'").fetchone()[0]
    # one-off: the same receipt does not launch the route again
    con = env.con()
    con.execute("UPDATE dispatches SET status='ended', ended_at=started_at WHERE task_id='T1'")
    con.execute("UPDATE tasks SET status='changes_required' WHERE id='T1'")
    con.commit()
    code, out = env.office("dispatch", "T1", "--as", ROUTE, env=_quota(env))
    assert code != 0 and _count(env) == 1, out
