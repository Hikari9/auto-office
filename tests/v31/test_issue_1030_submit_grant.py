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
def test_claude_reviewers_may_run_exactly_the_submit_command(kind, pane):
    a = adapters.load_all()["claude"]
    if pane:
        argv, _ = adapters.interactive_argv(a, kind, model="m", effort="high", cwd=Path("/tmp/co"), output=OUT)
    else:
        argv, _ = adapters.build_argv(a, kind, model="m", effort="high", cwd=Path("/tmp/co"), output=OUT)
    allowed = _tools(argv, "--allowedTools")
    assert [t for t in allowed if t.startswith("Bash")] == [f"Bash({EXACT})"], allowed
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


def test_pi_gets_no_submit_grant():
    a = adapters.load_all()["pi"]
    prof = adapters.profile(a, "reviewer")
    assert "submit_grant" not in prof
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
