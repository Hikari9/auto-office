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


def test_a_harness_without_subagents_gets_an_inline_self_review(monkeypatch):
    """#1074: opencode executors hung starting their self-review subagent (run 553f1ddc). Its adapter says
    `subagents: false`, so the brief says to do the task and the self-review in the executor's own session."""
    from office import adapters
    assert adapters.load_all()["opencode"].get("subagents") is False
    monkeypatch.setattr(briefs.state, "get_dispatch", lambda con, did: {"adapter_id": "opencode", "harness": "opencode"})
    packet = {**PACKET, "dispatch_id": "D1", "base_commit": "abc"}
    assert briefs.harness_subagents(object(), packet) is False
    monkeypatch.setattr(briefs.state, "get_dispatch", lambda con, did: {"adapter_id": "claude", "harness": "claude"})
    assert briefs.harness_subagents(object(), packet) is True
    assert briefs.harness_subagents(None, packet) is True  # no dispatch to look up: unchanged
    monkeypatch.setattr(briefs, "harness_subagents", lambda con, packet: False)
    for run in ({}, {"gates": {"gear_model_version": "run-gear-1"}}):
        brief = briefs.executor_brief(None, run, packet)
        assert briefs.NO_SUBAGENTS_LINE in brief and briefs.SUBAGENTS_LINE not in brief, brief
        assert "Start exactly one subagent" not in brief and "parallel subagents" not in brief, brief
