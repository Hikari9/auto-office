"""Regression: Requirements bullet continuations must survive office submit (#644)."""
from office import planfile


def test_wrapped_done_and_non_goals_are_preserved():
    text = """## Requirements
done:
- Full criterion starts here
  and finishes on another line.
non_goals:
- Not doing the first thing,
  nor this second thing.
"""
    result = planfile.parse(text)
    assert result.requirements["done_criteria"] == [
        "Full criterion starts here and finishes on another line."
    ]
    assert result.requirements["non_goals"] == [
        "Not doing the first thing, nor this second thing."
    ]


def test_indented_real_requirement_keys_are_not_swallowed():
    text = """## Requirements
done:
- Keep this criterion.
  blast_radius: low
"""
    result = planfile.parse(text)
    assert result.requirements["done_criteria"] == ["Keep this criterion."]
    assert result.requirements["blast_radius"] == "low"


def test_wrapped_criterion_containing_colon_is_preserved():
    text = """## Requirements
done:
- Criterion starts here.
  For example: check the member status.
- Second criterion.
"""
    result = planfile.parse(text)
    assert result.requirements["done_criteria"] == [
        "Criterion starts here. For example: check the member status.",
        "Second criterion.",
    ]


def test_indented_non_goals_alias_stays_separate_from_done_item():
    text = """## Requirements
done:
- Keep this criterion.
  non-goals: do not touch production
"""
    result = planfile.parse(text)
    assert result.requirements["done_criteria"] == ["Keep this criterion."]
    assert result.requirements["non_goals"] == "do not touch production"


def test_every_accepted_requirement_key_variant_stays_separate_when_indented():
    for key in sorted(planfile.REQUIREMENT_KEYS):
        spellings = {key, key.replace("_", "-"), key.replace("_", " "), key.upper(), key.replace("_", "-").title()}
        for spelling in spellings:
            text = f"## Requirements\ndone:\n- Keep this criterion.\n  {spelling}: value\n"
            result = planfile.parse(text)
            criteria = result.requirements["done_criteria"]
            # `done:` itself restarts the field; every other key must leave the item untouched.
            assert criteria == ([] if key == "done" else ["Keep this criterion."]), spelling
            assert not any("value" in c for c in criteria), spelling
    # every key the parser maps into a requirements field is in the table
    assert planfile.REQUIREMENT_KEYS == {
        "goal", "done", "non_goals", "actions", "checks", "blast_radius", "irreversible", "size_class",
        "lightweight", "end_state", "deploy_preview", "deploy_prod", "deploy_verify"}
