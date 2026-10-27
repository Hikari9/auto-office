"""A quoted user override sets an effort ceiling; an orchestrator raise needs new user words (#817)."""
import json

import pytest

from test_adaptive_dispatch import _approve, _quota
from test_issue_707_route_authority import _ended, _events, _latest

LOW, MED, HIGH = ("codex/gpt-6-astra@" + e for e in ("low", "medium", "high"))
QUOTE = "force it on astra, use the lowest qualifying astra for the task"


def _pin(env, route=LOW, quote=QUOTE):
    _approve(env)
    code, out = env.office("dispatch", "T1", "--as", route, "--quote", quote, env=_quota(env))
    assert code == 0 and "user override" in out, out
    _ended(env)


def _ceiling(env):
    return json.loads(env.con().execute("SELECT route_json FROM tasks WHERE id='T1'").fetchone()[0])["override_ceiling"]


@pytest.mark.parametrize("quote", [QUOTE, f"  {QUOTE} ", None])
def test_reused_or_missing_quote_cannot_raise_effort(env, quote):
    _pin(env)
    before = env.con().execute("SELECT COUNT(*) FROM dispatches WHERE task_id='T1'").fetchone()[0]
    args = ["rerun", "T1", "--fresh", "--as", HIGH, *(["--quote", quote] if quote else [])]
    code, out = env.office(*args, env=_quota(env))
    assert code != 0 and "effort-above-user-quote" in out, out
    assert "ask the user for new words" in out and "stay at or below @low" in out, out
    assert env.con().execute("SELECT COUNT(*) FROM dispatches WHERE task_id='T1'").fetchone()[0] == before
    assert "high" not in [e["after"] for e in _events(env)][-1:][0]


def test_new_quote_may_raise_effort_and_is_attributed_to_the_user(env):
    _pin(env)
    new = "ok, go to high on astra"
    code, out = env.office("rerun", "T1", "--fresh", "--as", HIGH, "--quote", new, env=_quota(env))
    assert code == 0 and "user override" in out, out
    event = _events(env)[-1]
    assert event["actor"] == "user" and event["quote"] == new and event["kind"] == "override"
    assert _ceiling(env) == {"model": "gpt-6-astra", "effort": "high", "quote": new}


def test_unquoted_change_is_the_orchestrators_and_ceiling_survives(env):
    _pin(env, route=MED)
    code, out = env.office("rerun", "T1", "--fresh", "--as", LOW, env=_quota(env))
    assert code == 0 and "user override" not in out, out
    event = _events(env)[-1]
    assert event["actor"] == "orchestrator" and event["kind"] == "reroute", event
    assert QUOTE not in event["reason"] and "quote" not in event
    assert _ceiling(env)["effort"] == "medium"
    _ended(env)
    code, out = env.office("rerun", "T1", "--fresh", "--as", HIGH, "--quote", QUOTE, env=_quota(env))
    assert code != 0 and "effort-above-user-quote" in out, out


def test_lowering_or_staying_at_the_quoted_effort_is_allowed(env):
    _pin(env, route=MED)
    code, out = env.office("rerun", "T1", "--fresh", "--as", LOW, "--quote", QUOTE, env=_quota(env))
    assert code == 0, out
    _ended(env)
    # Returning to the quoted effort is within the ceiling.
    code, out = env.office("rerun", "T1", "--fresh", "--as", MED, "--quote", QUOTE, env=_quota(env))
    assert code == 0 and _events(env)[-1]["actor"] == "user", out
    assert _ceiling(env)["effort"] == "medium"


def test_amend_route_with_the_same_quote_cannot_raise_effort_either(env):
    _pin(env)
    code, out = env.office("amend", "route", "T1", "--as", HIGH, "--quote", QUOTE, env=_quota(env))
    assert code != 0 and "effort-above-user-quote" in out and "ask the user for new words" in out, out
    code, out = env.office("amend", "route", "T1", "--as", HIGH, "--quote", "new words: high is fine", env=_quota(env))
    assert code == 0, out


def test_evidence_reroute_keeps_the_ceiling(env):
    _pin(env)
    code, out = env.office("rerun", "T1", "--fresh", "--reroute", env=_quota(env))
    assert code == 0, out
    assert _ceiling(env)["effort"] == "low"
    _ended(env)
    code, out = env.office("rerun", "T1", "--fresh", "--as", HIGH, "--quote", QUOTE, env=_quota(env))
    assert code != 0 and "effort-above-user-quote" in out, out


def test_omitted_effort_that_resolves_above_the_ceiling_is_refused(monkeypatch):
    from office import candidates, dispatch
    from office.state import Refused
    task = {"id": "T1", "route_json": json.dumps({
        "candidate": {"model_id": "gpt-6-astra", "effort": "low"},
        "override_ceiling": {"model": "gpt-6-astra", "effort": "low", "quote": QUOTE}})}
    monkeypatch.setattr(candidates, "declared_candidate", lambda *a, **k: {"effort": "high"})
    with pytest.raises(Refused) as exc:
        dispatch.enforce_effort_ceiling(task, "codex/gpt-6-astra", QUOTE)
    assert "effort-above-user-quote" in exc.value.category
    monkeypatch.setattr(candidates, "declared_candidate", lambda *a, **k: {"effort": "none"})
    assert dispatch.enforce_effort_ceiling(task, "codex/gpt-6-astra", QUOTE)["effort"] == "low"


def test_reroute_selecting_the_same_model_above_the_ceiling_is_refused(monkeypatch):
    from office import candidates, dispatch, state
    from office.state import Refused
    task = {"id": "T1", "route_json": json.dumps({
        "candidate": {"model_id": "gpt-6-astra", "effort": "low"},
        "override_ceiling": {"model": "gpt-6-astra", "effort": "low", "quote": QUOTE}})}
    monkeypatch.setattr(state, "pinned_config", lambda run: {})
    pick = {"status": "selected", "selected": "codex@1/gpt-6-astra@high",
            "candidate": {"model_id": "gpt-6-astra", "effort": "high"}}
    monkeypatch.setattr(candidates, "route_role", lambda *a, **k: dict(pick))
    with pytest.raises(Refused) as exc:
        dispatch.planned_route(None, {}, task, reroute=True)
    assert exc.value.category == "effort-above-user-quote" and "ask the user for new words" in exc.value.next_step
    pick["candidate"]["effort"] = "low"
    assert dispatch.planned_route(None, {}, task, reroute=True)["route_source"] == "reroute"


def test_reused_quote_cannot_authorize_another_model_above_the_ceiling(env):
    _pin(env)
    code, out = env.office("rerun", "T1", "--fresh", "--as", "claude/claude-opus-5-5@high", "--quote", QUOTE,
                           env=_quota(env))
    assert code != 0 and "effort-above-user-quote" in out, out


def test_model_only_route_resolving_above_the_ceiling_is_refused(env, monkeypatch):
    from office import candidates
    _pin(env)
    real = candidates.route_role

    def high(*a, **k):
        d = real(*a, **k)
        if d.get("status") == "selected":
            d["candidate"] = {**d["candidate"], "effort": "high"}
        return d
    monkeypatch.setattr(candidates, "route_role", high)
    code, out = env.office("dispatch", "T1", "--route", "gpt-6-astra", env=_quota(env))
    assert code != 0 and "effort-above-user-quote" in out, out


def test_reused_quote_keeps_the_original_ceiling_across_a_model_change(env):
    _pin(env, route=MED)
    code, out = env.office("rerun", "T1", "--fresh", "--as", "claude/claude-opus-5-5@low", "--quote", QUOTE,
                           env=_quota(env))
    assert code == 0, out
    assert _ceiling(env) == {"model": "gpt-6-astra", "effort": "medium", "quote": QUOTE}
