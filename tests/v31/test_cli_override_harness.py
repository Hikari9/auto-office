"""#732: a --cli override records the harness its argv actually launches."""
from __future__ import annotations

import json

import pytest

from conftest import approved_run

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}
PI_CLI = "pi --model anthropic/claude-sonnet-5-5 --thinking high"
QUOTE = "Run Sonnet on pi"


def _row(env, tid="T1"):
    con = env.con()
    did = con.execute("SELECT current_dispatch_id FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    return dict(con.execute("SELECT * FROM dispatches WHERE id=?", (did,)).fetchone())


def _dispatch(env, cli):
    approved_run(env)
    return env.office("dispatch", "T1", "--as", "claude/claude-sonnet-5-5@high", "--quote", QUOTE,
                      "--cli", cli, env=EXTERNAL, check=0)


@pytest.mark.approved
def test_pi_cli_records_pi_and_inspect_says_pi_exposes_none(env):
    _dispatch(env, PI_CLI)
    d = _row(env)
    assert d["harness"] == "pi" and d["adapter_id"] == "pi"
    code, out = env.office("inspect", "task", "T1")
    assert "session unavailable: pi exposes none" in out and "claude exposes none" not in out, out
    assert f"--cli {PI_CLI}" in out


@pytest.mark.approved
def test_cli_override_keeps_route_triple_quote_and_cli(env):
    _dispatch(env, PI_CLI)
    d = _row(env)
    ov = json.loads(d["override_json"])
    assert ov["by"] == "user" and ov["quote"] == QUOTE and ov["cli"] == PI_CLI
    assert ov["triple"] == d["triple"] and d["triple"].endswith("/claude-sonnet-5-5@high")
    route = json.loads(d["route_json"])
    assert route["launch"]["cli"] == PI_CLI


@pytest.mark.approved
def test_cli_matching_no_known_harness_keeps_the_routed_harness(env):
    _dispatch(env, "my-wrapper --model x")
    d = _row(env)
    assert d["harness"] == "claude"
    assert json.loads(d["override_json"])["cli"] == "my-wrapper --model x"


@pytest.mark.approved
def test_cli_path_and_unparseable_argv_resolve_safely(env):
    from office import dispatch
    assert dispatch.cli_harness("/opt/bin/pi --model x") == "pi"
    assert dispatch.cli_harness("claude --model x") == "claude"
    assert dispatch.cli_harness("pi 'unterminated") is None
    assert dispatch.cli_harness("") is None and dispatch.cli_harness(None) is None
    assert dispatch.cli_harness("my-wrapper") is None


@pytest.mark.approved
def test_learning_evidence_is_attributed_to_the_executing_harness(env):
    _dispatch(env, PI_CLI)
    d = _row(env)
    con = env.con()
    with con:
        con.execute("UPDATE dispatches SET ended_at='2030-01-01T00:00:00Z', status='ended' WHERE id=?", (d["id"],))
        con.execute("UPDATE runs SET phase='closed'")
    from office import route_learning
    outcomes = [o for o in route_learning.derive_outcomes(con) if o["dispatch_id"] == d["id"]]
    assert outcomes and outcomes[0]["route"] == "pi/claude-sonnet-5-5@high", outcomes


@pytest.mark.approved
def test_inspect_reads_a_pre_fix_row_by_its_cli_executable(env):
    _dispatch(env, PI_CLI)
    con = env.con()
    with con:
        con.execute("UPDATE dispatches SET harness='claude'")
    code, out = env.office("inspect", "task", "T1")
    assert "session unavailable: pi exposes none" in out, out
