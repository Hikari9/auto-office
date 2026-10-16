"""#571: shell-equivalent manual pushes are rejected without execution."""
import pytest
from office import planfile, shell_safety
from conftest import PLAN_ONE

@pytest.mark.parametrize("command", [
    '"git" push',
    "'git' push",
    'gi""t push',
    'g\\it push',
    'git push\n echo done',
    'env CI=1 git push',
    'sh -c "git push"',
    "eval 'git push'",
    'true; "git" push',
    "g$'it' push",
    "alias g=git; g push",
    'command $(printf git) push',
    'echo $(git push)',
])
def test_equivalent_shell_forms_are_denied(command):
    assert shell_safety.unsafe_manual_push(command)
    assert planfile._unsafe_force_push_instruction(command)

@pytest.mark.parametrize("command", ["git status", "git fetch origin main",
                                      "git show HEAD", "git log --oneline", "docker push my-image"])
def test_harmless_commands_permitted(command):
    assert not shell_safety.unsafe_manual_push(command)

def test_prohibition_non_goals_permitted():
    p = PLAN_ONE.replace("blast_radius: repo",
                         "blast_radius: repo\nnon_goals:\n- Never run git push to shared branch")
    assert not any("unsafe manual git push" in e for e in planfile.parse(p).errors)

def test_visual_start_denied():
    p = PLAN_ONE.replace("visual: none",
                         'visual:\n  url: http://127.0.0.1:8000\n  start: "git" push')
    assert any("unsafe manual git push" in e for e in planfile.parse(p).errors)

def test_visual_capture_denies_without_spawn(tmp_path, monkeypatch):
    from office import visual, paths, submit
    monkeypatch.setattr(submit, "matches_revision", lambda *args: True)
    monkeypatch.setattr(paths, "run_dir", lambda _: tmp_path / "run")
    monkeypatch.setattr(visual.subprocess, "Popen", lambda *a, **k:
                        pytest.fail("unsafe shell reached process spawn"))
    task = {"id": "T1", "visual": {"url": "http://127.0.0.1:8000",
                                      "start": "'git' push"}}
    r = visual.capture_all(None, {"id": "R1"}, task, {"id": "V1", "commit_sha": "x"},
                           {"id": "G1", "recaptures": 0}, tmp_path)
    assert r["evidence_status"] == "CAPTURE_BLOCKED"

def test_office_owned_sync_refuses_unowned_branch(monkeypatch):
    from office import prs
    monkeypatch.setattr(prs.subprocess, "run", lambda *a, **k:
                        pytest.fail("unowned branch reached subprocess"))
    run = {"id": "a" * 32}
    dispatch = {"worktree": "/tmp/mock", "task_id": "T1", "branch": "main"}
    ok, reason = prs.push(run, dispatch, commit="deadbeef")
    assert not ok and "not owned" in reason
    dispatch["branch"] = "office/" + run["id"][:8] + "/T1"
    ok, reason = prs.push(run, dispatch, commit="deadbeef", force=True, expected="bad")
    assert not ok and "complete recorded remote SHA" in reason


# --- P0 dynamic shell-wrapper admission (fail-closed) ---

@pytest.mark.parametrize("command", [
    # Authoritative P0 cases: field-splitting via unquoted expansion.
    "env -u $P origin HEAD:main",
    "timeout $D origin HEAD:main",
    "timeout -s $P origin HEAD:main",
])
def test_p0_wrapper_field_splitting_denied(command):
    assert shell_safety.unsafe_manual_push(command)
    assert planfile._unsafe_force_push_instruction(command)


@pytest.mark.parametrize("command", [
    # Variable / field-expansion operands smuggling the inner command.
    "env $X git push",
    "timeout 5 $CMD",
    "sudo $X git push",
    "nice $ADJ git push",
    "env -u $P git push",
    "env --unset=$P git push",
    "timeout --duration=$D git push",
    "timeout $D git push",
    "time -f $F git push",
    "time --format=$F git push",
    # Glob operands: unquoted expansion, unverifiable statically.
    "env $X git push",
    "timeout * git push",
    "env * git push",
    "sudo * git push",
    # Command substitution in wrapper operands.
    "timeout $(echo 5) git push",
    "timeout `echo 5` git push",
    "env -u $(id) git push",
    "env --unset=`id` git push",
    # Brace expansion forms.
    "timeout {5} git push",
    "env -u {HOME,USER} git push",
    "timeout --signal={TERM,KILL} git push",
    # Tilde / extra dynamic duration and unset variants.
    "timeout ~ git push",
    "timeout --duration=~ git push",
    "env --unset=~ git push",
    "time -f ~ git push",
    # Wrapper stacking with a dynamic link anywhere in the chain.
    "env FOO=1 timeout $D git push",
    "sudo env -u $P git push",
    "nice timeout $D git push",
    "nohup $X git push",
    "stdbuf -o $O git push",
    "command $X git push",
    "time $X git push",
])
def test_wrapper_dynamic_operands_denied(command):
    assert shell_safety.unsafe_manual_push(command), command
    assert planfile._unsafe_force_push_instruction(command), command


@pytest.mark.parametrize("command", [
    "git status",
    "git fetch origin main",
    "git show HEAD",
    "git log --oneline",
    "docker push my-image",
    # Static wrapped harmless reads stay permitted (behavior-preserving).
    "env CI=1 git status",
    "timeout 5 git status",
    "nice git status",
    "time git status",
    "nohup git status",
    "sudo git status",
    "command git status",
    "stdbuf -o0 git status",
    "time -f %e git status",
    "timeout --signal=TERM 5 git status",
    "env --unset=HOME git status",
])
def test_wrapper_static_harmless_permitted(command):
    assert not shell_safety.unsafe_manual_push(command), command
