"""#1106 H3: a reused executor route that a user deny now rejects is dropped and the rerun
routes from the approved plan; `amend route --auto` clears a stale reroute on the parent
dispatch; status names the route the next rerun will actually take."""
import json
import re
from datetime import datetime, timezone

from office import amend, candidates, route_override, routecmd, state
from conftest import start_inline
from test_adaptive_dispatch import EXTERNAL, FB1, FB2, PRIMARY, PLANNED, _approve, _quota

CLAUDE = "claude@1/claude-opus-5-5@medium"


def _denials(env):
    rows = env.con().execute("SELECT payload_json FROM events WHERE kind='route.changed' ORDER BY seq").fetchall()
    return [c for c in (json.loads(r[0]) for r in rows) if c["kind"] == "denied"]


def _triple(env):
    return env.con().execute("SELECT triple FROM dispatches WHERE task_id='T1' ORDER BY started_at DESC").fetchone()[0]


def _route(triple):
    """`harness@major/model@effort` as the `harness/model@effort` form `office route policy` takes."""
    return triple.split("@", 1)[0] + "/" + triple.split("/", 1)[1]


def _end_round(env):
    """The live dispatch ends and the task waits for a rerun, as after a review round."""
    con = env.con()
    con.execute("UPDATE dispatches SET status='ended', ended_at=? WHERE task_id='T1' AND ended_at IS NULL",
                (datetime.now(timezone.utc).isoformat(),))
    con.execute("UPDATE tasks SET status='changes_required' WHERE id='T1'")
    con.commit()


def _orchestrator_reroute(env, gear=None):
    """The plan primary dispatches, then `--reroute` moves the rerun off claude (spent) onto a codex
    route picked from current evidence. Returns that route's triple; the pick itself varies by run.
    `gear` pins a run-gear-1 run, where size-scoped policy rules apply; by default the run is legacy-pinned."""
    if gear:
        env.trust()
        start_inline(env, plan=PLANNED, gear=gear)
        env.office("approve", "plan", "--quote", "approved", check=0)
    else:
        _approve(env)
    env.office("dispatch", "T1", env=_quota(env), check=0)
    _end_round(env)
    code, out = env.office("rerun", "T1", "--fresh", "--reroute", env=_quota(env, claude=1))
    assert code == 0 and not _triple(env).startswith("claude@"), out
    rerouted = _triple(env)
    _end_round(env)
    return rerouted


def test_a_denied_orchestrator_reroute_is_dropped_and_the_rerun_repicks_the_plan(env):
    rerouted = _orchestrator_reroute(env)
    routecmd.change("deny", role="executor", route=_route(rerouted))
    code, out = env.office("rerun", "T1", "--fresh", env=EXTERNAL)
    assert code == 0 and _triple(env) == CLAUDE, out
    (change,) = _denials(env)
    assert (change["before"], change["after"], change["actor"]) == (rerouted, CLAUDE, "office"), change
    assert "deny" in change["reason"] and change["task_id"] == "T1" and change["stage"] == "executor"


def test_a_denied_declared_pin_is_dropped_by_a_rerun_and_the_rerun_repicks_the_plan(env):
    _approve(env)
    code, out = env.office("amend", "route", "T1", "--as", FB1, "--quote", "use astra here")
    assert code == 0, out
    env.office("dispatch", "T1", env=_quota(env), check=0)
    _end_round(env)
    routecmd.change("deny", role="executor", route=FB1)
    code, out = env.office("rerun", "T1", "--fresh", env=EXTERNAL)
    assert code == 0 and _triple(env) == CLAUDE, out
    (change,) = _denials(env)
    assert (change["before"], change["after"]) == ("codex@1/gpt-6-astra@low", CLAUDE), change


def test_auto_clears_a_stale_reroute_on_the_parent_and_the_rerun_routes_from_the_plan(env):
    rerouted = _orchestrator_reroute(env)
    code, out = env.office("amend", "route", "T1", "--auto", "--quote", "back to automatic")
    assert code == 0 and f"T1 route {rerouted} -> automatic ({CLAUDE} planned)" in out, out
    code, out = env.office("status")
    assert f"routes: T1 {CLAUDE} (planned)" in out, out
    code, out = env.office("rerun", "T1", "--fresh", env=EXTERNAL)
    assert code == 0 and _triple(env) == CLAUDE, out


def test_auto_still_refuses_a_task_that_routes_automatically_with_no_stale_reroute(env):
    _approve(env)
    env.office("dispatch", "T1", env=_quota(env), check=0)
    _end_round(env)
    code, out = env.office("amend", "route", "T1", "--auto", "--quote", "auto please")
    assert code != 0 and "already routes automatically" in out, out


def test_status_and_the_refused_rerun_name_the_same_planned_route_when_nothing_routes(env):
    rerouted = _orchestrator_reroute(env)
    for route in (_route(rerouted), PRIMARY, FB1, FB2):
        routecmd.change("deny", role="executor", route=route)
    code, out = env.office("status")
    assert f"routes: T1 {CLAUDE} (planned)" in out, out
    code, out = env.office("rerun", "T1", "--fresh", env=EXTERNAL)
    assert code != 0 and "every planned route is unavailable" in out, out
    assert f"{rerouted} is denied" in out and f"{CLAUDE}:" in out, out
    assert len(env.con().execute("SELECT id FROM dispatches WHERE task_id='T1'").fetchall()) == 2


def test_an_amendment_relaunch_after_auto_takes_the_plan_slate_not_the_stale_reroute(env):
    _orchestrator_reroute(env)
    code, out = env.office("amend", "route", "T1", "--auto", "--quote", "back to automatic")
    assert code == 0, out
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    task = state.get_task(con, run["id"], "T1")
    decision = amend._relaunch_decision(con, run, task)
    assert decision and decision.get("selected") == CLAUDE, decision


def test_a_size_scoped_deny_of_the_reused_route_is_seen_by_status_and_the_rerun(env):
    rerouted = _orchestrator_reroute(env, gear="M")
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    # Sized rules apply to the size routing gives the task on this run-gear-1 run.
    size = candidates._effective_task_descriptor(run, state.get_task(con, run["id"], "T1")).get("task_size")
    assert size, size
    routecmd.change("deny", role="executor", route=_route(rerouted), sizes=[size])
    code, out = env.office("status")
    planned = re.search(r"routes: T1 (\S+) \(planned\)", out)
    assert planned and planned.group(1) != rerouted, out
    code, out = env.office("rerun", "T1", "--fresh", env=EXTERNAL)
    assert code == 0 and _triple(env) == planned.group(1), out
    (change,) = _denials(env)
    assert change["before"] == rerouted, change


def test_a_receipt_that_lifts_the_deny_keeps_the_reused_route_in_status_and_the_rerun(env):
    rerouted = _orchestrator_reroute(env)
    routecmd.change("deny", role="executor", route=_route(rerouted))
    con = env.con()
    run_id = con.execute("SELECT id FROM runs").fetchone()[0]
    route_override.issue(con, run_id=run_id, role="executor", route=_route(rerouted), reason="Rico asked for it",
                         ttl_s=600, confirm=lambda _s: True, terminal_check=lambda: None)
    code, out = env.office("status")
    assert f"routes: T1 {rerouted}" in out, out
    code, out = env.office("rerun", "T1", "--fresh", env=EXTERNAL)
    assert code == 0 and _triple(env) == rerouted and _denials(env) == [], out
