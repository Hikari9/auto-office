"""Reviewers read Office state and global guidance outside the checkout (shared read roots)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import SRC  # noqa: F401  (puts src on sys.path via the env fixture)


@pytest.fixture
def scope(env, monkeypatch):
    """A fake home holding the guidance dirs, and the office modules under test."""
    monkeypatch.setenv("HOME", str(env.home))
    for d in (".claude/rules", ".claude/skills"):
        (env.home / d).mkdir(parents=True, exist_ok=True)
    from office import adapters, read_scope
    return env, adapters, read_scope


def _settings(env) -> Path:
    return env.home / ".claude" / "settings.json"


def _claude_install(env):
    return env.office("install", "--only", "claude", check=0)[1]


def test_install_writes_allow_rules_once_and_preserves_other_settings(scope):
    env, _, read_scope = scope
    mine = "Bash(git status)"
    _settings(env).write_text(json.dumps({"theme": "dark", "permissions": {"allow": [mine], "deny": ["Read(.env)"]}}))
    out = _claude_install(env)
    assert "reviewer read rules added" in out, out
    data = json.loads(_settings(env).read_text())
    allow = data["permissions"]["allow"]
    assert data["theme"] == "dark" and data["permissions"]["deny"] == ["Read(.env)"]
    assert allow[0] == mine
    for rule in read_scope.allow_rules():
        assert allow.count(rule) == 1, rule
    # Both absolute spellings cover Office state: Claude Code's `//abs` and context-mode's literal `/abs`.
    state = str(env.state)
    assert f"Read({state}/**)" in allow and f"Read(/{state}/**)" in allow
    assert f"Read({env.home}/.claude/rules/**)" in allow
    assert not any(".credentials" in r or "auth.json" in r for r in allow)

    before = _settings(env).read_text()
    backups = list((env.home / ".claude").glob("settings.json.bak.*"))
    out = _claude_install(env)
    assert "reviewer read rules already current" in out, out
    assert _settings(env).read_text() == before
    assert list((env.home / ".claude").glob("settings.json.bak.*")) == backups


def test_install_creates_permissions_when_absent_and_backs_up(scope):
    env, _, read_scope = scope
    _settings(env).write_text(json.dumps({"theme": "dark"}))
    _claude_install(env)
    data = json.loads(_settings(env).read_text())
    assert data["permissions"]["allow"] == read_scope.allow_rules()
    assert list((env.home / ".claude").glob("settings.json.bak.*"))


def test_uninstall_removes_only_office_rules(scope):
    env, _, read_scope = scope
    # A user's own copy of one wanted rule stays theirs.
    theirs = read_scope.allow_rules()[0]
    _settings(env).write_text(json.dumps({"permissions": {"allow": [theirs, "Bash(ls)"]}}))
    _claude_install(env)
    env.office("uninstall", check=0)
    data = json.loads(_settings(env).read_text())
    assert data["permissions"]["allow"] == [theirs, "Bash(ls)"]
    assert not read_scope.ledger_path().exists()


def test_uninstall_drops_containers_office_created(scope):
    env, _, read_scope = scope
    _settings(env).write_text(json.dumps({"theme": "dark"}))
    _claude_install(env)
    env.office("uninstall", check=0)
    data = json.loads(_settings(env).read_text())
    assert "permissions" not in data and data["theme"] == "dark"


def test_install_leaves_unexpected_permissions_shape_alone(scope):
    env, _, _ = scope
    _settings(env).write_text(json.dumps({"permissions": "strict"}))
    _claude_install(env)
    assert json.loads(_settings(env).read_text())["permissions"] == "strict"


def test_reinstall_removes_owned_rules_for_a_moved_root(scope, monkeypatch):
    env, _, read_scope = scope
    _settings(env).write_text("{}")
    _claude_install(env)
    old_state = str(env.state)
    moved = env.tmp / "state2"
    moved.mkdir()
    monkeypatch.setenv("OFFICE_STATE_HOME", str(moved))
    env.office("install", "--only", "claude", env={"OFFICE_STATE_HOME": str(moved)}, check=0)
    allow = json.loads(_settings(env).read_text())["permissions"]["allow"]
    assert f"Read({moved.resolve()}/**)" in allow
    assert f"Read({old_state}/**)" not in allow


def test_doctor_treats_absent_settings_as_empty(scope):
    env, _, read_scope = scope
    assert not _settings(env).exists()
    code, out = env.office("doctor")
    total = len(read_scope.allow_rules())
    assert f"reviewer read rules 0/{total}" in out and code == 1, out
    assert not _settings(env).exists()  # doctor never creates it
    _claude_install(env)
    code, out = env.office("doctor")
    assert f"reviewer read rules {total}/{total}" in out, out


def test_doctor_reports_rules_present_and_missing(scope):
    env, _, read_scope = scope
    _settings(env).write_text("{}")
    code, out = env.office("doctor")
    total = len(read_scope.allow_rules())
    assert f"reviewer read rules 0/{total}" in out and code == 1, out
    _claude_install(env)
    code, out = env.office("doctor")
    assert f"reviewer read rules {total}/{total}" in out and "ok" in out, out


def _reviewer_argv(adapters, adapter_id, kind, env, **kw):
    adapter = adapters.load_all()[adapter_id]
    out = env.tmp / "dispatch" / "reply.txt"
    return adapters.build_argv(adapter, kind, model="m", effort="high", cwd=env.repo, output=out,
                               include_dirs=[env.repo], **kw)[0]


@pytest.mark.parametrize("adapter_id,flag", [("claude", "--add-dir"), ("gemini", "--include-directories")])
@pytest.mark.parametrize("kind", ["reviewer", "vision"])
def test_reviewer_argv_includes_read_roots(scope, adapter_id, flag, kind):
    env, adapters, read_scope = scope
    if kind not in (adapters.load_all()[adapter_id].get("office_profiles") or {}):
        pytest.skip(f"{adapter_id} has no {kind} profile")
    argv = _reviewer_argv(adapters, adapter_id, kind, env)
    granted = [argv[i + 1] for i, a in enumerate(argv) if a == flag]
    assert str(env.repo) in granted
    for d in read_scope.add_dirs():
        assert str(d) in granted, (d, argv)
    assert str(env.state) in granted and str(env.home / ".claude" / "rules") in granted


def test_agy_vision_argv_includes_read_roots(scope):
    env, adapters, read_scope = scope
    argv = _reviewer_argv(adapters, "agy", "vision", env)
    granted = [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]
    assert [str(d) for d in read_scope.add_dirs()] == [g for g in granted if g != str(env.repo)]


def test_interactive_reviewer_argv_includes_read_roots(scope):
    env, adapters, read_scope = scope
    adapter = adapters.load_all()["claude"]
    args, _ = adapters.interactive_argv(adapter, "reviewer", model="m", effort="high", cwd=env.repo,
                                        include_dirs=[env.repo], output=env.tmp / "d" / "reply.txt")
    assert str(env.state) in args and str(env.tmp / "d") in args


def test_read_roots_skip_missing_directories(scope):
    env, _, read_scope = scope
    (env.home / ".claude" / "skills").rmdir()
    assert env.home / ".claude" / "skills" not in read_scope.add_dirs()
    # The allow rules do not depend on the directory existing yet.
    assert f"Read({env.home}/.claude/skills/**)" in read_scope.allow_rules()


@pytest.mark.parametrize("adapter_id", ["claude", "codex", "gemini", "agy", "hermes"])
def test_worker_argv_is_unchanged(scope, adapter_id):
    env, adapters, read_scope = scope
    adapter = adapters.load_all()[adapter_id]
    if "worker" not in (adapter.get("office_profiles") or {}):
        pytest.skip(f"{adapter_id} has no worker profile")
    with_dirs, _ = adapters.build_argv(adapter, "worker", model="m", effort="high", cwd=env.repo, include_dirs=None)
    assert not any(str(d) in with_dirs for d in read_scope.add_dirs())
    # Declared include_dirs are not widened either; a worker profile has no slot for read roots.
    widened, _ = adapters.build_argv(adapter, "worker", model="m", effort="high", cwd=env.repo, include_dirs=[env.tmp])
    assert widened == with_dirs
    assert read_scope.with_read_dirs("worker", None) == []


def test_codex_reviewer_writes_only_the_dispatch_dir(scope):
    env, adapters, _ = scope
    out = env.tmp / "dispatch" / "reply.txt"
    for kind in ("reviewer", "vision"):
        argv = _reviewer_argv(adapters, "codex", kind, env)
        assert argv[argv.index("--sandbox") + 1] == "workspace-write"
        assert argv[argv.index("--cd") + 1] == str(out.parent)
        assert str(env.repo) not in argv
        inter, _ = adapters.interactive_argv(adapters.load_all()["codex"], kind, model="m", effort="high",
                                             cwd=env.repo, output=out)
        assert inter[inter.index("--cd") + 1] == str(out.parent)
        assert str(env.repo) not in " ".join(inter)
        assert str(out.parent.resolve()) in " ".join(inter)  # the pane trusts the dispatch dir


def test_codex_worker_still_runs_in_the_worktree(scope):
    env, adapters, _ = scope
    argv, _ = adapters.build_argv(adapters.load_all()["codex"], "worker", model="m", effort="high", cwd=env.repo)
    assert argv[argv.index("--cd") + 1] == str(env.repo)


def test_claude_reviewer_cannot_edit_the_checkout_or_office_data(scope):
    env, adapters, _ = scope
    out = env.tmp / "dispatch" / "reply.txt"
    argv = _reviewer_argv(adapters, "claude", "reviewer", env)
    denied = argv[argv.index("--disallowedTools") + 1].split(",")
    assert {"Edit", "Bash", "NotebookEdit"} <= set(denied)
    # `Edit(path)` denies also block the Write tool; the reply file stays writable.
    assert f"Edit(/{env.repo}/**)" in denied and f"Edit(/{env.data}/**)" in denied
    assert f"Edit(/{env.state}/worktrees/**)" in denied
    assert not any(str(out.parent) in d for d in denied)


def test_claude_denial_skips_a_cwd_that_is_the_dispatch_dir(scope):
    env, adapters, read_scope = scope
    out = env.tmp / "dispatch" / "reply.txt"
    deny = read_scope.write_denials(out.parent, out)
    assert f"Edit(/{out.parent}/**)" not in deny and f"Edit(/{env.data}/**)" in deny


def _can_write(argv: list[str], target: Path, inherited_allow: tuple[str, ...] = ()) -> bool:
    """Claude Code's decision for a file write under `argv`'s rules: a deny rule
    wins, then an allow rule, else `dontAsk` denies. Only `Edit(path)` rules are
    consulted for file writes; `//abs` is an absolute path. `inherited_allow` are
    allow rules from the user/project/local settings files, which `--restricted`
    makes Claude Code ignore. A bare `Write` or `Edit` allow grants every path."""
    mode = argv[argv.index("--permission-mode") + 1]
    allowed = argv[argv.index("--allowedTools") + 1].split(",")
    denied = argv[argv.index("--disallowedTools") + 1].split(",")
    if "--restricted" not in argv:
        allowed = allowed + list(inherited_allow)

    def matches(rules: list[str]) -> bool:
        for rule in rules:
            if rule in ("Write", "Edit"):
                return True
            if not rule.startswith("Edit(//"):
                continue
            pat = rule[len("Edit(/"):-1]
            if pat.endswith("/**"):
                if str(target).startswith(pat[:-2]):
                    return True
            elif str(target) == pat:
                return True
        return False

    # Bare `Edit` in the deny list removes the Edit tool, not the Write tool.
    if matches([d for d in denied if d != "Edit"]):
        return False
    return matches(allowed) or mode != "dontAsk"


@pytest.mark.parametrize("kind", ["reviewer", "vision"])
def test_claude_reviewer_writes_only_to_its_dispatch_dir(scope, kind):
    env, adapters, _ = scope
    out = env.tmp / "dispatch" / "reply.txt"
    other = env.state / "runs" / "other-run" / "dispatches" / "Dxyz" / "reply.txt"
    argv = _reviewer_argv(adapters, "claude", kind, env)
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert "Write" not in argv[argv.index("--allowedTools") + 1].split(",")
    assert _can_write(argv, out)
    assert _can_write(argv, out.parent / "notes.md")
    home = env.home
    for target in (home / ".claude" / "rules" / "x.md", home / ".claude" / "settings.json",
                   home / ".claude" / "skills" / "s" / "SKILL.md", home / ".codex" / "AGENTS.md",
                   home / ".agents" / "skills" / "a.md", home / "AGENTS.md", home / "CLAUDE.md",
                   other, env.repo / "src" / "f.py", env.state / "worktrees" / "r" / "T1" / "f.py",
                   env.data / "runs.db"):
        assert not _can_write(argv, target), target


def test_claude_reviewer_without_a_reply_path_writes_nothing(scope):
    env, adapters, _ = scope
    adapter = adapters.load_all()["claude"]
    argv, _ = adapters.build_argv(adapter, "reviewer", model="m", effort="high", cwd=env.repo, output=None)
    assert not _can_write(argv, env.tmp / "dispatch" / "reply.txt")
    assert not _can_write(argv, env.repo / "f.py")


def test_claude_interactive_reviewer_writes_only_to_its_dispatch_dir(scope):
    env, adapters, _ = scope
    out = env.tmp / "dispatch" / "reply.txt"
    args, _ = adapters.interactive_argv(adapters.load_all()["claude"], "reviewer", model="m", effort="high",
                                        cwd=env.repo, include_dirs=[env.repo], output=out)
    assert _can_write(args, out)
    assert not _can_write(args, env.home / ".claude" / "rules" / "x.md")
    assert not _can_write(args, env.state / "runs" / "other-run" / "dispatches" / "Dxyz" / "reply.txt")


def test_claude_worker_argv_has_no_reviewer_write_rules(scope):
    env, adapters, _ = scope
    argv, _ = adapters.build_argv(adapters.load_all()["claude"], "worker", model="m", effort="high", cwd=env.repo)
    assert "--allowedTools" not in argv and "dontAsk" not in argv


@pytest.mark.parametrize("kind", ["reviewer", "vision"])
def test_preexisting_broad_write_allow_does_not_widen_a_claude_reviewer(scope, kind):
    """The operator's own settings may allow writes anywhere. A reviewer launch
    ignores them (`--restricted`), so a sibling run's dispatch stays unwritable."""
    env, adapters, _ = scope
    sibling = env.state / "runs" / "other-run" / "dispatches" / "Dxyz" / "reply.txt"
    own = env.tmp / "dispatch" / "reply.txt"
    broad = ("Write", "Edit", f"Edit(/{env.state}/**)", f"Edit(/{env.home}/**)")
    argv = _reviewer_argv(adapters, "claude", kind, env)
    assert "--restricted" in argv
    assert not _can_write(argv, sibling, inherited_allow=broad)
    assert not _can_write(argv, env.home / "scratch.txt", inherited_allow=broad)
    assert _can_write(argv, own, inherited_allow=broad)
    # The model is not vacuous: without `--restricted` the same settings do widen the fence.
    unrestricted = [a for a in argv if a != "--restricted"]
    assert _can_write(unrestricted, sibling, inherited_allow=broad)
    inter, _ = adapters.interactive_argv(adapters.load_all()["claude"], kind, model="m", effort="high",
                                         cwd=env.repo, include_dirs=[env.repo], output=own)
    assert "--restricted" in inter
    assert not _can_write(inter, sibling, inherited_allow=broad)


def test_claude_worker_is_not_restricted(scope):
    env, adapters, _ = scope
    argv, _ = adapters.build_argv(adapters.load_all()["claude"], "worker", model="m", effort="high", cwd=env.repo)
    assert "--restricted" not in argv
