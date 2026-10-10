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


def _dump(env) -> str:
    con = env.con()
    try:
        return "\n".join(con.iterdump())
    finally:
        con.close()


def test_context_writes_nothing_on_a_self_improve_armed_run(env):
    con, run = _con_run(env)
    try:
        from office import bugwatch
        bugwatch.arm(con, run["id"])
        con.execute("INSERT INTO events(run_id, kind, audience, summary, office_version, created_at) "
                    "VALUES(?,?,?,?,?,?)", (run["id"], "worker.failed", "orchestrator", "boom", "t", "2030"))
    finally:
        con.close()
    before = _dump(env)
    env.office("context", check=0)
    env.ojson("context", "--since", "0")
    assert _dump(env) == before


def test_hash_budget_leaves_later_artifacts_unhashed_not_stale(env, monkeypatch):
    from office import context_snapshot, state
    con, run = _con_run(env)
    try:
        paths = []
        for i in range(3):
            p = env.tmp / f"big-{i}.bin"
            p.write_bytes(b"x" * 1000)
            state.record_evidence(con, run["id"], "note", p)
            paths.append(p)
        monkeypatch.setattr(context_snapshot, "HASH_BUDGET_BYTES", 1500)
        snap = context_snapshot.snapshot(con, state.get_run(con, run["id"]))
    finally:
        con.close()
    statuses = sorted(a["status"] for a in snap["artifacts"] if a["path"] in map(str, paths))
    assert statuses == ["ok", "unhashed", "unhashed"], statuses
    assert not any("big-" in s for s in snap["stale"])


def test_many_tasks_and_json_payload_stay_bounded(env):
    import json
    con, run = _con_run(env)
    try:
        for i in range(200):
            con.execute("INSERT INTO tasks(run_id, id, title, role, scope_json, depends_json, accept_json, checks_json, "
                        "status, introduced_plan_version, contract_version, acceptance_version, created_at, updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (run["id"], f"X{i:03}", "t" * 3000, "executor", "[]", "[]", "[]", "[]",
                         "accepted" if i % 2 else "pending", 1, 1, 1, "2030", "2030"))
        for i in range(30):
            con.execute("INSERT INTO authorizations(id, run_id, kind, target, authorized_by, quote, created_at) "
                        "VALUES(?,?,?,?,?,?,?)", (f"A{i}", run["id"], "plan", "z" * 5000, "u", "q", "2030"))
    finally:
        con.close()
    code, data = env.ojson("context", "--since", "0")
    snap = data["data"]
    assert len(json.dumps(snap)) <= 24000 and snap["tasks"]["total"] == 201
    assert len(snap["tasks"]["open"]) <= 40 and snap["truncated"].get("tasks")
