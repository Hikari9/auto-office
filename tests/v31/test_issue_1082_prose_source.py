"""#1082: Prose mentioning 'source' with or without unbalanced quotes is not an unsafe manual push."""
import pytest

from conftest import PLAN_ONE
from office import planfile, shell_safety

# Verbatim accept text from the issue. It passes the guard on HEAD too; it is a
# regression guard for the same plan path, not a reproducer.
ISSUE_REPRO = ("[M] offers to write the twin source. T3 does not write the plugin controller "
               "source: run [M] writes it from the spec.")

SOURCE_PROSE = [
    "writes the source file",
    "writes the \"source file",
    "writes the source file\"",
    "writes the 'source file",
    "writes the source file'",
    "writes the source file; and the same sentence with an unbalanced quote mark",
    "writes the source file; and the same sentence with an unbalanced quote mark'",
    "writes the \"source file; and the same sentence with an unbalanced quote mark",
    "writes the source file for $VAR",
    "The definition's \"source\" is legitimate.",
    "push the source changes to the PR branch",
    "write the source push result to a file",
    ISSUE_REPRO,
]


@pytest.mark.parametrize("prose", SOURCE_PROSE)
def test_prose_with_source_does_not_trigger_manual_push_guard(prose):
    assert not shell_safety.unsafe_manual_push(prose, prose=True)
    assert not planfile._unsafe_force_push_instruction(prose)


@pytest.mark.parametrize("prose", SOURCE_PROSE)
def test_plan_with_source_prose_passes_plan_guard(prose):
    plan = PLAN_ONE.replace("- calc.add(2, 3) == 5", f"- {prose}")
    parsed = planfile.parse(plan)
    assert not any("unsafe manual git push" in e for e in parsed.errors)
    assert not parsed.errors


@pytest.mark.parametrize("prose", SOURCE_PROSE)
def test_amend_guard_allows_source_prose(prose):
    edited = planfile.edit_task(PLAN_ONE, "T1", add_accept=[prose])
    parsed = planfile.parse(edited)
    assert not any("unsafe manual git push" in e for e in parsed.errors)
    assert not parsed.errors


def test_real_manual_push_and_dynamic_commands_remain_denied():
    assert shell_safety.unsafe_manual_push("git push origin 'unclosed", prose=True)
    assert shell_safety.unsafe_manual_push("source $FOO")  # shell field strict scan
    assert shell_safety.unsafe_manual_push("eval $FOO")
    assert shell_safety.unsafe_manual_push("git push $BRANCH", prose=True)
    # Shell commands still treat source as a runner, so a sourced push stays refused.
    assert shell_safety.unsafe_manual_push("source ./x.sh; push")
    assert shell_safety.unsafe_manual_push("source ./env.sh && git push origin main", prose=True)
    # Prose that runs a sourced file with a path or expansion operand is still refused.
    assert shell_safety.unsafe_manual_push("source ./release.sh to push the tag", prose=True)
    assert shell_safety.unsafe_manual_push("run source ./scripts/release.sh and push the tag", prose=True)
    assert shell_safety.unsafe_manual_push("source $FOO", prose=True)
    assert shell_safety.unsafe_manual_push("source ./publish.sh 'push the tag", prose=True)
    assert shell_safety.unsafe_manual_push("source ./publish.sh 'push the tag", prose=True)


# F4: a sourced script named by a bare, relative or quoted path runs the file in
# prose too. Each must be refused by the plan and amend guards.
SOURCED_PUSH_PROSE = [
    "source release.sh to push the tag",
    "run source scripts/release.sh and push the tag",
    "source env.sh then push",
    "source \"release.sh to push the tag",
    "source 'scripts/release.sh' and push the tag",
    "run source \"release.sh\" to push the tag",
]


@pytest.mark.parametrize("prose", SOURCED_PUSH_PROSE)
def test_sourced_script_push_prose_is_refused(prose):
    assert shell_safety.unsafe_manual_push(prose, prose=True)


@pytest.mark.parametrize("prose", SOURCED_PUSH_PROSE)
def test_plan_guard_refuses_sourced_script_push_prose(prose):
    plan = PLAN_ONE.replace("- calc.add(2, 3) == 5", f"- {prose}")
    parsed = planfile.parse(plan)
    assert any("unsafe manual git push" in e for e in parsed.errors)


@pytest.mark.parametrize("prose", SOURCED_PUSH_PROSE)
def test_amend_guard_refuses_sourced_script_push_prose(prose):
    edited = planfile.edit_task(PLAN_ONE, "T1", add_accept=[prose])
    parsed = planfile.parse(edited)
    assert any("unsafe manual git push" in e for e in parsed.errors)


@pytest.mark.parametrize("prose", [
    "the source and/or target push result",
    "writes the source file and pushes nothing",
    "writes the source docs/plan.md for review",
    "writes the source \"release notes\" file",
])
def test_non_script_operands_after_source_stay_allowed(prose):
    assert not shell_safety.unsafe_manual_push(prose, prose=True)
    assert not planfile._unsafe_force_push_instruction(prose)
