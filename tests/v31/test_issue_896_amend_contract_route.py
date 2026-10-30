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
    ("--restart",),
])
def test_contract_amendment_with_route_is_refused_and_records_nothing(live, route_args):
    # A valid contract edit, so the only thing wrong with the amendment is its route.
    live.write_plan(PLAN_ONE.replace("- calc.add(2, 3) == 5", "- calc.add(2, 3) == 5\n- calc.add(0, 0) == 0"))
    con = live.con()
    before = _state(con)
    code, out = live.office("amend", "T1", "--contract", *route_args, "--", "change the T1 contract", env=EXTERNAL)
    assert code == 4, out
    assert "route-not-amendment" in out and "amend route" in out, out
    assert _state(con) == before, "a refused amendment records no contract version, amendment, or route"


@pytest.mark.approved
@pytest.mark.parametrize("flags,needle", [
    (("--contract",), "--contract"),
    (("--requirements",), "--requirements"),
    (("--no-review",), "--no-review"),
    (("--reason", "why"), "--reason"),
    (("--reason", ""), "--reason"),
    (("--add-criterion", "x"), "--add-criterion"),
    (("--add-criterion", ""), "--add-criterion"),
    (("--drop-criterion", "x"), "--drop-criterion"),
    (("--add-non-goal", "x"), "--add-non-goal"),
    (("--drop-non-goal", "x"), "--drop-non-goal"),
    (("--add-accept", "calc.add(0, 0) == 0"), "--add-accept"),
    (("--drop-accept", "calc.add(2, 3) == 5"), "--drop-accept"),
    (("--add-check", "true"), "--add-check"),
    (("--drop-check", "true"), "--drop-check"),
    (("--set", "depends=none"), "--set"),
    (("--set", ""), "--set"),
    (("--add-check", ""), "--add-check"),
    (("--redirect", "P1"), "--redirect"),
    (("--redirect", ""), "--redirect"),
    (("--root-cause", ""), "--root-cause"),
    (("--requirement", ""), "--requirement"),
    (("--reviewer", "same"), "--reviewer"),
])
def test_amend_route_refuses_amendment_flags_it_would_drop(live, flags, needle):
    # A1: `amend route` must not drop an amendment-only flag and exit 0.
    con = live.con()
    before = _state(con)
    code, out = live.office("amend", "route", "T1", "--as", ROUTE, "--quote", QUOTE, *flags, env=EXTERNAL)
    assert code == 4, out
    assert needle in out and "nothing was recorded" in out, out
    assert _state(con) == before


def test_every_amend_flag_is_either_route_kept_or_refused_by_amend_route():
    """A flag added to `office amend` must be classified here, or it would be dropped by `amend route`."""
    import argparse

    from office import cli

    def options(parser):
        return {opt for a in parser._actions if not isinstance(a, argparse._HelpAction) for opt in a.option_strings
                if opt.startswith("--")}

    top = cli._parser()
    (sub,) = [a for a in top._actions if isinstance(a, argparse._SubParsersAction)]
    global_flags = options(top)  # the shared --run/--state-dir/... flags every subcommand inherits
    amend_specific = options(sub.choices["amend"]) - global_flags
    assert amend_specific - {"--as", "--quote", "--restart"} == set(cli._ROUTE_AMEND_ONLY_FLAGS.values())


@pytest.mark.approved
@pytest.mark.parametrize("route_args,needle", [
    (("--as", ROUTE, "--quote", QUOTE), "--as"),
    (("--restart",), "--restart"),
])
def test_ordinary_amendment_with_route_is_refused(live, route_args, needle):
    con = live.con()
    before = _state(con)
    code, out = live.office("amend", "T1", *route_args, "--", "tweak T1", env=EXTERNAL)
    assert code == 4, out
    assert "route-not-amendment" in out and needle in out and "amend route" in out, out
    assert _state(con) == before


@pytest.mark.approved
def test_amend_route_with_as_relaunches_on_the_named_route(live):
    # The positive counterpart: `amend route` keeps working and records the named route.
    code, out = live.office("amend", "route", "T1", "--as", ROUTE, "--quote", QUOTE, env=EXTERNAL)
    assert code == 0, out
    con = live.con()
    (row,) = [dict(r) for r in con.execute("SELECT * FROM dispatches WHERE task_id='T1' ORDER BY started_at")]
    assert row["triple"] == "claude@1/sonnet@high" and row["effort"] == "high", row
    assert json.loads(row["route_json"])["route_change"]["quote"] == QUOTE


@pytest.mark.approved
@pytest.mark.parametrize("route_args", [
    ("--as", ROUTE, "--quote", QUOTE),
    ("--restart", "--as", ROUTE, "--quote", QUOTE),
])
@pytest.mark.parametrize("identity", [
    ("OFFICE_DISPATCH_ID", "OFFICE_ROLE"),
    ("OFFICE_ROLE",),
])
def test_a_worker_cannot_change_its_own_route(live, route_args, identity):
    # The worker env is the one a dispatch launches with; `amend route` must refuse it when any worker
    # marker is set, not record a user route change. A worker that lost OFFICE_DISPATCH_ID still has OFFICE_ROLE.
    con = live.con()
    d = dict(con.execute("SELECT * FROM dispatches WHERE task_id='T1'").fetchone())
    markers = {"OFFICE_RUN_ID": d["run_id"], "OFFICE_DISPATCH_ID": d["id"], "OFFICE_TASK_ID": "T1",
               "OFFICE_ROLE": "executor", "OFFICE_JOBS": "manual"}
    worker = {k: markers[k] for k in (*identity, "OFFICE_RUN_ID", "OFFICE_TASK_ID", "OFFICE_JOBS")}
    before = _state(con)
    code, out = live.office("amend", "route", "T1", *route_args, env=worker)
    assert code == 4, out
    assert "worker-cannot-change-route" in out, out
    assert _state(con) == before
