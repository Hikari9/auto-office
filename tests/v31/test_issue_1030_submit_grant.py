"""#1030 narrow grant (Rico, 2026-11-02: "Yes, propose narrow grant"): claude and codex reviewers may run
exactly `office review submit --file <its dispatch dir>/review-submit.txt` and nothing else; their read-only
posture is otherwise unchanged. pi gets no grant: it cannot restrict bash to one command."""
from __future__ import annotations

from pathlib import Path

import pytest

from office import adapters, review_submit

OUT = Path("/tmp/office-grant-test/D1/reply.txt")
EXACT = f"office review submit --file {OUT.parent / review_submit.SUBMIT_FILE}"


def _tools(argv, flag):
    return argv[argv.index(flag) + 1].split(",")


@pytest.mark.parametrize("kind", ["reviewer", "vision"])
@pytest.mark.parametrize("pane", [False, True])
def test_claude_reviewers_may_run_exactly_the_submit_command(kind, pane, monkeypatch):
    # The #1030 posture is what a host without Claude Code's OS sandbox keeps (#1186 widens it only where the
    # sandbox runs; tests/v31/test_issue_1186_reviewer_shell.py).
    from office import read_scope
    monkeypatch.setattr(read_scope, "claude_sandbox_available", lambda: False)
    a = adapters.load_all()["claude"]
    if pane:
        argv, _ = adapters.interactive_argv(a, kind, model="m", effort="high", cwd=Path("/tmp/co"), output=OUT)
    else:
        argv, _ = adapters.build_argv(a, kind, model="m", effort="high", cwd=Path("/tmp/co"), output=OUT)
    allowed = _tools(argv, "--allowedTools")
    assert [t for t in allowed if t.startswith("Bash")] == [f"Bash({EXACT})"], allowed
    # #1186: `--restricted` removes Bash unless `--tools` names it, which left this allow dead.
    assert "--restricted" in argv and "Bash" in _tools(argv, "--tools") and "--settings" not in argv
    denied = _tools(argv, "--disallowedTools")
    assert "Bash" not in denied, "the blanket deny would override the one allow (deny wins)"
    for cmd in review_submit.CLAUDE_READONLY_BASH:
        assert f"Bash({cmd})" in denied and f"Bash({cmd} *)" in denied, cmd
    assert "Edit" in denied and "NotebookEdit" in denied and "*" not in EXACT


def test_without_a_reply_path_the_blanket_bash_deny_stays():
    argv, _ = adapters.build_argv(adapters.load_all()["claude"], "reviewer", model="m", effort="high",
                                  cwd=Path("/tmp/co"), output=None)
    assert "Bash" in _tools(argv, "--disallowedTools") and not [t for t in _tools(argv, "--allowedTools")
                                                                  if t.startswith("Bash")]


def test_codex_reviewers_get_one_exact_execpolicy_rule_in_their_own_dispatch_dir(tmp_path):
    out = tmp_path / "D2" / "reply.txt"
    adapters.reviewer_env(adapters.load_all()["codex"], "reviewer", cwd=tmp_path, output=out)
    rules = (out.parent / ".codex" / "rules" / "office-review-submit.rules").read_text()
    exact = review_submit.submit_argv(out)
    assert f"pattern={exact!r}" in rules and 'decision="allow"' in rules and rules.count("prefix_rule(") == 1
    argv, _ = adapters.build_argv(adapters.load_all()["codex"], "reviewer", model="m", effort="high",
                                  cwd=tmp_path, output=out)
    assert "--sandbox" in argv and argv[argv.index("--sandbox") + 1] == "workspace-write", "sandbox unchanged"
    assert any("trust_level" in a and str(out.parent) in a for a in argv), "its own dispatch dir is the trusted project"


@pytest.mark.parametrize("pane", [False, True])
def test_codex_visual_reviewers_get_the_same_submit_rule_and_trust(tmp_path, pane):
    """Rico, 2026-11-15: "there should also be an office submit for visual reviewers". The codex vision profile
    had neither the grant nor a trusted dispatch dir headless, so its submit hit the sandbox."""
    a = adapters.load_all()["codex"]
    out = tmp_path / "DV" / "reply.txt"
    assert adapters.profile(a, "vision").get("submit_grant") == "codex-rules"
    adapters.reviewer_env(a, "vision", cwd=tmp_path, output=out)
    rules = (out.parent / ".codex" / "rules" / "office-review-submit.rules").read_text()
    assert f"pattern={review_submit.submit_argv(out)!r}" in rules and rules.count("prefix_rule(") == 1
    build = adapters.interactive_argv if pane else adapters.build_argv
    argv, _ = build(a, "vision", model="m", effort="high", cwd=tmp_path, output=out)
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert any("trust_level" in x and str(out.parent.resolve()) in x for x in argv), argv


def test_every_visual_reviewer_harness_can_hand_back_with_submit(monkeypatch):
    """claude (the #1186 shell), codex (the execpolicy rule) and agy (skip-permissions shell) visual reviewers can
    all run `office review submit`; every reviewer role, visual included, gets the submit identity."""
    from office import read_scope
    monkeypatch.setattr(read_scope, "claude_sandbox_available", lambda: True)
    assert "visual_reviewer" in review_submit.REVIEW_ROLES
    A = adapters.load_all()
    argv, _ = adapters.build_argv(A["claude"], "vision", model="m", effort="high", cwd=Path("/tmp/co"), output=OUT)
    assert "Bash" in _tools(argv, "--tools") and f"Bash({EXACT})" in _tools(argv, "--allowedTools")
    assert adapters.profile(A["codex"], "vision")["submit_grant"] == "codex-rules"
    assert "--dangerously-skip-permissions" in adapters.profile(A["agy"], "vision")["argv"]


def test_pi_gets_no_bash_only_its_submit_extension_grant():
    """#1231: pi never gets bash; its pane form reaches `office review submit` through one extension tool."""
    a = adapters.load_all()["pi"]
    prof = adapters.profile(a, "reviewer")
    assert prof["submit_grant"] == "pi-extension"
    for argv in (prof["argv"], prof["interactive"]["argv"]):
        assert "bash" not in argv[argv.index("--tools") + 1].split(",")


def test_workers_are_untouched():
    argv, _ = adapters.build_argv(adapters.load_all()["claude"], "worker", model="m", effort="high", cwd=Path("/tmp/co"))
    assert not any("office review submit" in a for a in argv)


def test_a_reply_path_with_spaces_stays_one_exact_argument(tmp_path):
    out = tmp_path / "Application Support" / "D3" / "reply.txt"
    cmd = review_submit.submit_command(out)
    assert cmd.endswith(f"'{out.parent / review_submit.SUBMIT_FILE}'") and review_submit.submit_argv(out)[-1] == str(
        out.parent / review_submit.SUBMIT_FILE)
    adapters.reviewer_env(adapters.load_all()["codex"], "reviewer", cwd=tmp_path, output=out)
    rules = (out.parent / ".codex" / "rules" / "office-review-submit.rules").read_text()
    assert repr(str(out.parent / review_submit.SUBMIT_FILE)) in rules
