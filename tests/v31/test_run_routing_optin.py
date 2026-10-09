"""Per-run routing opt-in (#308): a run keeps the roles and routing it pinned at start, and
`office config --run <id> --apply-routing --quote "<words>"` re-pins them on the user's word."""
from __future__ import annotations

import json

import pytest

from conftest import PLAN_ONE

from test_adaptive_dispatch import EXTERNAL, _approve, _quota
from office import candidates, state
from office.config import config_drift

SEED = "codex/gpt-6-astra@low"


def _run(env):
    con = env.con()
    return state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])


def _seed(run):
    return run["policy"]["roles"]["executor"].get("preferred_seed")


def _requested_seed(env, run):
    con = env.con()
    fresh = candidates.route_role(con, state.pinned_config(run), run, "executor", task_id="T1", probe=False)
    return fresh["request"]["preferred_seed"]


def test_a_running_run_keeps_its_pinned_routing_after_a_config_edit(env):
    _approve(env, plan=PLAN_ONE)
    pinned_before = _seed(_run(env))
    code, out = env.office("config", "roles.executor.preferred_seed", SEED, env=EXTERNAL)
    assert code == 0, out
    run = _run(env)
    assert _seed(run) == pinned_before, "the edit is not applied to the running run"
    assert config_drift(run) and "not applied to running run" in config_drift(run), config_drift(run)
    assert _requested_seed(env, run) == pinned_before
    code, out = env.office("dispatch", "T1", env=_quota(env))
    assert code == 0, out


def test_apply_routing_repins_roles_and_later_dispatches_use_them(env):
    _approve(env, plan=PLAN_ONE)
    env.office("config", "roles.executor.preferred_seed", SEED, env=EXTERNAL, check=0)
    rid = _run(env)["id"]
    code, out = env.office("config", "--run", rid[:8], "--apply-routing", "--quote", "use astra for this run",
                           env=EXTERNAL)
    assert code == 0 and f"run {rid[:8]} re-pinned roles" in out, out
    run = _run(env)
    assert [e["model_id"] for e in _seed(run)] == ["gpt-6-astra"], _seed(run)
    assert _requested_seed(env, run) == _seed(run)
    assert config_drift(run) is None, "the re-pinned roles no longer differ from the files"
    (event,) = [json.loads(r[0]) for r in env.con().execute("SELECT payload_json FROM events WHERE kind='run.routing_applied'")]
    assert event["quote"] == "use astra for this run" and event["changed"] == ["roles"]


def test_apply_routing_repins_the_routing_block_too(env):
    _approve(env, plan=PLAN_ONE)
    env.office("config", "routing.adaptive.exploration.rate", "0.2", env=EXTERNAL, check=0)
    rid = _run(env)["id"]
    code, out = env.office("config", "--run", rid[:8], "--apply-routing", "--quote", "explore more", env=EXTERNAL)
    assert code == 0, out
    run = _run(env)
    assert run["policy"]["routing"]["adaptive"]["exploration"]["rate"] == 0.2
    assert config_drift(run) is None
    code, out = env.office("dispatch", "T1", env=_quota(env))
    assert code == 0, out


def test_apply_routing_needs_the_users_words_and_changes_nothing(env):
    _approve(env, plan=PLAN_ONE)
    env.office("config", "roles.executor.preferred_seed", SEED, env=EXTERNAL, check=0)
    pinned_before = _seed(_run(env))
    rid = _run(env)["id"]
    code, out = env.office("config", "--run", rid[:8], "--apply-routing", env=EXTERNAL)
    assert code == 2 and "records the user's words" in out, out
    assert _seed(_run(env)) == pinned_before
    assert env.con().execute("SELECT COUNT(*) FROM events WHERE kind='run.routing_applied'").fetchone()[0] == 0


def test_apply_routing_without_a_run_is_refused(env):
    _approve(env, plan=PLAN_ONE)
    code, out = env.office("config", "--apply-routing", "--quote", "x", env=EXTERNAL)
    assert code == 2 and "--apply-routing needs --run" in out, out


def test_apply_routing_takes_only_run_and_quote(env):
    _approve(env, plan=PLAN_ONE)
    rid = _run(env)["id"]
    code, out = env.office("config", "roles.executor.preferred_seed", SEED, "--apply-routing", "--run", rid[:8],
                           "--quote", "x", env=EXTERNAL)
    assert code == 2 and "takes only --run and --quote" in out, out
    assert env.con().execute("SELECT COUNT(*) FROM events WHERE kind='run.routing_applied'").fetchone()[0] == 0


def test_apply_routing_refuses_a_run_with_no_repo_on_record(env):
    _approve(env, plan=PLAN_ONE)
    con = env.con()
    con.execute("UPDATE runs SET repo_root='/nonexistent/office-repo'")
    con.commit()
    rid = _run(env)["id"]
    before = _seed(_run(env))
    code, out = env.office("config", "--run", rid[:8], "--apply-routing", "--quote", "x", env=EXTERNAL)
    assert code != 0 and "its repo config cannot be read" in out, out
    assert _seed(_run(env)) == before


@pytest.mark.parametrize("worker_env", [
    {"OFFICE_DISPATCH_ID": "D1", "OFFICE_ROLE": "executor"},
    {"OFFICE_DISPATCH_ID": "D1"},
    {"OFFICE_ROLE": "executor"},
])
def test_a_worker_cannot_apply_routing_even_with_a_quote(env, worker_env):
    _approve(env, plan=PLAN_ONE)
    env.office("config", "roles.executor.preferred_seed", SEED, env=EXTERNAL, check=0)
    pinned_before = _seed(_run(env))
    assert pinned_before != [{"model_id": "gpt-6-astra", "harness": "codex", "effort": "low"}]
    rid = _run(env)["id"]
    code, out = env.office("config", "--run", rid[:8], "--apply-routing", "--quote", "ok",
                           env={**EXTERNAL, **worker_env})
    assert code != 0 and "worker-cannot-apply-routing" in out, out
    assert _seed(_run(env)) == pinned_before
    assert env.con().execute("SELECT COUNT(*) FROM events WHERE kind='run.routing_applied'").fetchone()[0] == 0
