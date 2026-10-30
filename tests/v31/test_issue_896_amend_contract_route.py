"""#896: `office amend T1 --contract --as ...` silently dropped `--as`.

The chosen behavior is refusal: a route is not part of a contract amendment, so the amendment
is refused before anything is recorded (no amendment row, no plan version, no route change) and
points to `office amend route`, which keeps its own quote and restart semantics.

Agents never run here: the dispatch is launched external, so its row stays live.
"""
from __future__ import annotations

import json

import pytest

from conftest import GOOD_ADD, PLAN_ONE, approved_run

EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}
ROUTE = "claude/sonnet@high"
QUOTE = "switch T1 to sonnet"


@pytest.fixture
def live(request, monkeypatch):
    env = request.getfixturevalue("env")
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": False}] * 3)
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    for k, v in EXTERNAL.items():
        monkeypatch.setenv(k, v)
    return env


def _state(con):
    run_id = con.execute("SELECT run_id FROM tasks WHERE id='T1'").fetchone()[0]
    return {
        "plan_version": con.execute("SELECT plan_version FROM runs WHERE id=?", (run_id,)).fetchone()[0],
        "amendments": con.execute("SELECT COUNT(*) FROM amendments WHERE run_id=?", (run_id,)).fetchone()[0],
        "dispatches": [dict(r) for r in con.execute("SELECT * FROM dispatches WHERE task_id='T1' ORDER BY started_at")],
        "events": [tuple(r) for r in con.execute("SELECT kind, summary FROM events WHERE run_id=? ORDER BY seq", (run_id,))],
        "outbox": [tuple(r) for r in con.execute("SELECT kind FROM outbox WHERE run_id=?", (run_id,))],
        "task_route": json.loads(con.execute("SELECT route_json FROM tasks WHERE id='T1'").fetchone()[0] or "null"),
    }


@pytest.mark.approved
@pytest.mark.parametrize("route_args", [
    ("--as", ROUTE, "--quote", QUOTE),
    ("--as", ROUTE),
])
def test_contract_amendment_with_route_is_refused_and_records_nothing(live, route_args):
    # A valid contract edit, so the only thing wrong with the amendment is its route.
    live.write_plan(PLAN_ONE.replace("- calc.add(2, 3) == 5", "- calc.add(2, 3) == 5\n- calc.add(0, 0) == 0"))
    con = live.con()
    before = _state(con)
    code, out = live.office("amend", "T1", "--contract", *route_args, "--", "change the T1 contract", env=EXTERNAL)
    assert code == 4, out
    assert "amend route" in out, out
    assert _state(con) == before, "a refused amendment records no contract version, amendment, or route"


@pytest.mark.approved
def test_ordinary_amendment_with_route_is_refused(live):
    con = live.con()
    before = _state(con)
    code, out = live.office("amend", "T1", "--as", ROUTE, "--quote", QUOTE, "--", "tweak T1", env=EXTERNAL)
    assert code == 4, out
    assert "amend route" in out, out
    assert _state(con) == before
