"""A named executor route has user authority only with the user's actual words."""
import json

import pytest

from test_adaptive_dispatch import FB1, _approve, _quota


def _latest(env):
    return dict(env.con().execute(
        "SELECT * FROM dispatches WHERE task_id='T1' ORDER BY started_at DESC LIMIT 1").fetchone())


def _ended(env):
    con = env.con()
    con.execute("UPDATE dispatches SET status='ended', ended_at=started_at WHERE task_id='T1'")
    con.execute("UPDATE tasks SET status='changes_required' WHERE id='T1'")
    con.commit()


def _events(env):
    return [json.loads(r[0]) for r in env.con().execute(
        "SELECT payload_json FROM events WHERE kind='route.changed' ORDER BY seq")]


@pytest.mark.parametrize("command", ["dispatch", "rerun"])
def test_unquoted_as_records_orchestrator_authority_and_can_be_rerouted(env, command):
    _approve(env)
    if command == "rerun":
        env.office("dispatch", "T1", env=_quota(env), check=0)
        _ended(env)
    args = [command, "T1", *(["--fresh"] if command == "rerun" else []), "--as", FB1]
    code, out = env.office(*args, env=_quota(env))
    assert code == 0 and "user override" not in out, out
    d = _latest(env)
    route = json.loads(d["route_json"])
    assert not route.get("override") and not route.get("declared"), route
    assert route["route_authority"]["actor"] == "orchestrator"
    assert route["route_authority"]["reason"]
    assert _events(env)[-1]["actor"] == "orchestrator"
    assert "user override" not in _events(env)[-1]["reason"]
    # The named route becomes unavailable; an ordinary evidence reroute can replace it.
    _ended(env)
    code, out = env.office("rerun", "T1", "--fresh", "--reroute", env=_quota(env, codex=1))
    assert code == 0 and _latest(env)["triple"] != d["triple"], out
    assert _events(env)[-1]["actor"] == "orchestrator"


@pytest.mark.parametrize("flag", ["--as", "--route"])
def test_unquoted_named_route_cannot_spend_protected_quota(env, flag):
    _approve(env)
    code, out = env.office("dispatch", "T1", flag, FB1, env=_quota(env, codex=1))
    assert code == 4 and "quota" in out.lower(), out
    assert env.con().execute("SELECT COUNT(*) FROM dispatches WHERE task_id='T1'").fetchone()[0] == 0


@pytest.mark.parametrize("command", ["dispatch", "rerun"])
def test_quoted_route_keeps_verbatim_authority_through_rerun(env, command):
    _approve(env)
    if command == "rerun":
        env.office("dispatch", "T1", env=_quota(env), check=0)
        _ended(env)
    quote = "Use Astra for this task; I authorize that route."
    args = [command, "T1", *(["--fresh"] if command == "rerun" else []),
            "--as", FB1, "--quote", quote]
    code, out = env.office(*args, env=_quota(env))
    assert code == 0 and "user override" in out, out
    d = _latest(env)
    route = json.loads(d["route_json"])
    assert route["override"] and route["declared"]
    assert route["route_authority"]["actor"] == "user" and route["route_authority"]["quote"] == quote
    assert json.loads(d["override_json"])["quote"] == quote
    assert _events(env)[-1]["actor"] == "user" and _events(env)[-1]["quote"] == quote
    _ended(env)
    env.office("rerun", "T1", "--fresh", env=_quota(env), check=0)
    inherited = json.loads(_latest(env)["route_json"])
    assert inherited["route_authority"] == route["route_authority"]


def test_unquoted_as_does_not_bypass_registry_or_trust(env):
    _approve(env)
    code, out = env.office("dispatch", "T1", "--as", "claude/some-future-model@low", env=_quota(env))
    assert code == 4 and "no qualifying" in out, out
    assert env.con().execute("SELECT COUNT(*) FROM dispatches WHERE task_id='T1'").fetchone()[0] == 0


def test_unquoted_known_route_still_requires_current_trust(env):
    _approve(env)
    con = env.con()
    con.execute("DELETE FROM adapter_trust_acts WHERE triple LIKE 'codex@%'")
    con.commit()
    code, out = env.office("dispatch", "T1", "--as", FB1, env=_quota(env))
    assert code == 4 and "trust" in out.lower(), out
    assert con.execute("SELECT COUNT(*) FROM dispatches WHERE task_id='T1'").fetchone()[0] == 0


@pytest.mark.parametrize("flags", [["--as", FB1, "--quote", "  "], ["--quote", "Use Astra"]])
def test_empty_or_unattached_quote_is_rejected(env, flags):
    _approve(env)
    code, out = env.office("dispatch", "T1", *flags, env=_quota(env))
    assert code == 2 and "quote" in out.lower(), out
    assert env.con().execute("SELECT COUNT(*) FROM dispatches WHERE task_id='T1'").fetchone()[0] == 0
