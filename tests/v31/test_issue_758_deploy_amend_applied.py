"""#567/#758: a user-quoted `office amend requirements -- "deploy_prod: <cmd>"` is applied to the stored requirements,
so `office land` uses the new command, a PLAN.md that matches it passes a later contract amendment, and nothing
without the user's words can change a deploy command."""
from __future__ import annotations

import pytest
from conftest import PLAN_ONE

from office import amend, land, state

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}
QUOTE = "use echo new-deploy for production"


def _end_state(env) -> dict:
    con = env.con()
    try:
        run = dict(con.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT 1").fetchone())
        return land.end_state(con, state.get_run(con, run["id"]))
    finally:
        con.close()


def _version(env) -> int:
    con = env.con()
    try:
        return con.execute("SELECT requirements_version FROM runs").fetchone()[0]
    finally:
        con.close()


def test_deploy_edits_reads_one_command_per_labelled_line():
    assert amend.deploy_edits("deploy_prod: echo a\ndeploy_verify:  curl -f x \nprose deploy_prod: no") == {
        "prod": "echo a", "verify": "curl -f x"}
    assert amend.deploy_edits("make the deploy faster") == {}


@pytest.mark.approved
def test_quoted_deploy_amendment_changes_the_command_land_uses(env):
    assert _end_state(env)["deploy"] == {}
    code, out = env.office("amend", "requirements", "--quote", QUOTE, "--", "deploy_prod: echo new-deploy",
                           env=EXTERNAL)
    before = _version(env)
    assert code == 0 and f"requirements r{before} recorded" in out, out
    assert _end_state(env)["deploy"] == {"prod": "echo new-deploy"}
    env.office("approve", "plan", "--quote", "approved the new deploy", check=0)
    assert _end_state(env)["deploy"] == {"prod": "echo new-deploy"}, "reauthorization keeps the amended command"
    code, out = env.office("amend", "requirements", "--quote", QUOTE, "--",
                           "deploy_prod: echo newer\ndeploy_verify: echo ok", env=EXTERNAL)
    assert code == 0, out
    assert _end_state(env)["deploy"] == {"prod": "echo newer", "verify": "echo ok"}


@pytest.mark.approved
def test_unquoted_deploy_amendment_changes_nothing(env):
    before = _version(env)
    code, out = env.office("amend", "requirements", "--", "deploy_prod: echo sneaky", env=EXTERNAL)
    assert code != 0 and "user-quote-required" in out, out
    code, out = env.office("amend", "requirements", "--quote", "  ", "--", "deploy_prod: echo sneaky", env=EXTERNAL)
    assert code != 0 and "user-quote-required" in out, out
    assert _version(env) == before and _end_state(env)["deploy"] == {}


@pytest.mark.approved
def test_plan_matching_the_amended_deploy_no_longer_trips_requirements_change(env):
    env.office("amend", "requirements", "--quote", QUOTE, "--", "deploy_prod: echo new-deploy", env=EXTERNAL,
               check=0)
    edited = PLAN_ONE.replace("calc.add(2, 3) == 5\nvisual", "calc.add(2, 3) == 5\n- calc.add(0, 0) == 0\nvisual")
    # a plan that still carries the old (absent) deploy line is not what the user authorized
    env.write_plan(edited.replace("blast_radius: repo", "blast_radius: repo\ndeploy_prod: echo other"))
    code, out = env.office("amend", "T1", "--contract", "--", "add acceptance", env=EXTERNAL)
    assert code != 0 and "requirements-change" in out, out
    # the plan states the stored command: the amendment is a contract change only
    env.write_plan(edited.replace("blast_radius: repo", "blast_radius: repo\ndeploy_prod: echo new-deploy"))
    code, out = env.office("amend", "T1", "--contract", "--", "add acceptance", env=EXTERNAL)
    assert "requirements-change" not in out, out
    assert code == 0, out
    assert _end_state(env)["deploy"] == {"prod": "echo new-deploy"}


@pytest.mark.approved
def test_a_plan_resubmit_does_not_revert_the_users_deploy_edit(env):
    env.office("amend", "requirements", "--quote", QUOTE, "--", "deploy_prod: echo new-deploy", env=EXTERNAL,
               check=0)
    env.write_plan(PLAN_ONE.replace("blast_radius: repo", "blast_radius: repo\ndeploy_prod: echo planner-old")
                   .replace("- no CLI", "- no CLI\n- no GUI"))
    env.office("submit")
    assert _end_state(env)["deploy"] == {"prod": "echo new-deploy"}
