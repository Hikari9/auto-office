"""Structured check edits use a clear contract source and local build semantics."""
import json

import pytest

from conftest import PLAN_ONE, approved_run, task_row

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}
ORIGINAL = 'python3 -c "import calc; assert calc.add(2, 3) == 5"'


def _running(env, plan=PLAN_ONE):
    approved_run(env, plan=plan, executor=[{}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)


@pytest.mark.parametrize("command", [
    "dotnet build app.csproj -c Release -nologo",
    "dotnet build app.csproj --configuration Release -nologo",
    "dotnet test app.csproj --configuration=Release",
])
def test_release_build_configuration_is_an_ordinary_check_amendment(env, command):
    _running(env)
    env.office("amend", "T1", "--drop-check", ORIGINAL, "--add-check", command,
               "--", "use the local compiler check", env=EXTERNAL, check=0)
    assert json.loads(task_row(env)["checks_json"]) == [command]
    env.office("amend", "T1", "--drop-check", command, "--add-check", ORIGINAL,
               "--", "restore the deterministic check", env=EXTERNAL, check=0)


def test_release_build_option_does_not_hide_real_external_action(env):
    _running(env)
    code, output = env.office("amend", "T1", "--add-check",
                              "dotnet build -c Release && gh release create v1",
                              "--", "add a check", env=EXTERNAL)
    assert code == 4 and "contract-level-change" in output and "release" in output
    assert json.loads(task_row(env)["checks_json"]) == [ORIGINAL]


def test_draft_and_stored_check_drift_names_both_sources(env):
    _running(env)
    draft = PLAN_ONE.replace(ORIGINAL, "python3 -m pytest tests/ -q")
    env.write_plan(draft)
    draft_path = next(env.repo.glob(".office/plans/*/PLAN.md"))
    code, output = env.office("amend", "T1", "--drop-check", ORIGINAL,
                              "--add-check", "python3 -m pytest tests/ --all",
                              "--", "replace stored check", env=EXTERNAL)
    assert code != 0 and "PLAN.md" in output and "stored" in output and "--contract" in output, output
    assert json.loads(task_row(env)["checks_json"]) == [ORIGINAL]
    assert "python3 -m pytest tests/ -q" in draft_path.read_text()


def test_ambiguous_acceptance_drop_is_refused_and_exact_drop_is_echoed(env):
    plan = PLAN_ONE.replace("- calc.add(2, 3) == 5", "- calc.add handles positive input\n- calc.add handles negative input")
    _running(env, plan)
    code, output = env.office("amend", "T1", "--drop-accept", "calc.add handles",
                              "--", "narrow the criterion", env=EXTERNAL)
    assert code != 0 and "names 2" in output
    code, output = env.office("amend", "T1", "--drop-accept", "calc.add handles positive input",
                              "--", "remove one criterion", env=EXTERNAL)
    assert code == 0 and "dropped T1 acceptance criterion: calc.add handles positive input" in output
