"""#755: a running run keeps the verification limits it pinned at start. `office config --run <id>
--apply-verification --quote "<words>"` re-pins the check timeouts and check concurrency on the user's word,
and `office config --run <id> <key>` shows the pinned value its gates use, not the live file value."""
from __future__ import annotations

import json

import pytest

from conftest import PLAN_ONE

from test_adaptive_dispatch import EXTERNAL, _approve
from office import state

pytestmark = pytest.mark.integration


def _run(env):
    con = env.con()
    return state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])


def _verification(env):
    return _run(env)["policy"].get("verification") or {}


def test_run_read_shows_the_pinned_value_and_names_the_live_one(env):
    _approve(env, plan=PLAN_ONE)
    pinned = _verification(env)["check_timeout_seconds"]
    env.office("config", "--repo", "verification.check_timeout_seconds", "3000", env=EXTERNAL, check=0)
    rid = _run(env)["id"]
    code, out = env.office("config", "--run", rid[:8], "verification.check_timeout_seconds", "--show-origin",
                           env=EXTERNAL)
    assert code == 0 and f"run {rid[:8]} (pinned)\t{pinned}" in out, out
    assert "3000 (repo)" in out and "--apply-verification" in out, out
    # A plain read of the files still reports the live value.
    code, out = env.office("config", "verification.check_timeout_seconds", "--show-origin", env=EXTERNAL)
    assert code == 0 and "repo\t3000" in out, out


def test_apply_verification_repins_only_the_limits_and_records_the_quote(env):
    _approve(env, plan=PLAN_ONE)
    before = dict(_verification(env))
    for key, value in (("check_timeout_seconds", "3000"), ("full_suite_timeout_seconds", "9000"),
                       ("check_concurrency", "1"), ("environment_retry_max", "5")):
        env.office("config", "--repo", f"verification.{key}", value, env=EXTERNAL, check=0)
    assert _verification(env) == before, "a config edit alone does not reach the running run"
    rid = _run(env)["id"]
    code, out = env.office("config", "--run", rid[:8], "--apply-verification", "--quote", "raise the timeout",
                           env=EXTERNAL)
    assert code == 0 and "verification.check_timeout_seconds" in out and "-> 3000" in out, out
    after = _verification(env)
    assert after["check_timeout_seconds"] == 3000 and after["full_suite_timeout_seconds"] == 9000
    assert after["check_concurrency"] == 1
    assert after["environment_retry_max"] == before["environment_retry_max"], "verdict caps stay pinned"
    (event,) = [json.loads(r[0]) for r in env.con().execute(
        "SELECT payload_json FROM events WHERE kind='run.verification_applied'")]
    assert event["quote"] == "raise the timeout"
    assert set(event["changed"]) == {"check_timeout_seconds", "full_suite_timeout_seconds", "check_concurrency"}
    code, out = env.office("config", "--run", rid[:8], "--apply-verification", "--quote", "again", env=EXTERNAL)
    assert code == 0 and "nothing changed" in out, out


def test_apply_verification_needs_the_users_words_and_refuses_a_worker(env):
    _approve(env, plan=PLAN_ONE)
    rid = _run(env)["id"]
    code, out = env.office("config", "--run", rid[:8], "--apply-verification", env=EXTERNAL)
    assert code != 0 and "--quote" in out, out
    code, out = env.office("config", "--run", rid[:8], "--apply-verification", "--quote", "ok",
                           env={**EXTERNAL, "OFFICE_ROLE": "executor"})
    assert code != 0 and "worker-cannot-apply-verification" in out, out


def test_a_run_pinned_config_is_read_only(env):
    _approve(env, plan=PLAN_ONE)
    rid = _run(env)["id"]
    code, out = env.office("config", "--run", rid[:8], "verification.check_timeout_seconds", "99", env=EXTERNAL)
    assert code != 0 and "read-only" in out, out
