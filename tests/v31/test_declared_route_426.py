"""#426: the effective route is recorded on the task before its agent runs, followed
until deliberately changed, and every change names old/new route, reason, actor, time
and the affected task. Agents never run here: dispatches are launched external."""
import json

from conftest import PLAN_ONE

from test_adaptive_dispatch import EXTERNAL, FB1, PLANNED, _approve, _quota

CODEX = "codex@1/gpt-6-astra@low"


def _events(env):
    return [json.loads(r[0]) for r in env.con().execute("SELECT payload_json FROM events WHERE kind='route.changed'")]


def _task_route(env):
    return json.loads(env.con().execute("SELECT route_json FROM tasks WHERE id='T1'").fetchone()[0])


def _triple(env):
    return env.con().execute("SELECT triple FROM dispatches WHERE task_id='T1' ORDER BY started_at DESC").fetchone()[0]


def test_an_unchanged_route_is_recorded_before_execution_with_no_change_event(env):
    _approve(env)
    code, out = env.office("status")
    assert "routes: T1 claude@1/claude-opus-5-5@medium (planned)" in out, out
    code, out = env.office("dispatch", "T1", env=_quota(env))
    assert code == 0, out
    route = _task_route(env)
    assert route["candidate"]["model_id"] == "claude-opus-5-5" and route["candidate"]["effort"] == "medium"
    assert _events(env) == []
    code, out = env.office("status")
    assert "routes: T1 claude@1/claude-opus-5-5@medium" in out and "(planned)" not in out, out
    code, out = env.office("inspect", "route", "T1")
    assert "T1 effective route claude@1/claude-opus-5-5@medium" in out and "route change" not in out, out


def test_a_deliberate_reroute_of_a_pending_task_is_recorded_and_dispatch_follows_it(env):
    _approve(env)
    code, out = env.office("amend", "route", "T1", "--as", "codex/gpt-6-astra@low", "--quote", "use astra here")
    assert code == 0 and "declared" in out, out
    (event,) = _events(env)
    assert (event["before"], event["after"]) == ("claude@1/claude-opus-5-5@medium", CODEX)
    assert event["actor"] == "user" and event["task_id"] == "T1" and event["stage"] == "executor"
    assert event["kind"] == "reroute" and "use astra here" in event["reason"] and event["at"]
    code, out = env.office("status")
    assert f"T1 {CODEX} (declared)" in out, out
    code, out = env.office("dispatch", "T1", env=_quota(env))
    assert code == 0 and f"executor/{CODEX}" in out, out
    assert _triple(env) == CODEX and len(_events(env)) == 1
    code, out = env.office("inspect", "route", "T1")
    assert "route change" in out and "by user" in out and f"effective route {CODEX} (declared)" in out, out


def test_a_declared_route_is_never_swapped_silently_when_it_cannot_run(env):
    _approve(env)
    env.office("amend", "route", "T1", "--as", "codex/gpt-6-astra@low", "--quote", "use astra here", check=0)
    code, out = env.office("dispatch", "T1", env=_quota(env, codex=1))
    assert code != 0 and "every planned route is unavailable" in out and "--reroute" in out, out
    assert env.con().execute("SELECT COUNT(*) FROM dispatches WHERE task_id='T1'").fetchone()[0] == 0
    assert len(_events(env)) == 1, "no fallback change was recorded: nothing was swapped"


def test_a_quota_driven_fallback_records_the_swap_and_its_reason(env):
    _approve(env)
    code, out = env.office("dispatch", "T1", env=_quota(env, claude=1))
    assert code == 0 and CODEX in out, out
    (event,) = _events(env)
    assert (event["before"], event["after"], event["kind"], event["actor"]) == (
        "claude@1/claude-opus-5-5@medium", CODEX, "quota", "office")
    assert "quota" in event["reason"] and event["task_id"] == "T1" and event["dispatch_id"] is None
    assert _task_route(env)["candidate"]["model_id"] == "gpt-6-astra"
    code, out = env.office("inspect", "route", "T1")
    assert "[quota] by office" in out, out


def test_an_unavailable_model_fallback_records_the_swap(env, monkeypatch):
    _approve(env)
    from office import candidates, db, dispatch, routing, state
    con = env.con()
    run = state.get_run(con, con.execute("SELECT run_id FROM tasks WHERE id='T1'").fetchone()[0])
    task = state.get_task(con, run["id"], "T1")
    planned = dispatch._planned_slate(con, run, "T1")
    fb = candidates.declared_candidate("codex", "gpt-6-astra", "low")
    fresh = {"status": "selected", "qualifying_candidates": {routing.candidate_id(fb): fb}, "rejected": [],
             "request": {"policy": {}}}
    monkeypatch.setattr(candidates, "route_role", lambda *a, **k: fresh)
    decision = dispatch.planned_route(con, run, task)
    assert decision["selected"] == routing.candidate_id(fb)
    with db.transaction(con):
        dispatch.note_route(con, run, task, decision)
    (event,) = _events(env)
    assert event["before"] == planned["primary"] and event["kind"] == "unavailable" and event["actor"] == "office"
    assert "no longer a candidate" in event["reason"]


def test_a_redispatch_restores_the_recorded_route_instead_of_recomputing(env):
    _approve(env)
    env.office("dispatch", "T1", env=_quota(env, claude=1), check=0)
    assert _triple(env) == CODEX
    env.office("revoke", "T1", env=EXTERNAL, check=0)
    # Quota is healthy again: a fresh computation would pick the plan's primary on claude.
    code, out = env.office("dispatch", "T1", env=_quota(env))
    assert code == 0, out
    assert _triple(env) == CODEX, "the recorded route is restored"
    assert len(_events(env)) == 1, "restoring it is not a change"


def test_a_sticky_rerun_keeps_the_recorded_route_and_adds_no_change(env):
    _approve(env)
    env.office("dispatch", "T1", env=_quota(env), check=0)
    before = _task_route(env)
    from office import state
    con = env.con()
    run = state.get_run(con, con.execute("SELECT run_id FROM tasks WHERE id='T1'").fetchone()[0])
    assert state.recorded_route(state.get_task(con, run["id"], "T1")) == before
    assert _events(env) == []


def test_a_first_dispatch_with_as_records_the_deviation_and_stays_declared(env):
    _approve(env)
    code, out = env.office("dispatch", "T1", "--as", "codex/gpt-6-astra@low", env=EXTERNAL)
    assert code == 0, out
    (event,) = _events(env)
    assert (event["before"], event["after"], event["kind"], event["actor"]) == (
        "claude@1/claude-opus-5-5@medium", CODEX, "override", "user")
    assert event["reason"] and event["at"] and event["task_id"] == "T1"
    assert _task_route(env)["declared"] is True
    env.office("revoke", "T1", env=EXTERNAL, check=0)
    code, out = env.office("dispatch", "T1", env=_quota(env))
    assert code == 0 and _triple(env) == CODEX and len(_events(env)) == 1, out


def _trust(monkeypatch, triple, trust_state="valid-unverified"):
    from office import scoring
    real = scoring.evaluate_trust_state
    monkeypatch.setattr(scoring, "evaluate_trust_state",
                        lambda con, cid: (0, trust_state) if cid == triple else real(con, cid))


def test_a_redispatch_follows_a_declared_unverified_route_on_the_users_authority(env, monkeypatch):
    _approve(env)
    _trust(monkeypatch, CODEX)
    code, out = env.office("dispatch", "T1", "--as", "codex/gpt-6-astra@low", env=EXTERNAL)
    assert code == 0, out
    env.office("revoke", "T1", env=EXTERNAL, check=0)
    code, out = env.office("dispatch", "T1", env=_quota(env))
    assert code == 0 and "every planned route is unavailable" not in out, out
    assert _triple(env) == CODEX and len(_events(env)) == 1
    assert _task_route(env)["declared"] is True


def test_a_declared_unverified_route_still_stops_on_quota(env, monkeypatch):
    _approve(env)
    _trust(monkeypatch, CODEX)
    env.office("amend", "route", "T1", "--as", "codex/gpt-6-astra@low", "--quote", "use astra here", check=0)
    code, out = env.office("dispatch", "T1", env=_quota(env, codex=1))
    assert code != 0 and "every planned route is unavailable" in out and "quota" in out, out
    assert "adapter trust" not in out, out  # trust passed; the later quota stage stopped it


def test_a_declared_quarantined_route_is_never_launched(env, monkeypatch):
    _approve(env)
    _trust(monkeypatch, CODEX, "quarantined")
    env.office("amend", "route", "T1", "--as", "codex/gpt-6-astra@low", "--quote", "use astra here", check=0)
    code, out = env.office("dispatch", "T1", env=_quota(env))
    assert code != 0 and "quarantined" in out, out
    assert env.con().execute("SELECT COUNT(*) FROM dispatches WHERE task_id='T1'").fetchone()[0] == 0


def test_a_recorded_unverified_route_gets_no_declared_pass(env, monkeypatch):
    _approve(env)
    env.office("dispatch", "T1", env=_quota(env), check=0)
    primary = _triple(env)
    assert _task_route(env).get("declared") is not True
    env.office("revoke", "T1", env=EXTERNAL, check=0)
    _trust(monkeypatch, primary)
    code, out = env.office("dispatch", "T1", env=_quota(env))
    assert code == 0 and _triple(env) != primary, out  # fell back: only a declared route passes unverified trust


def test_a_task_between_rounds_can_be_rerouted_and_rerun_follows_it(env):
    _approve(env)
    env.office("dispatch", "T1", env=_quota(env), check=0)
    con = env.con()
    con.execute("UPDATE dispatches SET status='ended', ended_at='2026-10-09T00:00:00+00:00' WHERE task_id='T1'")
    con.execute("UPDATE tasks SET status='changes_required' WHERE id='T1'")
    con.commit()
    code, out = env.office("amend", "route", "T1", "--as", "codex/gpt-6-astra@low", "--quote", "next round on astra")
    assert code == 0, out
    (event,) = _events(env)
    assert event["before"] == "claude@1/claude-opus-5-5@medium" and event["after"] == CODEX and event["actor"] == "user"
    code, out = env.office("rerun", "T1", "--fresh", env=EXTERNAL)
    assert code == 0, out
    assert _triple(env) == CODEX and len(_events(env)) == 1
