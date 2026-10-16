"""#653: Markdown inline code in plan prose is not shell substitution."""
import pytest

from conftest import PLAN_ONE
from office import planfile, shell_safety


@pytest.mark.parametrize("instruction", [
    "`package.json` gains `deploy:wait-prod`; no other script changes.",
    "The `src/office/main.py` path must be readable.",
    "A `git status` check should show a clean tree.",
    "Use ``inline code`` in the documentation.",
])
def test_markdown_code_in_prose_does_not_block_amendments(instruction):
    assert not shell_safety.unsafe_manual_push(instruction, prose=True)
    assert not planfile._unsafe_force_push_instruction(instruction)


@pytest.mark.parametrize("instruction", [
    "Please run `git push` on the shared main branch",
    "Run `git publish-alias` to upload the branch",
    "git push origin main",
    "echo $(git push)",
])
def test_actual_manual_push_in_prose_remains_blocked(instruction):
    assert shell_safety.unsafe_manual_push(instruction, prose=True)


def test_full_plan_accepts_literal_inline_code_in_requirement_and_task():
    plan = PLAN_ONE.replace(
        "- add() returns the sum",
        "- `package.json` gains `deploy:wait-prod`; no other script changes."
    ).replace(
        "- calc.add(2, 3) == 5",
        "- `calc.add(2, 3)` still returns 5."
    )
    assert not [e for e in planfile.parse(plan).errors if "unsafe manual git push" in e]


@pytest.mark.parametrize("command_field", ["checks", "visual"])
def test_actual_shell_command_fields_keep_strict_substitution_guard(command_field):
    command = "echo `git push`"
    if command_field == "checks":
        plan = PLAN_ONE.replace('checks: python3 -c "import calc; assert calc.add(2, 3) == 5"', "checks: " + command)
    else:
        plan = PLAN_ONE.replace("visual: none", "visual:\n  url: http://127.0.0.1:3000\n  start: " + command)
    assert any("unsafe manual git push" in e for e in planfile.parse(plan).errors)
