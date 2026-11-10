"""#1082: Prose mentioning 'source' with or without unbalanced quotes is not an unsafe manual push."""
import pytest

from conftest import PLAN_ONE
from office import planfile
from office import shell_safety

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
    "source \"my dir/release.sh\" and push the tag",
    "Source release.sh to push the tag",
    "Run source release.sh. Then push the tag",
    "source release.sh and pushes the tag",
    "source release.sh to Push the tag",
    "source release.sh to push the tag\"",
    # F7: an extensionless command-position operand is a PATH script too.
    "source publish to push the tag",
    "Source publish to push the tag",
    "run source publish and push the tag",
    "source publish to push the tag'",
    "source \"publish to push the tag",
    "'source publish to push the tag",
    "\"source publish to push the tag",
    "Run source publish, then push the tag.",
    "source publish to push, then build",
    "first source publish and push the tag",
    "please source publish to push the tag",
    "just source publish to push the tag",
    "execute source publish to push the tag",
    "then source publish to push the tag",
    "Done; source publish to push the tag",
    # F10: punctuation after a command lead ("Run:") still puts the next word in command position.
    "Run: source publish to push the tag",
    "Run, source publish and push the tag",
    "first: source publish to push the tag",
    "Run: source publish to push the tag'",
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
    "The funding source $500 covers the retreat.",
    "Track the source $ amount for the report.",
    "source / sink then push",
    "run source release.sh to build the tag",
    "run source release.sh to build the tag\"",
    "source \"release.sh to build the tag",
    "writes the source \"release notes.md\" file and pushes nothing",
    # F7 negatives: a bare operand is a script only in command position and only with a push.
    "writes the source publish file and pushes nothing",
    "source publish to build the tag",
    "source publish to build the tag'",
    "Source revenue $500 covers the retreat.",
    "run source of truth notes for the retreat",
    # Inflected or negated push words are not the push token after a bare-word run.
    "Source tree must not be pushed.",
    "Source files are never pushed to the remote.",
    "The docs and source code are pushed by CI.",
    "Keep the docs and source code unpushed.",
    "First source tree is pushed to origin by CI.",
    # F10 negative: punctuation after a lead does not make a bare operand a push.
    "Run: source publish to build the tag",
])
def test_non_script_operands_after_source_stay_allowed(prose):
    assert not shell_safety.unsafe_manual_push(prose, prose=True)
    assert not planfile._unsafe_force_push_instruction(prose)


# Self-review: a sourced script's check command may name a word that merely contains push.
# A push form or push flag still refuses, so only the containing word is allowed.
@pytest.mark.parametrize("command", [
    "source ./env.sh && pytest tests/test_pusher.py",
    "source ./env.sh && pytest tests/test_pushover.py",
])
def test_sourced_check_naming_pusher_word_stays_allowed(command):
    assert not shell_safety.unsafe_manual_push(command)
    assert not shell_safety.unsafe_manual_push(command, prose=True)


def test_sourced_script_with_push_flag_or_form_stays_refused():
    assert shell_safety.unsafe_manual_push("source ./env.sh && ./release --push")
    assert shell_safety.unsafe_manual_push("source ./env.sh && ./release.sh pushes")


def test_lead_and_source_noun_with_push_is_refused_by_design():
    """Fail-closed by design (self-review medium; owner answer: keep the refusal).

    `and` and `first` are command leads, so a `source` after them is read as a script
    run. A noun there plus a separate `push` token is refused too. That false positive
    costs a rephrase, and the refusal names the line. Dropping the leads would let
    "Build the app and source publish to push the tag" through, so the refusal is pinned.
    """
    assert shell_safety.unsafe_manual_push("Update the docs and source code, then push the branch.", prose=True)
    assert planfile._unsafe_force_push_instruction("Update the docs and source code, then push the branch.")
    assert shell_safety.unsafe_manual_push("Build the app and source publish to push the tag", prose=True)


# Self-review lows: markdown emphasis around a lead word, and a quoted script operand with a space
# after a non-lead word in the unbalanced-quote fallback.
LOW_BYPASS_PROSE = [
    "**Run:** source publish to push the tag",
    "**Run**: source publish to push the tag",
    "_Run_: source publish to push the tag",
    "Done. source \"my dir/release.sh and push the tag",
]
@pytest.mark.parametrize("prose", LOW_BYPASS_PROSE)
def test_emphasis_and_quoted_operand_prose_is_refused(prose):
    assert shell_safety.unsafe_manual_push(prose, prose=True)
    assert planfile._unsafe_force_push_instruction(prose)
