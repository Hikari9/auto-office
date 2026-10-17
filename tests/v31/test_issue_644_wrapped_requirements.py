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
