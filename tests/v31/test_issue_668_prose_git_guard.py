"""#668: Type notation and normal prose never masquerade as a manual git push."""
import pytest

from conftest import PLAN_ONE
from office import planfile, shell_safety


@pytest.mark.parametrize("text", [
    "room?: {year:number; room:KidsRoom} | null",
    "encodeRoomValue({year, room})",
    "Map<personId, KidsRoom>",
    "FavorKidsRoom<CODE>",
    "familySheet && askKidsRoom",
    "format YYYY|Room",
    "The definition's \"display\" is **bold**, legitimately reviewed.",
    "Use the type Map<personId, ...> for the selected room.",
    "Quoted prose like \"users' preferred category\" is valid.",
])
def test_plain_prose_does_not_trigger_manual_push_guard(text):
    assert not shell_safety.unsafe_manual_push(text, prose=True)
    assert not planfile._unsafe_force_push_instruction(text)


@pytest.mark.parametrize("text", [
    "git push origin main",
    "Please run git push on a shared branch",
    "Use git publish-alias for the deployment",
    "env CI=1 git push origin main",
    "timeout $D origin HEAD:main",
    "echo $(git push)",
    "git push origin 'unclosed",
    "Never mind, run git push origin main",
    "Never run git push, but also run git push origin main",
    "Do not run git push. Then run git push origin main",
    "Never run git push and then git push origin main",
])
def test_actual_or_unverifiable_manual_push_still_denied(text):
    assert shell_safety.unsafe_manual_push(text, prose=True)


@pytest.mark.parametrize("text", [
    "Never run git push to a shared branch",
    "Do not run git push or git push --force",
    "Never run git push, but run git fetch instead",
])
def test_purely_negated_push_prose_allowed(text):
    assert not shell_safety.unsafe_manual_push(text, prose=True)


def test_full_plan_allows_types_in_interfaces_accept_and_done():
    plan = PLAN_ONE.replace(
        "- add() returns the sum",
        "- The definition's \"type\" is Map<personId, KidsRoom> and **bold**"
    ).replace(
        "accept:\n- calc.add(2, 3) == 5",
        "interfaces:\n- room?: {year:number; room:KidsRoom} | null\n"
        "accept:\n- encodeRoomValue({year, room}) works"
    )
    assert not [e for e in planfile.parse(plan).errors if "unsafe manual git push" in e]


def test_dangerous_plan_shows_offending_instruction_and_line():
    plan = PLAN_ONE.replace(
        "- calc.add(2, 3) == 5", "- Please run git push origin main"
    )
    errs = [e for e in planfile.parse(plan).errors if "unsafe manual git push" in e]
    assert len(errs) == 1
    assert "git push origin main" in errs[0]
    assert "line " in errs[0]


@pytest.mark.parametrize("field", ["checks", "visual", "deploy"])
def test_executable_fields_stay_strict(field):
    plan = PLAN_ONE
    if field == "checks":
        plan = plan.replace('checks: python3 -c "import calc; assert calc.add(2, 3) == 5"',
                            'checks: echo $(git push)')
    elif field == "visual":
        plan = plan.replace("visual: none",
                            "visual:\n  url: http://localhost:3000\n  start: echo $(git push)")
    else:
        plan = plan.replace("blast_radius: repo", "blast_radius: repo\ndeploy_prod: echo $(git push)")
    assert any("unsafe manual git push" in e for e in planfile.parse(plan).errors)

def test_line_number_points_to_offending_requirements_line():
    plan = PLAN_ONE.replace("- add() returns the sum", "- git push origin main")
    errors = [e for e in planfile.parse(plan).errors if "unsafe manual git push" in e]
    assert len(errors) == 1
    assert "line 5" in errors[0]
    assert "git push origin main" in errors[0]
