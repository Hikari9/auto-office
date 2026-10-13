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
