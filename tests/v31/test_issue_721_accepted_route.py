"""An accepted producer stays immutable while the next executor route changes."""
import json

import pytest

from conftest import task_row
from test_revoke_accepted import EXTERNAL, _accepted

NEW = "codex/gpt-6-astra@low"
QUOTE = "Use Astra when this task next needs work."


def _dispatch(env, did):
    return dict(env.con().execute("SELECT * FROM dispatches WHERE id=?", (did,)).fetchone())


def test_accepted_task_records_future_route_without_changing_accepted_evidence(env):
    accepted = _accepted(env)
    producer = _dispatch(env, accepted["current_dispatch_id"])
    count = env.con().execute("SELECT COUNT(*) FROM dispatches").fetchone()[0]
    code, out = env.office("amend", "route", "T1", "--as", NEW, "--quote", QUOTE, env=EXTERNAL)
    assert code == 0 and "next reopen" in out, out
    task = task_row(env, "T1")
    for key in ("status", "current_revision_id", "accepted_revision_id", "current_dispatch_id", "contract_version"):
        assert task[key] == accepted[key], key
    assert _dispatch(env, producer["id"]) == producer
    assert env.con().execute("SELECT COUNT(*) FROM dispatches").fetchone()[0] == count
    route = json.loads(task["route_json"])
    assert route["candidate"]["model_id"] == "gpt-6-astra" and route["route_change"]["quote"] == QUOTE
    # A submitted dispatch id still identifies immutable past evidence.
    code, out = env.office("amend", "route", producer["id"], "--as", NEW, "--quote", QUOTE, env=EXTERNAL)
    assert code == 4 and "submitted" in out, out
    env.office("amend", "T1", "--add-accept", "reject nonnumeric arguments", "--", "tighten input handling",
               env=EXTERNAL, check=0)
    reopened = task_row(env, "T1")
    worker = _dispatch(env, reopened["current_dispatch_id"])
    assert reopened["status"] == "running" and worker["id"] != producer["id"]
    assert worker["triple"] == "codex@1/gpt-6-astra@low"
    assert json.loads(worker["route_json"])["route_change"]["quote"] == QUOTE
    assert _dispatch(env, producer["id"]) == producer


def test_revoke_of_new_acceptance_work_does_not_restore_stale_acceptance(env):
    accepted = _accepted(env)
    env.office("amend", "T1", "--add-accept", "reject nonnumeric arguments", "--", "tighten input handling",
               env=EXTERNAL, check=0)
    code, out = env.office("revoke", "T1", "--reason", "change executor")
    task = task_row(env, "T1")
    assert code == 0 and task["status"] == "changes_required", (task, out)
    assert task["accepted_revision_id"] == accepted["accepted_revision_id"]
    assert "stays accepted" not in out and "A1" in out, out
    code, out = env.office("rerun", "T1", "--fresh", "--as", NEW, env=EXTERNAL)
    assert code == 0 and "task-done" not in out, out
    assert _dispatch(env, task_row(env, "T1")["current_dispatch_id"])["triple"] == "codex@1/gpt-6-astra@low"


@pytest.mark.parametrize("block", ["denied", "quota", "quarantined", "capabilities"])
def test_accepted_future_route_rechecks_factual_gates_and_refusal_is_atomic(env, monkeypatch, block):
    accepted = _accepted(env)
    producer = _dispatch(env, accepted["current_dispatch_id"])
    env.office("amend", "route", "T1", "--as", NEW, "--quote", QUOTE, env=EXTERNAL, check=0)
    con = env.con()
    tables = ("dispatches", "plans", "amendments", "deliveries", "leases", "outbox", "route_audit")
    counts = {table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}
    before = task_row(env, "T1")
    extra = EXTERNAL
    if block == "denied":
        (env.tmp / "user-config.yaml").write_text(f"routing:\n  user_policy:\n    denied_models: [{NEW}]\n")
    elif block == "quota":
        from test_adaptive_dispatch import _quota
        extra = _quota(env, codex=1)
    elif block == "quarantined":
        from test_declared_route_426 import _trust
        _trust(monkeypatch, "codex@1/gpt-6-astra@low", "quarantined")
    else:
        from office import adapters
        real = adapters.load_sources
        def without_builder():
            sources = real()
            adapter, *rest = sources["codex"]
            sources["codex"] = ({**adapter, "capabilities": []}, *rest)
            return sources
        monkeypatch.setattr(adapters, "load_sources", without_builder)
    code, out = env.office("amend", "T1", "--add-accept", "reject nonnumeric arguments", "--",
                          "tighten input handling", env=extra)
    assert code == 4 and block in out.lower(), out
    assert {table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables} == counts
    assert _dispatch(env, producer["id"]) == producer
    assert task_row(env, "T1") == before
