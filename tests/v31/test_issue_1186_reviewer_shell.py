"""#1186 (Rico, 2026-11-15: "Fix it by allowing `office review submit` command when office launches the reviewer.
Honestly also some bash commands should be allowed too by reviewers, as long as safe ones."):

`--restricted` removes Bash from a Claude reviewer unless `--tools` names it, so the #1030 submit allow never
applied. A Claude reviewer now names its tools, and where Claude Code's OS sandbox runs it gets a safe shell:
the built-in read-only commands plus targeted test runners, every call sandboxed with no network and no writes
outside its dispatch dir and temp, and only the exact submit command run outside the sandbox."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import SRC  # noqa: F401  (puts src on sys.path via the env fixture)


@pytest.fixture
def shell(env, monkeypatch):
    monkeypatch.setenv("HOME", str(env.home))
    for d in (".claude/rules", ".claude/skills"):
        (env.home / d).mkdir(parents=True, exist_ok=True)
    from office import adapters, read_scope, review_submit
    monkeypatch.setattr(read_scope, "claude_sandbox_available", lambda: True)
    return env, adapters, read_scope, review_submit


def _out(env) -> Path:
    out = env.state / "runs" / "R1" / "dispatches" / "D1" / "reply.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


def _argv(adapters, env, kind="reviewer", pane=False, output=None):
    build = adapters.interactive_argv if pane else adapters.build_argv
    argv, _ = build(adapters.load_all()["claude"], kind, model="m", effort="high", cwd=env.repo,
                    include_dirs=[env.repo], output=output)
    return argv


def _list(argv, flag):
    return argv[argv.index(flag) + 1].split(",")


def _sandbox(argv) -> dict:
    return json.loads(argv[argv.index("--settings") + 1])["sandbox"]


@pytest.mark.parametrize("kind", ["reviewer", "vision"])
@pytest.mark.parametrize("pane", [False, True])
def test_a_claude_reviewer_names_bash_and_runs_it_sandboxed(shell, kind, pane):
    env, adapters, _, review_submit = shell
    out = _out(env)
    argv = _argv(adapters, env, kind, pane, out)
    assert "--restricted" in argv and argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert {"Read", "Grep", "Glob", "Write", "Bash"} <= set(_list(argv, "--tools"))
    sb = _sandbox(argv)
    assert sb["enabled"] and sb["failIfUnavailable"] and sb["allowUnsandboxedCommands"] is False
    assert sb["autoAllowBashIfSandboxed"] is False and sb["network"] == {"allowedDomains": []}
    assert sb["excludedCommands"] == [review_submit.submit_command(out)], "only the exact submit leaves the sandbox"


def test_reviewers_may_run_targeted_tests_and_submit_but_not_mutate(shell):
    env, adapters, _, review_submit = shell
    out = _out(env)
    argv = _argv(adapters, env, output=out)
    allowed, denied = _list(argv, "--allowedTools"), _list(argv, "--disallowedTools")
    assert f"Bash({review_submit.submit_command(out)})" in allowed
    for cmd in ("pytest", "python3 -m pytest", ".venv/bin/python -m pytest", "pnpm test", "node --test"):
        assert f"Bash({cmd} *)" in allowed, cmd
    assert "Bash" not in denied, "a blanket deny would beat every allow"
    for cmd in ("rm", "git push", "git commit", "git checkout", "git reset", "curl", "wget", "pip install",
                "pnpm install", "npm install", "sudo", "find * -delete", "find * -exec"):
        assert f"Bash({cmd})" in denied and f"Bash({cmd} *)" in denied, cmd
    # Claude Code's built-in read-only commands (git log/diff/show, ls, cat, grep, ...) are no longer denied.
    for cmd in ("git", "cat", "ls", "grep", "find"):
        assert f"Bash({cmd} *)" not in denied, cmd
    assert not any(r.startswith("Bash") and "*" in r.split(" ", 1)[0] for r in allowed), "no wildcard command"


def test_the_sandbox_denies_writes_everywhere_but_the_dispatch_dir(shell):
    env, adapters, _, _ = shell
    out = _out(env)
    (env.state / "runs" / "R1" / "dispatches" / "D0").mkdir(parents=True)
    (env.state / "runs" / "OTHER").mkdir(parents=True)
    (env.state / "worktrees").mkdir(parents=True, exist_ok=True)
    deny = set(_sandbox(_argv(adapters, env, output=out))["filesystem"]["denyWrite"])
    for path in (env.repo, env.data, env.state / "worktrees", env.home / ".claude", env.home / "AGENTS.md",
                 env.state / "runs" / "OTHER", env.state / "runs" / "R1" / "dispatches" / "D0"):
        assert str(path) in deny, path
    # Deny beats every allow in the sandbox, so nothing on the path to the dispatch dir may be denied.
    for keep in (out.parent, *out.parent.parents):
        assert str(keep) not in deny, keep


def test_a_vision_reviewer_whose_cwd_is_its_dispatch_dir_keeps_it_writable(shell):
    env, adapters, read_scope, _ = shell
    out = _out(env)
    (out.parent / "image.png").write_bytes(b"x")
    deny = read_scope.sandbox_write_denials(out.parent, out, [out.parent])
    assert not any(d == str(out.parent) or d.startswith(str(out.parent) + "/") for d in deny)
    assert str(env.data) in deny and str(env.state / "runs" / "R1" / "dispatches") not in deny


def test_without_a_sandbox_the_reviewer_keeps_the_submit_only_shell(shell, monkeypatch):
    env, adapters, read_scope, review_submit = shell
    monkeypatch.setattr(read_scope, "claude_sandbox_available", lambda: False)
    out = _out(env)
    for pane in (False, True):
        argv = _argv(adapters, env, pane=pane, output=out)
        assert "--settings" not in argv and "{sandbox_settings}" not in " ".join(argv)
        assert "Bash" in _list(argv, "--tools")
        assert [r for r in _list(argv, "--allowedTools") if r.startswith("Bash")] == [
            f"Bash({review_submit.submit_command(out)})"]
        assert "Bash(git *)" in _list(argv, "--disallowedTools")


def test_without_a_reply_path_bash_stays_denied(shell):
    env, adapters, _, _ = shell
    argv = _argv(adapters, env, output=None)
    assert "Bash" in _list(argv, "--disallowedTools") and "--settings" not in argv
    assert not [r for r in _list(argv, "--allowedTools") if r.startswith("Bash")]


def test_sandbox_availability_follows_the_platform(monkeypatch):
    from office import read_scope
    monkeypatch.setattr(read_scope.sys, "platform", "darwin")
    assert read_scope.claude_sandbox_available()
    monkeypatch.setattr(read_scope.sys, "platform", "linux")
    monkeypatch.setattr(read_scope.shutil, "which", lambda name: None)
    assert not read_scope.claude_sandbox_available()
    monkeypatch.setattr(read_scope.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert read_scope.claude_sandbox_available()
    monkeypatch.setattr(read_scope.sys, "platform", "win32")
    assert not read_scope.claude_sandbox_available()


@pytest.mark.skipif(os.environ.get("OFFICE_LIVE_CLAUDE") != "1" or not shutil.which("claude"),
                    reason="live Claude Code check: set OFFICE_LIVE_CLAUDE=1 with claude on PATH")
def test_live_claude_reviewer_tool_list_has_bash(tmp_path):
    """The rendered reviewer argv, launched for real, reports Bash among its tools (the argv-only checks of
    #1030 missed that `--restricted` dropped it). The model name does not exist, so Claude Code prints its
    init event, tools included, and stops without a model reply."""
    from office import adapters
    out = tmp_path / "dispatch" / "reply.txt"
    out.parent.mkdir(parents=True)
    argv, _ = adapters.build_argv(adapters.load_all()["claude"], "reviewer", model="office-no-such-model",
                                  effort="low", cwd=tmp_path, output=out)
    argv = [a for a in argv if a != "text"]
    argv[argv.index("--output-format"):argv.index("--output-format") + 1] = ["--verbose", "--output-format",
                                                                              "stream-json"]
    proc = subprocess.run(argv, input="hi", cwd=tmp_path, capture_output=True, text=True, timeout=120)
    init = next(json.loads(line) for line in proc.stdout.splitlines()
                if line.startswith("{") and json.loads(line).get("subtype") == "init")
    tools = {t for t in init["tools"] if not t.startswith("mcp__")}
    assert {"Bash", "Read", "Write"} <= tools, (tools, proc.stderr[-2000:])
