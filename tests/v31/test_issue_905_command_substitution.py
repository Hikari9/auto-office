"""#905: command substitution that publishes nothing is accepted; hidden pushes stay refused."""
import pytest
from office import planfile, shell_safety
from conftest import PLAN_ONE

CHECKS_LINE = 'checks: python3 -c "import calc; assert calc.add(2, 3) == 5"'


@pytest.mark.parametrize("command", [
    'FPR_TEST_NODE_BIN="$(which node)" pnpm relay:package:mac',
    'FPR_TEST_NODE_BIN="$(command -v node)" pnpm relay:package:mac',
    "NODE_BIN=$(which node) pnpm build",
    "NODE_BIN=`which node` pnpm build",
    "env NODE_BIN=\"$(which node)\" pnpm build",
    "pnpm test --bin=$(which node)",
    "echo $(date)",
    'FOO="$((1 + 2))" pnpm build',
    'FOO="$(printf \'(\')" pnpm build',
    'FOO="$(echo $(which node))" pnpm build',
    "echo '$(git status)'",
    # Escaped backticks are literal text: echo prints them and runs nothing.
    "echo \\`git push\\`",
])
def test_substitution_without_push_is_permitted(command):
    assert not shell_safety.unsafe_manual_push(command)


@pytest.mark.parametrize("command", [
    "$(git push origin main)",
    'FOO="$(git push origin HEAD:main)" pnpm build',
    "FOO=`git push` pnpm build",
    'pnpm test --rev="$(git -C repo push)"',
    "pnpm test --rev=`git push`",
    "echo $(echo \"$(git push)\")",
    "echo $(echo $(git push))",
    "echo $(echo `git push`)",
    "$(echo git) push origin main",
    '"$(which git)" push origin main',
    "git $(echo push) origin main",
    'git "$(echo push)" origin main',
    'env X="$(which node)" git push',
    "FOO=\"$(echo ')'; git push)\" pnpm build",
    'FOO="$(echo x" pnpm build',
    "echo $(date",
    # Unbalanced quote: the line cannot be tokenized, so it cannot be verified.
    'echo "; g\\it p\\ush o\\rigin m\\ain',
    # Newline ends a command: the dynamic word starts a new one.
    "true\n$(echo g)it $(echo pu)sh origin main",
    # Assignment and reserved-word prefixes do not hide a dynamic command head.
    "x=1 $(echo g)it $(echo pu)sh origin main",
    "! $(echo g)it $(echo pu)sh origin main",
    # Backslash-escaped backtick is literal; the split words still run git push.
    "echo \\` ; g\\it p\\ush o\\rigin m\\ain ; echo \\`",
    # A shell that reads stdin runs input this guard cannot see.
    "printf '\\x67it\\x20push\\x20origin\\x20main' | sh",
    "cat script.sh | bash",
    # zsh expands =cmd to a command path.
    "=git $(echo pu)sh origin main",
])
def test_hidden_push_or_unverifiable_substitution_is_refused(command):
    assert shell_safety.unsafe_manual_push(command)


def test_substitution_with_push_is_refused_in_prose():
    assert shell_safety.unsafe_manual_push("Run $(git push origin main) when ready", prose=True)


def test_unclosed_substitution_in_prose_is_text():
    assert not shell_safety.unsafe_manual_push("Replace $( with the name", prose=True)


def test_plan_accepts_checks_line_with_command_substitution():
    plan = PLAN_ONE.replace(CHECKS_LINE,
                            'checks: FPR_TEST_NODE_BIN="$(which node)" pnpm relay:package:mac')
    parsed = planfile.parse(plan)
    assert not any("unsafe manual git push" in e for e in parsed.errors)
    assert parsed.tasks[0]["checks"] == ['FPR_TEST_NODE_BIN="$(which node)" pnpm relay:package:mac']


def test_plan_refusal_names_the_line():
    line = 'checks: FOO="$(git push origin main)" pnpm build'
    errors = [e for e in planfile.parse(PLAN_ONE.replace(CHECKS_LINE, line)).errors
              if "unsafe manual git push" in e]
    assert len(errors) == 1
    assert errors[0].startswith("T1: ")
    assert "FOO=" in errors[0] and "git push origin main" in errors[0]
    assert "cannot verify" in errors[0]


def test_run_level_requirement_refusal_names_the_line():
    plan = PLAN_ONE.replace("blast_radius: repo",
                            'blast_radius: repo\nchecks: FOO="$(git push origin main)" pnpm build')
    errors = [e for e in planfile.parse(plan).errors if "run-level Requirements" in e]
    assert len(errors) == 1 and "git push origin main" in errors[0]


def test_run_level_requirement_substitution_without_push_is_permitted():
    plan = PLAN_ONE.replace("blast_radius: repo",
                            'blast_radius: repo\nchecks: FOO="$(which node)" pnpm build')
    assert not any("run-level Requirements" in e for e in planfile.parse(plan).errors)
