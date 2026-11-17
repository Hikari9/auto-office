"""#858: a route the user quoted through `office amend route --quote` qualifies as their `--as` does:
by `declared_decision`, not by a routing row that may carry no allow. The role deny still wins."""
import json

from test_adaptive_dispatch import _approve, _quota

SOL_HIGH = "codex/gpt-6.1-sol@high"  # in the catalog, but no proof and no user allow: routing alone would pool it
QUOTE = "use sol here"


def _dispatches(env):
    return env.con().execute("SELECT triple FROM dispatches WHERE task_id='T1'").fetchall()


def test_a_quoted_amend_route_to_a_gated_row_with_no_allow_dispatches(env):
    _approve(env)
    code, out = env.office("amend", "route", "T1", "--as", SOL_HIGH, "--quote", QUOTE)
    assert code == 0 and "declared" in out, out
    code, out = env.office("dispatch", "T1", env=_quota(env))
    assert code == 0, out
    ((triple,),) = _dispatches(env)
    assert triple.endswith("/gpt-6.1-sol@high"), triple
    route = json.loads(env.con().execute("SELECT route_json FROM tasks WHERE id='T1'").fetchone()[0])
    assert route["declared"] is True and route["route_authority"]["quote"] == QUOTE


def test_a_role_deny_still_refuses_a_quoted_amend_route(env):
    from office import routecmd
    _approve(env)
    env.office("amend", "route", "T1", "--as", SOL_HIGH, "--quote", QUOTE, check=0)
    routecmd.change("deny", role="executor", route=SOL_HIGH)
    code, out = env.office("dispatch", "T1", env=_quota(env))
    assert code != 0 and "deny" in out.lower(), out
    assert _dispatches(env) == []


def test_a_quoted_amend_route_still_stops_on_quota(env):
    _approve(env)
    env.office("amend", "route", "T1", "--as", SOL_HIGH, "--quote", QUOTE, check=0)
    code, out = env.office("dispatch", "T1", env=_quota(env, codex=1))
    assert code != 0 and _dispatches(env) == [], out
