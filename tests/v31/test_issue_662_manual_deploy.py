"""#662: manual handoff text must never be mistaken for a deploy command."""
from contextlib import nullcontext

import pytest

from office import land, planfile
from office.result import Result
from office.state import Refused


HANDOFF = "manual: actions A3-A5 (handoff); no web deploy in this run"


@pytest.mark.parametrize("mode,commands", [
    ("e2e", {"prod": HANDOFF}),
    ("preview", {"preview": HANDOFF}),
    ("e2e", {"prod": "echo deployed", "verify": HANDOFF}),
])
def test_manual_deploy_placeholder_is_invalid_in_plan(mode, commands):
    errors, _warnings = planfile.end_state_problems(mode, commands)
    assert any("manual" in e and "deploy_" in e for e in errors), errors


@pytest.mark.parametrize("mode,commands", [
    ("e2e", {"prod": "echo shipped"}),
    ("preview", {"preview": "python3 -c 'print(123)'"}),
    ("merge", {"prod": HANDOFF}),
])
def test_valid_commands_and_nonexecuting_modes_are_preserved(mode, commands):
    errors, _warnings = planfile.end_state_problems(mode, commands)
    assert not errors, errors


@pytest.mark.parametrize("deploy", [
    {"prod": HANDOFF},
    {"prod": "echo deployed", "verify": HANDOFF},
])
def test_land_refuses_manual_placeholder_before_any_merging(monkeypatch, deploy):
    called = []
    monkeypatch.setattr(land, "_snapshot", lambda *a: {"commit": "deadbeef"})
    monkeypatch.setattr(land, "end_state", lambda *a: {"mode": "e2e", "deploy": deploy})
    monkeypatch.setattr(land, "_land_lock", lambda *a: nullcontext())
    monkeypatch.setattr(land, "_require_unchanged", lambda *a, **k: None)
    monkeypatch.setattr(land, "_land_merge", lambda *a, **k: called.append(True) or Result())
    with pytest.raises(Refused) as caught:
        land.land(None, {"id": "deadbeef", "repo_root": "/tmp/unmodified"})
    assert caught.value.category == "manual-deploy-command"
    assert not called, "land must refuse before any PR is merged"


@pytest.mark.parametrize("override", ["merge", "preview"])
def test_explicit_override_of_saved_e2e_run_still_refuses_manual_placeholder(monkeypatch, override):
    called = []
    monkeypatch.setattr(land, "_snapshot", lambda *a: {"commit": "deadbeef"})
    monkeypatch.setattr(land, "end_state", lambda *a: {"mode": "e2e", "deploy": {"prod": HANDOFF}})
    monkeypatch.setattr(land, "_authorize", lambda *a, **k: called.append("authorize"))
    monkeypatch.setattr(land, "_land_lock", lambda *a: nullcontext())
    monkeypatch.setattr(land, "_require_unchanged", lambda *a, **k: None)
    monkeypatch.setattr(land, "_land_merge", lambda *a, **k: called.append("merge") or Result())
    monkeypatch.setattr(land, "_land_preview", lambda *a, **k: called.append("preview") or Result())
    with pytest.raises(Refused) as caught:
        land.land(None, {"id": "deadbeef"}, mode=override, quote="land it now")
    assert caught.value.category == "manual-deploy-command"
    assert not called, "no authorization or merge before the refusal"


def test_manual_placeholder_is_rejected_during_plan_submit(env):
    from test_land import _plan
    code, out = env.office("start", "manual deploy case", "--planner", "inline")
    assert code == 0, out
    env.write_plan(_plan("e2e", prod=HANDOFF))
    code, out = env.office("submit")
    assert code == 4, out
    assert "manual" in out and "deploy_prod" in out, out


@pytest.mark.parametrize("value", ["manual", "manual: handoff", "none", "n/a", "skip", "no deploy", "not applicable"])
def test_manual_sentinels_are_recognized(value):
    assert planfile.manual_deploy_placeholder(value)


def test_valid_executable_command_containing_word_manual_is_not_blocked():
    assert not planfile.manual_deploy_placeholder("echo 'manual: this is a valid shell argument'")


def test_deploy_internal_guard_refuses_before_env_copy_or_subprocess(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setattr(land.shutil, "copytree", lambda *a, **k: pytest.fail("copied env before refusal"))
    monkeypatch.setattr(land.subprocess, "run", lambda *a, **k: pytest.fail("spawned manual handoff as command"))
    with pytest.raises(Refused) as caught:
        land._deploy(None, {"id": "R1", "repo_root": str(tmp_path)}, "prod",
                     {"prod": HANDOFF}, checkout, Result(), commit="sha", tree="tree")
    assert caught.value.category == "manual-deploy-command"


PROSE_PLACEHOLDERS = [
    "manual handoff", "manual deployment required", "Manual deploy: see runbook", "hand off to ops",
    "Handoff to the release manager", "deploy by hand", "deploy manually after merge", "TBD", "todo",
    "TODO: add deploy", "N/A", "n/a - handled elsewhere", "operator will deploy", "Human deploys this",
    "Deployment is handled by a human", "handoff required", "manual; see runbook.", "No deploy in this run",
    "pending manual approval", "Ask the operator to deploy", "run deploy by hand", "deferred to ops",
    "Handled by hand after merge",
]
REAL_COMMANDS = [
    "./scripts/manual-deploy.sh", "make manual", "ansible-playbook manual.yml", "npm run manual-deploy",
    "FOO=1 ./deploy.sh --manual", "bash scripts/handoff.sh", "python3 tools/handoff.py --operator ci",
    "uv run handoff", "./deploy --manual", "cd app && make manual", "echo manual",
    "kubectl apply -f manual.yaml", "deploy --manual", "vercel deploy --prod",
]


@pytest.mark.parametrize("value", PROSE_PLACEHOLDERS)
def test_colonless_handoff_prose_is_a_placeholder(value):
    assert planfile.manual_deploy_placeholder(value)
    errors, _ = planfile.end_state_problems("e2e", {"prod": value})
    assert any("deploy_prod" in e for e in errors), errors


@pytest.mark.parametrize("value", REAL_COMMANDS)
def test_real_commands_mentioning_manual_or_handoff_are_accepted(value):
    assert not planfile.manual_deploy_placeholder(value)
    errors, _ = planfile.end_state_problems("e2e", {"prod": value})
    assert not errors, errors


@pytest.mark.parametrize("value", ["manual handoff", "manual deployment required"])
def test_colonless_prose_refused_before_merge_on_land(monkeypatch, value):
    called = []
    monkeypatch.setattr(land, "_snapshot", lambda *a: {"commit": "deadbeef"})
    monkeypatch.setattr(land, "end_state", lambda *a: {"mode": "e2e", "deploy": {"prod": value}})
    monkeypatch.setattr(land, "_land_lock", lambda *a: nullcontext())
    monkeypatch.setattr(land, "_require_unchanged", lambda *a, **k: None)
    monkeypatch.setattr(land, "_land_merge", lambda *a, **k: called.append(True) or Result())
    with pytest.raises(Refused) as caught:
        land.land(None, {"id": "deadbeef", "repo_root": "/tmp/unmodified"})
    assert caught.value.category == "manual-deploy-command"
    assert not called


@pytest.mark.parametrize("value", ["manual handoff", "manual deployment required"])
def test_colonless_prose_is_rejected_during_plan_submit(env, value):
    from test_land import _plan
    code, out = env.office("start", "manual deploy case", "--planner", "inline")
    assert code == 0, out
    env.write_plan(_plan("e2e", prod=value))
    code, out = env.office("submit")
    assert code == 4, out
    assert "deploy_prod" in out, out


# Round 3: classification is structural. Punctuation never makes prose a command.
PUNCTUATED_PROSE = [
    "deploy manually (after merge)", "ask ops to deploy (see runbook)", "deploy: manual", "manual (human step)",
    "deploy manually [after merge]", "deploy manually - after merge", "deploy manually — after merge",
    "deploy after merge; ops will run it", "ask ops: deploy (see runbook)", "ops deploys 'after merge'",
    "ask the release manager to deploy (see \"runbook\")", "someone deploys it (not automated)",
    "run the deploy in the dashboard (manual)", "see runbook (docs/deploy.md)", "see docs/runbook.md",
    "contact ops/devops to deploy", "wait for approval (human), then deploy", "deploy (manual step)",
    "(manual) deploy after merge", "[manual] deploy", "'manual' deploy", "\"deploy manually\"",
    "just deploy manually", "make sure ops deploys it", "go to the dashboard and click deploy (manual)",
    "test the deploy manually (staging first)", "time to deploy by hand (after merge)",
    "ship it: ask ops (see slack)", "release: manual; owner=ops", "after merge, ops deploys",
    "Deploy after merge (manual)", "Echo to ops that merge is done", "set up the deploy by hand (human)",
    "open the Vercel dashboard (https://vercel.com) and promote", "deploy via the ticket (JIRA-12)",
    "hand-off to ops", "deploy", "ops", "read the runbook then deploy (manual)", "type deploy in slack",
    "kill the old deploy and wait (human)", "wait for ops", "deploy-by-hand (ops)", "N/A (handled by ops)",
    "n/a (see runbook)", "cd to the server and deploy manually (ssh)",
]
STRUCTURAL_COMMANDS = [
    "flyctl deploy", "gh workflow run deploy.yml", "rsync -a build/ host:/srv", "docker compose up -d",
    "terraform apply -auto-approve", "vercel deploy --prod", "make deploy", "just deploy prod", "go run ./cmd/deploy",
    "cd app && make deploy", "(cd app && make deploy)", "FOO=1 ./deploy.sh", "./scripts/deploy.sh --env prod",
    "bin/deploy", "npm run deploy", "pnpm deploy --prod", "kubectl apply -f k8s/", "helm upgrade --install app ./chart",
    "ssh host 'systemctl restart app'", "curl -fsS https://example.test/health", "bash -c 'echo hi'",
    "deploy-tool --env prod", "deployctl --prod", "cap production deploy", "sudo systemctl restart app",
    "time make deploy", "set -a && . ./.env && vercel deploy --prod", "source scripts/deploy.sh x", "echo shipped",
    "make manual", "echo 'manual: x'", "test -f build/index.html", "[ -f x ] && ./deploy.sh", "git push heroku main",
    "aws s3 sync build s3://bucket", "wrangler deploy", "firebase deploy --only hosting", "$DEPLOY_CMD",
    "exec ./deploy.sh", "env FOO=1 make deploy", "uv run python -m deploy", "task deploy", "mvn -q deploy",
    "my-deploy-tool", "deploy_prod.sh", "kamal deploy", "sam deploy --guided", "python3 deploy.py",
]

@pytest.mark.parametrize("value", PUNCTUATED_PROSE)
def test_punctuated_prose_is_a_placeholder_in_plan_and_land_guard(value):
    assert planfile.manual_deploy_placeholder(value)
    errors, _ = planfile.end_state_problems("e2e", {"prod": value})
    assert any("deploy_prod" in e for e in errors), errors
    with pytest.raises(Refused) as caught:
        land._require_executable_deploy("e2e", {"prod": value})
    assert caught.value.category == "manual-deploy-command"


@pytest.mark.parametrize("value", STRUCTURAL_COMMANDS)
def test_structurally_real_commands_pass_plan_and_land_guard(value):
    assert not planfile.manual_deploy_placeholder(value)
    errors, _ = planfile.end_state_problems("e2e", {"prod": value})
    assert not any("manual handoff" in e for e in errors), errors
    land._require_executable_deploy("e2e", {"prod": value})


def test_parenthetical_prose_refused_before_merge_on_land(monkeypatch):
    called = []
    monkeypatch.setattr(land, "_snapshot", lambda *a: {"commit": "deadbeef"})
    monkeypatch.setattr(land, "end_state", lambda *a: {"mode": "e2e", "deploy": {"prod": "deploy manually (after merge)"}})
    monkeypatch.setattr(land, "_land_lock", lambda *a: nullcontext())
    monkeypatch.setattr(land, "_require_unchanged", lambda *a, **k: None)
    monkeypatch.setattr(land, "_land_merge", lambda *a, **k: called.append(True) or Result())
    with pytest.raises(Refused) as caught:
        land.land(None, {"id": "deadbeef", "repo_root": "/tmp/unmodified"})
    assert caught.value.category == "manual-deploy-command"
    assert not called


def test_parenthetical_prose_is_rejected_during_plan_submit(env):
    from test_land import _plan
    code, out = env.office("start", "manual deploy case", "--planner", "inline")
    assert code == 0, out
    env.write_plan(_plan("e2e", prod="deploy manually (after merge)"))
    code, out = env.office("submit")
    assert code == 4, out
    assert "deploy_prod" in out, out
