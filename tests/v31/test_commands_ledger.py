"""Command receipts: idempotent record, explicit transitions, interrupted recovery."""
from __future__ import annotations

import json

import pytest

from office import commands, db
from office.state import OfficeError


@pytest.fixture
def con(tmp_path):
    c = db.connect(tmp_path / "runs.db")
    yield c
    c.close()


def _record(con, command_id="c1", payload=None):
    return commands.record(con, command_id=command_id, kind="queue.add", target="#12",
                           payload=payload or {"ref": "#12"}, origin="cli")


def test_record_inserts_an_accepted_receipt(con):
    r = _record(con)
    assert (r["status"], r["replayed"], r["origin"], r["target"]) == ("accepted", False, "cli", "#12")
    assert r["accepted_at"] and r["started_at"] is None


def test_same_id_same_payload_returns_the_existing_receipt(con):
    first = _record(con)
    commands.transition(con, "c1", "running", pid=4242)
    again = _record(con)
    assert again["replayed"] is True
    assert again["status"] == "running" and again["accepted_at"] == first["accepted_at"]
    assert con.execute("SELECT COUNT(*) FROM commands").fetchone()[0] == 1


def test_same_id_different_payload_is_refused(con):
    _record(con)
    with pytest.raises(OfficeError) as exc:
        _record(con, payload={"ref": "#13"})
    assert exc.value.category == "idempotency-conflict"
    assert json.loads(commands.get(con, "c1")["payload_json"]) == {"ref": "#12"}


@pytest.mark.parametrize("end", ["completed", "failed", "unknown"])
def test_transitions_record_timestamps_and_outcome(con, end):
    _record(con)
    running = commands.transition(con, "c1", "running", pid=99)
    assert running["status"] == "running" and running["pid"] == 99 and running["started_at"]
    done = commands.transition(con, "c1", end, result={"ok": end == "completed"},
                               error=None if end == "completed" else "boom")
    assert done["status"] == end and done["finished_at"]
    assert ("ok" in (done["result_json"] or "")) and (done["error"] == (None if end == "completed" else "boom"))


@pytest.mark.parametrize("start,to", [("accepted", "completed"), ("accepted", "failed"), ("completed", "running")])
def test_illegal_transitions_are_refused(con, start, to):
    _record(con)
    if start == "completed":
        commands.transition(con, "c1", "running")
        commands.transition(con, "c1", "completed")
    with pytest.raises(OfficeError) as exc:
        commands.transition(con, "c1", to)
    assert exc.value.category == "invalid-transition"


def test_recover_interrupted_marks_dead_executors_unknown(con):
    for cid, pid in (("dead", 111), ("live", 222), ("nopid", None)):
        _record(con, cid)
        commands.transition(con, cid, "running", pid=pid)
    _record(con, "waiting")
    lost = commands.recover_interrupted(con, alive_pids={222})
    assert sorted(lost) == ["dead", "nopid"]
    status = {r["id"]: r["status"] for r in con.execute("SELECT id, status FROM commands")}
    assert status == {"dead": "unknown", "nopid": "unknown", "live": "running", "waiting": "accepted"}
    assert "failed" not in status.values()
    # Unknown is terminal: it is never retried or failed later.
    with pytest.raises(OfficeError):
        commands.transition(con, "dead", "failed")
    assert commands.recover_interrupted(con, alive_pids=set()) == ["live"]
