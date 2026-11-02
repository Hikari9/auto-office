"""#1031 (Rico, 2026-11-02): an executor may use its harness's subagents for the task itself. One neutral
line: allowed at its discretion, never pushed or discouraged; it keeps responsibility for scope, checks and
submit. Self-review subagents stay a separate instruction."""
from office import briefs

PACKET = {"task_id": "T1", "title": "x", "scope": ["a.py"], "plan_version": 1, "requirements_version": 1}


def test_the_executor_brief_allows_subagents_for_the_task_neutrally():
    for scope in (["a.py"], []):
        brief = briefs.executor_brief(None, {}, {**PACKET, "scope": scope})
        assert brief.count(briefs.SUBAGENTS_LINE) == 1, brief
    line = briefs.SUBAGENTS_LINE.lower()
    assert "if you judge that useful" in line and "responsible for scope, the checks and office submit" in line
    assert not any(w in line for w in ("must", "should", "avoid", "prefer", "encouraged", "do not"))
