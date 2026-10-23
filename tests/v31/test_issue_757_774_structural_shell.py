"""Plan commands keep quoted program data separate from executable shell syntax."""
import pytest

from conftest import PLAN_ONE
from office import planfile, shell_safety


@pytest.mark.parametrize("command", [
    'node -e "a[0]; b"',
    'node -e "a[0];new X"',
    'node -e "const t=require(\'fs\').readFileSync(\'index.html\',\'utf8\');'
    'const s=t.split(\'<script>\').pop().split(\'</script>\')[0];new Function(s)"',
    'python -c "import sys;sys.path.insert(0,\'scripts\');s={1:2};assert s[1]==2"',
    "gh api repos/owner/repo/actions/runs --jq '.workflow_runs[] | .status'",
    "while true; do gh api repos/owner/repo/actions/runs --jq '.workflow_runs[] | .status'; sleep 5; done",
    "D=$(pwd) && true",
    "d=$(mktemp -d) && echo $d",
    'd=$PWD; (cd /tmp && echo "$d")',
    'curl -fsS https://app.test/api/health >/dev/null && B=$(curl -fsS https://app.test/api/version) '
    '&& [ "$(gh api repos/owner/repo/commits/$B -q .commit.tree.sha)" = '
    '"$(gh api repos/owner/repo/commits/main -q .commit.tree.sha)" ]',
])
def test_quoted_program_and_filter_arguments_are_data(command):
    assert not shell_safety.unsafe_manual_push(command)
    plan = PLAN_ONE.replace('checks: python3 -c "import calc; assert calc.add(2, 3) == 5"',
                            'checks: ' + command)
    assert not [e for e in planfile.parse(plan).errors if "unsafe manual git push" in e]


@pytest.mark.parametrize("text", [
    "Keep the option off by default. Invalidate only the owned blocks' content, then read the source before each change.",
    "Exclude locked rows' counts, then inspect the git source.",
    "The users' source selection uses `git status` for diagnostics.",
    'The users\' "source" setting is retained.',
    "Provides a -> [b].",
    "Provides a; [b].",
    "the deployed asset hash matches git; the source is unchanged",
    "Existing authorization is unchanged: `getCheckinPrintStatusAction` still calls "
    '`assertKioskOrAdmin({ definitionName, surface: "event" })`, and the printer still comes from the signed pairing cookie.',
    "First step: `git merge --ff-only <sha>` (...); the submitted HEAD has <sha> as an ancestor",
])
def test_plural_possessives_are_prose(text):
    assert not shell_safety.unsafe_manual_push(text, prose=True)


@pytest.mark.parametrize("command", [
    'node -e "a[0]; b"; git push origin main',
    'python -c "s={1:2};print(s)" && git push origin main',
    'node -e "console.log(\'$(git push origin main)\')"',
    'printf "%s" "$(git push origin main)"',
    'printf "%s" "$(echo $(git push origin main))"',
    'printf "%s" "`git push origin main`"',
    "eval 'git push origin main'",
    "eval 'gi'\"t push origin main\"",
    "bash -c 'git push origin main'",
    'eval "$COMMAND origin HEAD:main"',
    "env -u $P origin HEAD:main",
    "timeout $D origin HEAD:main",
    "D=$(git push origin main) && true",
    "D=$(pwd) && git push origin main",
    "echo ready\n timeout $D origin HEAD:main",
    "D=5\n timeout $D origin HEAD:main",
])
def test_real_shell_execution_inside_or_after_arguments_is_still_denied(command):
    assert shell_safety.unsafe_manual_push(command)


def test_possessive_does_not_hide_push():
    assert shell_safety.unsafe_manual_push("After updating the blocks' source, run `git push origin main`.", prose=True)


def test_refusal_identifies_the_bad_command():
    plan = PLAN_ONE.replace('checks: python3 -c "import calc; assert calc.add(2, 3) == 5"',
                            'checks: git push origin HEAD:main')
    assert any("git push origin HEAD:main" in e for e in planfile.parse(plan).errors)


@pytest.mark.parametrize("replacement", ["", "done:\n"])
def test_requirements_need_a_done_list(replacement):
    plan = PLAN_ONE.replace("done:\n- add() returns the sum\n", replacement)
    assert any("missing non-empty `done:`" in e for e in planfile.parse(plan).errors)


def test_plan_correction_is_not_reported_as_an_impossible_resume():
    from office import bughint
    assert bughint.refusal_signal("plan-invalid", "submit", ["submit"], "office submit") is None
