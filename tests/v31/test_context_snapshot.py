"""`office context` (#502 PR 2): a bounded, read-only root snapshot that flags stale references."""
from __future__ import annotations

import pytest

from conftest import PLAN_ONE

pytestmark = pytest.mark.approved


def _con_run(env):
    con = env.con()
    return con, con.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()


def _total_changes(env) -> tuple:
    con, run = _con_run(env)
    try:
        return (con.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                con.execute("SELECT COUNT(*) FROM cursors").fetchone()[0],
                con.execute("SELECT MAX(last_seq) FROM cursors").fetchone()[0], run["updated_at"])
    finally:
        con.close()


def test_context_is_actionable_and_read_only(env):
    before = _total_changes(env)
    code, data = env.ojson("context")
    assert code == 0, data
    snap = data["data"]
    assert snap["run"]["goal"] == "fixture goal" and snap["plan"]["version"] >= 1
    assert snap["requirements"]["done"] == ["add() returns the sum"]
    assert [t["id"] for t in snap["tasks"]["open"]] == ["T1"]
    assert data["next"] and data["next"] == snap["next"]
    assert snap["cursor"]["event_seq"] > 0 and snap["stale"] == []
    assert _total_changes(env) == before, "office context writes nothing"
    code, out = env.office("context")
    assert "stale: none" in out and "next:" in out


def test_since_lists_bounded_events_after_the_cursor(env):
    code, data = env.ojson("context", "--since", "0")
    events = data["data"]["events"]
    assert events and all(e["seq"] > 0 for e in events) and len(events) <= 20
    last = data["data"]["cursor"]["event_seq"]
    code, data = env.ojson("context", "--since", str(last))
    assert data["data"]["events"] == []


def test_stale_plan_draft_and_evidence_are_flagged(env):
    env.write_plan(PLAN_ONE.replace("add() returns the sum", "add() returns the product"))
    con, run = _con_run(env)
    try:
        good = env.tmp / "ev-good.txt"
        good.write_text("ok")
        from office import state
        state.record_evidence(con, run["id"], "note", good)
        changed = env.tmp / "ev-changed.txt"
        changed.write_text("before")
        state.record_evidence(con, run["id"], "note", changed)
        changed.write_text("after")
        gone = env.tmp / "ev-gone.txt"
        gone.write_text("x")
        state.record_evidence(con, run["id"], "note", gone)
        gone.unlink()
    finally:
        con.close()
    code, data = env.ojson("context")
    stale = "\n".join(data["data"]["stale"])
    assert "plan draft differs" in stale, stale
    assert f"stale: {changed}" in stale, stale
    assert f"missing: {gone}" in stale and str(good) not in stale, stale
    statuses = {a["path"]: a["status"] for a in data["data"]["artifacts"]}
    assert statuses[str(good)] == "ok" and statuses[str(changed)] == "stale" and statuses[str(gone)] == "missing"


def test_output_stays_bounded_with_many_records(env):
    con, run = _con_run(env)
    try:
        for i in range(300):
            con.execute("INSERT INTO events(run_id, kind, audience, summary, office_version, created_at) "
                        "VALUES(?,?,?,?,?,?)", (run["id"], "note", "orchestrator", "x" * 5000, "t", "2030"))
        from office import state
        for i in range(60):
            state.record_evidence(con, run["id"], "note", None, digest="sha256:00")
        con.execute("UPDATE runs SET goal=? WHERE id=?", ("g" * 20000, run["id"]))
    finally:
        con.close()
    code, out = env.office("context", "--since", "0")
    assert code == 0 and len(out) < 20000, len(out)
    code, data = env.ojson("context", "--since", "0")
    snap = data["data"]
    assert len(snap["events"]) == 20 and snap["events_truncated"]
    assert len(snap["run"]["goal"]) <= 400 and snap["truncated"].get("evidence")
    assert all(len(e["summary"]) <= 200 for e in snap["events"])


def test_pinned_runtime_mismatch_is_visible(env):
    con, run = _con_run(env)
    try:
        con.execute("UPDATE runs SET office_version='2.9' WHERE id=?", (run["id"],))
        from office import context_snapshot, state
        snap = context_snapshot.snapshot(con, state.get_run(con, run["id"]))
    finally:
        con.close()
    assert any("pinned to office 2.9" in s for s in snap["stale"]), snap["stale"]
