"""Finding identity is stable across review rounds (#306 #326 #398 finding codes).

A finding is identified by its scope, its code and its fingerprint. A reviewer who reuses a code for
different content gets a fresh code, so a disposition keyed by code never lands on a finding it was not
given for; a re-raised finding with the fingerprint of an already dispositioned one keeps that
disposition. Both review contracts keep working: the v3.1 plan and code paths rename the same way.

The unit tiers drive the ingest helpers against a real runs.db; the end-to-end tier scripts reviewers
through the CLI like test_convergence_contract.py.
"""
from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path
from unittest import mock

import pytest
from hypothesis import HealthCheck, example, given, settings, strategies as st

from conftest import GOOD_ADD, PLAN_ONE, start_inline

APPROVED = "VERDICT: APPROVED\nNEXT proceed"
GOOD_ADD2 = GOOD_ADD + "# reviewed\n"


def recheck(*findings, nxt="fix the blocking findings"):
    return "\n".join(["VERDICT: RECHECK", *findings, f"NEXT {nxt}"])


def finding(code, owner="T1", severity="medium", blocking=True, where="calc.py:1", what="add is wrong"):
    return (f"FINDING {code} | {severity} | {'blocking' if blocking else 'non-blocking'} | {where} | {what} | fix it"
            f" | owner: {owner}")


def approved_with(*findings):
    return APPROVED.replace("NEXT", "\n".join(findings) + "\nNEXT")


# ------------------------------------------------------------------ a real runs.db, no run

class World:
    """One run's rows in a real runs.db. Ingest helpers are driven directly, one gate per round."""

    SCOPE = {"id": "L-T1", "tasks": ["T1"]}

    def __init__(self, path: Path):
        from office import db, state, version
        self.con = db.connect(path / "runs.db")
        self.run_id = "R" + uuid.uuid4().hex[:8]
        self.con.execute("INSERT INTO runs(id, status, requirements_version, plan_version, office_version) "
                         "VALUES(?, 'executing', 1, 1, ?)", (self.run_id, version.current()))
        self.state = state
        self.n = 0

    @property
    def run(self) -> dict:
        return self.state.get_run(self.con, self.run_id)

    def gate(self, kind: str = "convergence_review") -> dict:
        self.n += 1
        gid = f"G{self.n}-{self.run_id}"
        self.con.execute("INSERT INTO gates(id, run_id, subject, kind, input_key, status, created_at, plan_version, round, scope) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?)", (gid, self.run_id, "lane", kind, f"k{self.n}", "running",
                                                         f"2026-01-01T00:00:{self.n:02d}", 1, self.n, self.SCOPE["id"]))
        return dict(self.con.execute("SELECT * FROM gates WHERE id=?", (gid,)).fetchone())

    def rows(self, where: str = "1=1", args=()) -> list[dict]:
        return [dict(r) for r in self.con.execute(
            f"SELECT * FROM findings WHERE run_id=? AND ({where}) ORDER BY rowid", (self.run_id, *args)).fetchall()]


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.con.close()


def f(code, summary="add is wrong", where="calc.py:1", blocking=False, level="medium"):
    return {"code": code, "summary": summary, "location": where, "blocking": blocking, "severity": level, "level": level,
            "action": "fix it"}


def lane_round(w: World, *findings, resolved=(), retracted=()) -> dict:
    """What convergence.ingest does for one lane review, minus the scope bookkeeping."""
    from office import convergence, gates
    gate = w.gate()
    where, params = "run_id=? AND scope=? AND gate_kind=?", (w.run_id, w.SCOPE["id"], gate["kind"])
    with _tx(w.con):
        stable = gates.stable_codes(w.con, w.run, where, params, list(findings))
        gates.mark_current(w.con, where, params, resolved, ("open",), "resolved")
        gates.mark_current(w.con, where, params, retracted, ("open", "nonblocking"), "retracted")
        owned: dict = {}
        for x in stable:
            convergence._record_finding(w.con, w.run, w.SCOPE, gate, x, None, owned)
    return {"gate": gate, "codes": [x["code"] for x in stable]}


def _tx(con):
    from office import db
    return db.transaction(con)


def disposition(w: World, spec: str, how: str = "dismissed", note: str = "reviewed"):
    from office import convergence
    with mock.patch.dict(os.environ):
        os.environ.pop("OFFICE_DISPATCH_ID", None)
        return convergence.disposition(w.con, w.run, spec, how, note)


# ------------------------------------------------------------------ fresh codes

def test_a_code_reused_for_a_different_finding_is_recorded_under_a_fresh_code(world):
    first = lane_round(world, f("F1", "add is wrong"))
    second = lane_round(world, f("F1", "mul leaks a file handle", where="mul.py:4"))
    assert first["codes"] == ["F1"] and second["codes"] == ["F2"]
    rows = world.rows()
    assert [r["code"] for r in rows] == ["F1", "F2"]
    assert rows[0]["fingerprint"] != rows[1]["fingerprint"]


def test_a_finding_that_only_moved_keeps_its_code(world):
    """Line numbers are not identity: a carried finding re-raised after the code shifted is the same finding."""
    lane_round(world, f("F1", where="calc.py:142"))
    assert lane_round(world, f("F1", where="calc.py:147"))["codes"] == ["F1"]


def test_one_finding_repeated_in_a_reply_under_a_reused_code_is_recorded_once(world):
    lane_round(world, f("F1", "add is wrong"))
    got = lane_round(world, f("F1", "mul leaks a handle", where="mul.py:4"), f("F1", "mul leaks a handle", where="mul.py:4"))
    assert got["codes"] == ["F2", "F2"]


def test_the_same_finding_keeps_its_code(world):
    lane_round(world, f("F1"))
    again = lane_round(world, f("F1"))
    assert again["codes"] == ["F1"]
    assert {r["fingerprint"] for r in world.rows()} == {world.rows()[0]["fingerprint"]}


def test_a_fresh_code_skips_every_code_the_scope_used_in_any_state(world):
    lane_round(world, f("F1", "one"), f("F2", "two"), f("F3", "three"))
    lane_round(world, resolved=["F2"], retracted=["F3"])
    got = lane_round(world, f("F1", "an unrelated finding"))
    assert got["codes"] == ["F4"], "F2 and F3 are resolved and retracted but still taken"


def test_one_reply_reusing_a_code_twice_gets_distinct_codes(world):
    got = lane_round(world, f("F1", "first thing"), f("F1", "second thing"), f("F2", "third thing"))
    assert got["codes"] == ["F1", "F3", "F2"], "the fresh code must not take F2, which the same reply uses"


def test_codes_without_a_number_get_one(world):
    lane_round(world, f("SEAM", "the seam moved"))
    assert lane_round(world, f("SEAM", "another seam"))["codes"] == ["SEAM2"]


def test_a_code_with_a_huge_digit_run_does_not_break_the_next_brief_or_fresh_code(world):
    """A reviewer chooses the code; 5000 digits must neither raise while listing used codes nor while recoding."""
    from office import gates
    huge = "F" + "9" * 5000
    lane_round(world, f(huge, "one"), f("F1", "two"))
    codes = gates.finding_codes(world.con, "run_id=?", (world.run_id,))
    assert codes == ["F1", huge]
    assert lane_round(world, f("F1", "an unrelated finding"))["codes"] == ["F2"]


def test_a_finding_recorded_before_fingerprints_existed_keeps_its_code(world):
    lane_round(world, f("F1"))
    world.con.execute("UPDATE findings SET fingerprint=NULL WHERE run_id=?", (world.run_id,))
    assert lane_round(world, f("F1", "anything at all"))["codes"] == ["F1"]


def test_reusing_a_code_is_announced(world):
    lane_round(world, f("F1"))
    lane_round(world, f("F1", "a different finding", where="mul.py:2"))
    events = [dict(r) for r in world.con.execute("SELECT * FROM events WHERE run_id=? AND kind='finding.recoded'",
                                                 (world.run_id,))]
    assert len(events) == 1 and "F1" in events[0]["summary"] and "F2" in events[0]["summary"]


def test_checks_findings_keep_their_runtime_codes(world):
    """The runtime names check findings C1, C2: the code is the check, not a reviewer's id."""
    from office import gates
    task = {"id": "T1"}
    first, other = f("C1", "pytest failed: test_add assertion"), f("C1", "ruff failed: unused import", where="calc.py:3")
    assert gates._fingerprint(first) != gates._fingerprint(other)
    world.con.execute("INSERT INTO findings(id, status, summary, run_id, task_id, gate_kind, code, fingerprint, state, "
                      "created_at) VALUES('Fx', 'x', 's', ?, 'T1', 'checks', 'C1', ?, 'open', 'now')",
                      (world.run_id, gates._fingerprint(first)))
    assert [x["code"] for x in gates._stable_task_findings(world.con, world.run, task, "checks", [other])] == ["C1"]
    assert [x["code"] for x in gates._stable_task_findings(world.con, world.run, task, "code_review", [other])] == ["C1"]
    world.con.execute("UPDATE findings SET gate_kind='code_review' WHERE id='Fx'")
    assert [x["code"] for x in gates._stable_task_findings(world.con, world.run, task, "code_review", [other])] == ["C2"]


# ------------------------------------------------------------------ dispositions follow identity

def test_a_new_finding_never_inherits_an_old_findings_disposition(world):
    lane_round(world, f("F1", "add is wrong"))
    disposition(world, "L-T1:F1", "dismissed", "comment wording only")
    lane_round(world, f("F1", "mul leaks a file handle", where="mul.py:4"))
    rows = {r["code"]: r for r in world.rows()}
    assert rows["F1"]["disposition"] == "dismissed"
    assert rows["F2"]["disposition"] is None and rows["F2"]["state"] == "nonblocking"


def test_a_re_raised_finding_carries_its_disposition_forward(world):
    lane_round(world, f("F1"))
    disposition(world, "L-T1:F1", "dismissed", "comment wording only")
    lane_round(world, f("F1"))
    old, new = world.rows()
    assert old["id"] != new["id"] and new["code"] == "F1" and new["fingerprint"] == old["fingerprint"]
    assert (new["disposition"], new["disposition_note"], new["disposition_by"], new["disposition_at"]) == (
        old["disposition"], old["disposition_note"], old["disposition_by"], old["disposition_at"])
    assert new["state"] == "nonblocking"


def test_the_disposition_follows_the_fingerprint_not_the_code(world):
    lane_round(world, f("F1", "add is wrong"))
    disposition(world, "L-T1:F1", "follow-up", "tracked in #12")
    got = lane_round(world, f("F7", "add is wrong"))  # the same finding, under another code
    new = world.rows("code='F7'")[0]
    assert got["codes"] == ["F7"] and new["disposition"] == "follow-up" and new["disposition_note"] == "tracked in #12"


def test_a_repair_in_flight_is_not_carried_forward(world):
    lane_round(world, f("F1"))
    disposition(world, "L-T1:F1", "fix")
    lane_round(world, f("F1"))
    assert [r["disposition"] for r in world.rows()] == ["fix", None]


def test_a_blocking_re_raise_is_not_carried_forward(world):
    lane_round(world, f("F1"))
    disposition(world, "L-T1:F1", "fixed", "done")
    lane_round(world, f("F1", blocking=True))
    new = world.rows()[-1]
    assert new["state"] == "open" and new["disposition"] is None


def test_disposition_targets_only_the_current_finding_row(world):
    lane_round(world, f("F1"))
    disposition(world, "L-T1:F1", "dismissed", "first call")
    lane_round(world, f("F1"))  # same finding again: a new row carrying the disposition
    old_before = world.rows()[0]
    disposition(world, "L-T1:F1", "follow-up", "tracked in #9")
    old, new = world.rows()
    assert old == old_before, "the older row is never rewritten"
    assert new["disposition"] == "follow-up" and new["disposition_note"] == "tracked in #9"


def test_a_code_whose_newest_row_is_resolved_has_nothing_to_disposition(world):
    lane_round(world, f("F1", blocking=True))
    disposition_target = world.rows()[0]
    world.con.execute("UPDATE findings SET state='nonblocking', disposition='dismissed' WHERE id=?",
                      (disposition_target["id"],))
    lane_round(world, f("F1", blocking=True))  # same finding again, blocking: the old row stays as it was
    lane_round(world, resolved=["F1"])
    from office.state import Usage
    with pytest.raises(Usage) as e:
        disposition(world, "L-T1:F1")
    assert e.value.category == "unknown-finding"
    assert world.rows()[0]["state"] == "nonblocking" and world.rows()[0]["disposition"] == "dismissed"


def test_resolve_and_retract_leave_older_rows_alone(world):
    lane_round(world, f("F1"))
    disposition(world, "L-T1:F1", "dismissed", "ok")
    lane_round(world, f("F1"))
    old_before = world.rows()[0]
    lane_round(world, retracted=["F1"])
    old, new = world.rows()
    assert old == old_before and old["state"] == "nonblocking"
    assert new["state"] == "retracted"


# ------------------------------------------------------------------ the property

CODES = ["F1", "F2", "F3"]
TEXTS = [("add is wrong", "calc.py:1"), ("mul leaks a handle", "mul.py:4"), ("README typo", "README.md:2"),
         ("missing test", "tests/t.py:9")]
HOW = ["fixed", "dismissed", "follow-up"]

reported = st.lists(st.tuples(st.sampled_from(CODES), st.sampled_from(TEXTS), st.booleans()), max_size=4)
orchestrator = st.lists(st.tuples(st.sampled_from(CODES), st.sampled_from(HOW)), max_size=3)
rounds_of = st.lists(st.tuples(reported, orchestrator), min_size=1, max_size=7)


@given(rounds=rounds_of)
@example(rounds=[([("F1", TEXTS[0], False)], [("F1", "dismissed")]), ([("F1", TEXTS[1], False)], [("F1", "fixed")]),
                 ([("F1", TEXTS[0], False)], [])])
@example(rounds=[([("F1", TEXTS[0], False)], [("F1", "dismissed")]), ([("F1", TEXTS[1], False)], []),
                 ([("F1", TEXTS[1], False)], [("F1", "follow-up")])])
@settings(deadline=None, max_examples=40, suppress_health_check=[HealthCheck.too_slow])
def test_no_disposition_is_attached_to_a_finding_whose_fingerprint_differs(rounds):
    """Across generated rounds that reuse codes: a disposition only ever lands on the finding it was
    given for, and a new row carries one only from a row with its own fingerprint."""
    from office.state import Refused, Usage
    with tempfile.TemporaryDirectory() as tmp:
        w = World(Path(tmp))
        given: dict[str, set[str]] = {}  # fingerprint -> dispositions the orchestrator gave it
        try:
            for reports, actions in rounds:
                before = {r["id"] for r in w.rows()}
                lane_round(w, *[f(code, what, where, blocking=blocking) for code, (what, where), blocking in reports])
                for r in w.rows():
                    if r["id"] not in before and r["disposition"]:
                        assert r["disposition"] in given.get(r["fingerprint"], ()), (
                            f"{r['code']} {r['fingerprint'][:12]} carries {r['disposition']} from a different finding")
                    if r["id"] not in before and r["state"] == "nonblocking" and not r["disposition"]:
                        assert not given.get(r["fingerprint"], set()) - {"fix"}, (
                            f"{r['code']} was dispositioned under this fingerprint but the re-raise carries none")
                for code, how in actions:
                    rows = w.rows("code=?", (code,))
                    if not rows:
                        continue
                    seen = rows[-1]["fingerprint"]  # the finding `office status` shows for the code
                    snap = {r["id"]: (r["disposition"], r["disposition_note"], r["disposition_at"]) for r in w.rows()}
                    try:
                        disposition(w, f"L-T1:{code}", how, "note")
                    except (Usage, Refused):
                        continue
                    for r in w.rows():
                        if snap[r["id"]] != (r["disposition"], r["disposition_note"], r["disposition_at"]):
                            assert r["fingerprint"] == seen, f"{how} given for {seen[:12]} landed on {r['fingerprint'][:12]}"
                            given.setdefault(seen, set()).add(how)
            by_code: dict[str, set[str]] = {}
            for r in w.rows():
                by_code.setdefault(r["code"], set()).add(r["fingerprint"])
            assert all(len(fps) == 1 for fps in by_code.values()), by_code
        finally:
            w.con.close()


# ------------------------------------------------------------------ plan review, both contracts

def plan_round(w: World, *findings, resolved=(), retracted=(), verdict="APPROVED"):
    """One convergence plan review result, through plans._ingest_convergence."""
    from office import contract, plans, review_parse
    gid = f"P{uuid.uuid4().hex[:8]}"
    w.n += 1
    w.con.execute("INSERT INTO gates(id, run_id, subject, kind, input_key, status, created_at, plan_version, round) "
                  "VALUES(?,?,?,?,?,?,?,?,?)", (gid, w.run_id, "plan", "plan_review", f"p{w.n}", "running",
                                               f"2026-01-01T00:00:{w.n:02d}", 1, w.n))
    parsed = review_parse.Parsed(verdict=verdict, findings=list(findings), resolved=list(resolved),
                                 retracted=[{"code": c, "why": "wrong"} for c in retracted], contract=contract.CONVERGENCE)
    with _tx(w.con):
        plans._ingest_convergence(w.con, w.run, gid, {"status": contract.COMPLETED, "verdict": verdict, "parsed": parsed})
    return parsed


def test_convergence_plan_review_renames_a_reused_code_and_keeps_the_disposition_on_its_own_finding(world):
    plan_round(world, f("P1", "the contract is unclear", where="T1"))
    disposition(world, "plan:P1", "dismissed", "fine as written")
    got = plan_round(world, f("P1", "T2 has no rollback", where="T2"))
    assert [x["code"] for x in got.findings] == ["P2"], "the caller sees the code the finding is recorded under"
    rows = {r["code"]: r for r in world.rows()}
    assert rows["P1"]["disposition"] == "dismissed" and rows["P2"]["disposition"] is None
    assert rows["P2"]["fingerprint"] and rows["P2"]["state"] == "nonblocking"


def test_convergence_plan_review_carries_a_disposition_to_a_re_raised_finding(world):
    plan_round(world, f("P1", "the contract is unclear", where="T1"))
    disposition(world, "plan:P1", "follow-up", "tracked in #4")
    plan_round(world, retracted=["P1"])
    assert world.rows()[0]["state"] == "retracted"
    plan_round(world, f("P1", "the contract is unclear", where="T1"))
    old, new = world.rows()
    assert old["state"] == "retracted" and new["state"] == "nonblocking"
    assert (new["disposition"], new["disposition_note"]) == ("follow-up", "tracked in #4")


def test_a_reused_plan_code_does_not_rewrite_the_old_finding_in_place(world):
    plan_round(world, f("P1", "the contract is unclear", where="T1", blocking=True))
    plan_round(world, f("P1", "T2 has no rollback", where="T2", blocking=True), verdict="RECHECK")
    rows = {r["code"]: r for r in world.rows()}
    assert rows["P1"]["summary"] == "the contract is unclear" and rows["P1"]["state"] == "resolved", (
        "the reviewer dropped the old finding when it reused its code")
    assert rows["P2"]["summary"] == "T2 has no rollback" and rows["P2"]["state"] == "open"


# ------------------------------------------------------------------ v3.1 plan and code paths

def v31_reply(*lines, verdict="CHANGES_REQUIRED"):
    return "\n".join([f"VERDICT: {verdict}", *lines])


def v31_plan_round(w: World, reply: str):
    from office import plans, review_parse
    gid = f"P{uuid.uuid4().hex[:8]}"
    w.n += 1
    w.con.execute("INSERT INTO gates(id, run_id, subject, kind, input_key, status, created_at, plan_version, round) "
                  "VALUES(?,?,?,?,?,?,?,?,?)", (gid, w.run_id, "plan", "plan_review", f"p{w.n}", "running",
                                               f"2026-01-01T00:00:{w.n:02d}", 1, w.n))
    parsed = review_parse.parse(reply, plan_review=True)
    assert parsed.valid, parsed.errors
    with _tx(w.con):
        plans.ingest_plan_review(w.con, w.run, gid, {"verdict": parsed.verdict, "parsed": parsed, "route": "fake/r",
                                                     "dispatch_id": None, "summary": "v3.1"})


def test_v31_plan_review_records_a_reused_code_for_a_different_finding_under_a_fresh_code(world):
    v31_plan_round(world, v31_reply("FINDING P1 | material | T1 | the contract is unclear | add one"))
    v31_plan_round(world, v31_reply("FINDING P1 | material | T2 | T2 has no rollback | add one"))
    rows = {r["code"]: r for r in world.rows("gate_kind='plan_review'")}
    assert set(rows) == {"P1", "P2"}
    assert rows["P1"]["summary"] == "the contract is unclear" and rows["P2"]["summary"] == "T2 has no rollback"


def test_v31_a_defect_line_reusing_a_findings_code_in_the_same_reply_still_upgrades_that_finding(world):
    reply = v31_reply("FINDING P2 | material | T2 | mul reuses calc.add | implement it directly",
                      "DEFECT P2 | requirement-contradiction | T2 | mul must reuse calc.add, which cannot multiply | "
                      "evidence: calc.py:1 adds only", verdict="PLAN_DEFECT")
    v31_plan_round(world, reply)
    v31_plan_round(world, reply)
    rows = world.rows("gate_kind='plan_review'")
    assert [(r["code"], r["category"], r["state"]) for r in rows] == [("P2", "requirement-contradiction", "open")]


def _task_gate_reply(w: World, reply: str, *, current: bool = True):
    """One v3.1 code-review result through gates.ingest_task_gate (a task, its revision and gate are inserted)."""
    from office import gates, review_parse
    gid = f"C{uuid.uuid4().hex[:8]}"
    w.n += 1
    if not w.con.execute("SELECT 1 FROM tasks WHERE run_id=? AND id='T1'", (w.run_id,)).fetchone():
        w.con.execute("INSERT INTO tasks(run_id, id, title, role, scope_json, depends_json, accept_json, checks_json, status, "
                      "introduced_plan_version, contract_version, acceptance_version, created_at, updated_at) "
                      "VALUES(?, 'T1', 't', 'executor', '[]', '[]', '[]', '[]', 'submitted', 1, 1, 1, 'n', 'n')", (w.run_id,))
    w.con.execute("UPDATE tasks SET current_revision_id=? WHERE run_id=? AND id='T1'", ("REV-OLD" if not current else "REV1", w.run_id))
    w.con.execute("INSERT INTO gates(id, run_id, subject, task_id, revision_id, kind, input_key, status, created_at, round, plan_version) "
                  "VALUES(?,?,'task','T1','REV1','code_review',?, 'running', ?, ?, 1)",
                  (gid, w.run_id, f"k{w.n}", f"2026-01-01T00:00:{w.n:02d}", w.n))
    parsed = review_parse.parse(reply)
    assert parsed.valid, parsed.errors
    with _tx(w.con):
        gates.ingest_task_gate(w.con, w.run, gid, {"verdict": parsed.verdict, "parsed": parsed, "route": "fake/r",
                                                   "dispatch_id": None, "summary": "v3.1"})


def test_v31_code_review_records_a_reused_code_for_a_different_finding_under_a_fresh_code(world):
    _task_gate_reply(world, "VERDICT: CHANGES_REQUIRED\nFINDING F1 | material | calc.py:1 | add is wrong | fix it")
    _task_gate_reply(world, "VERDICT: CHANGES_REQUIRED\nFINDING F1 | material | mul.py:4 | mul leaks a handle | fix it")
    rows = {r["code"]: r for r in world.rows("gate_kind='code_review'")}
    assert set(rows) == {"F1", "F2"}
    assert rows["F1"]["summary"] == "add is wrong" and rows["F1"]["state"] == "open", "the old finding is not overwritten"
    assert rows["F2"]["summary"] == "mul leaks a handle" and rows["F2"]["state"] == "open"
    _task_gate_reply(world, "VERDICT: CHANGES_REQUIRED\nFINDING F1 | material | calc.py:1 | add is wrong | fix it")
    assert len(world.rows("gate_kind='code_review' AND code='F1'")) == 1, "the same finding keeps its row and its code"


def test_v31_a_stale_code_review_result_carries_a_reused_code_under_a_fresh_code(world):
    _task_gate_reply(world, "VERDICT: CHANGES_REQUIRED\nFINDING F1 | material | calc.py:1 | add is wrong | fix it")
    _task_gate_reply(world, "VERDICT: CHANGES_REQUIRED\nFINDING F1 | material | mul.py:4 | mul leaks a handle | fix it",
                     current=False)
    assert sorted(r["code"] for r in world.rows("gate_kind='code_review'")) == ["F1", "F2"]


def test_v31_plan_defect_reusing_a_code_with_new_content_is_renamed_and_its_finding_follows(world):
    first = v31_reply("DEFECT P2 | requirement-contradiction | T2 | mul must reuse calc.add | evidence: calc.py:1",
                      verdict="PLAN_DEFECT")
    second = v31_reply("FINDING P2 | material | T3 | sub is missing | add it",
                       "DEFECT P2 | requirement-contradiction | T3 | sub must not depend on mul | evidence: sub.py:1",
                       verdict="PLAN_DEFECT")
    v31_plan_round(world, first)
    v31_plan_round(world, second)
    rows = world.rows("gate_kind='plan_review'")
    assert [(r["code"], r["category"], r["location"]) for r in rows] == [
        ("P2", "requirement-contradiction", "T2"), ("P3", "requirement-contradiction", "T3")], rows


def test_v31_a_fresh_defect_code_never_collides_with_a_code_the_same_reply_uses(world):
    v31_plan_round(world, v31_reply("DEFECT P2 | requirement-contradiction | T2 | one | evidence: a", verdict="PLAN_DEFECT"))
    v31_plan_round(world, v31_reply("FINDING P3 | material | T9 | unrelated | x",
                                    "DEFECT P2 | requirement-contradiction | T3 | two | evidence: b", verdict="PLAN_DEFECT"))
    codes = [r["code"] for r in world.rows("gate_kind='plan_review'")]
    assert sorted(codes) == ["P2", "P3", "P4"], codes


# ------------------------------------------------------------------ briefs

def test_review_briefs_list_the_codes_already_used():
    from office import briefs
    line = briefs.used_codes_line(["F1", "F2", "F10"])
    assert len(line) == 1 and "F1, F2, F10" in line[0] and "new finding takes a new code" in line[0]
    assert briefs.used_codes_line([]) == [] and briefs.used_codes_line(None) == []
    run = {"id": "R", "goal": "g", "gates": {"review_contract": "v3.1"}, "repo_root": "/x"}
    task = {"id": "T1", "title": "t", "accept": [], "scope": ["a.py"]}
    rev = {"id": "V1", "commit_sha": "a" * 40}
    text = briefs.code_review_brief(run, task, rev, "diff", "none", [], "/co", used_codes=["F1", "F4"])
    assert "FINDING CODES ALREADY USED in this scope (any state): F1, F4" in text
    assert "ALREADY USED" not in briefs.code_review_brief(run, task, rev, "diff", "none", [], "/co")
    scope = {"id": "L-T1", "shared": False}
    conv = briefs.convergence_review_brief(run, scope, [task], rev, "diff", "T1 APPROVED", [], "/co", 1, used_codes=["F2"])
    assert "FINDING CODES ALREADY USED in this scope (any state): F2" in conv
    plan = {"version": 1, "body": "# Plan"}
    for contract_name in ("convergence-v1", "v3.1"):
        run = {"id": "R", "goal": "g", "gates": {"review_contract": contract_name}}
        text = briefs.plan_review_brief(run, plan, {}, [], False, used_codes=["P1", "P3"])
        assert "FINDING CODES ALREADY USED in this scope (any state): P1, P3" in text, contract_name


# ------------------------------------------------------------------ end to end through the CLI

def _q(env, sql, args=()):
    con = env.con()
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


def _start(env, plan=PLAN_ONE, **script):
    env.trust()
    env.script(**script)
    start_inline(env, plan=plan, gear="direct+review")
    env.office("approve", "plan", "--quote", "approved", check=0)


def test_lane_review_records_a_reused_code_as_a_new_finding_and_the_brief_lists_used_codes(env, monkeypatch):
    from office import briefs
    seen = []
    real = briefs.convergence_review_brief

    def spy(*args, **kwargs):
        seen.append(kwargs.get("used_codes"))
        return real(*args, **kwargs)

    monkeypatch.setattr(briefs, "convergence_review_brief", spy)
    _start(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}, {"write": {"calc.py": GOOD_ADD2}, "submit": True}],
           convergence_reviewer=[{"reply": recheck(finding("F1"))},
                                 {"reply": approved_with(finding("F1", blocking=False, where="README.md:9", what="typo"))}])
    env.office("dispatch", "T1", check=0)
    env.office("rerun", "T1", "--fresh", check=0)
    rows = {r["code"]: r for r in _q(env, "SELECT * FROM findings WHERE scope='L-T1'")}
    assert set(rows) == {"F1", "F2"}, rows
    assert rows["F1"]["state"] == "resolved" and rows["F1"]["location"] == "calc.py:1"
    assert rows["F2"]["state"] == "nonblocking" and rows["F2"]["location"] == "README.md:9"
    assert seen and seen[0] == [] and seen[-1] == ["F1"], seen
    code, out = env.office("disposition", "L-T1:F1", "dismissed", "--", "x")
    assert code == 2 and "unknown-finding" in out, out
    env.office("disposition", "L-T1:F2", "dismissed", "--", "typo is intended", check=0)
    after = {r["code"]: r["disposition"] for r in _q(env, "SELECT code, disposition FROM findings WHERE scope='L-T1'")}
    assert after == {"F1": None, "F2": "dismissed"}


def test_lane_re_review_carries_the_disposition_to_the_re_raised_finding(env):
    note = finding("F1", severity="low", blocking=False, where="calc.py:1", what="comment wording only")
    _start(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}, {"write": {"calc.py": GOOD_ADD2}, "submit": True}],
           convergence_reviewer=[{"reply": approved_with(note)}])
    env.office("dispatch", "T1", check=0)
    env.office("disposition", "L-T1:F1", "dismissed", "--", "comment wording only", check=0)
    from conftest import PLAN_ONE as plan
    env.write_plan(plan.replace("scope: calc.py", "scope: calc.py, calc_helpers.py"))
    env.office("amend", "T1", "--contract", "--", "widen the envelope", check=0)
    env.office("rerun", "T1", "--fresh", check=0)
    rows = _q(env, "SELECT code, state, disposition, disposition_note FROM findings WHERE scope='L-T1' ORDER BY rowid")
    assert [(r["code"], r["state"], r["disposition"]) for r in rows] == [("F1", "nonblocking", "dismissed")] * 2, rows
    assert rows[1]["disposition_note"] == "comment wording only"
    code, out = env.office("close", "--handoff", "https://example.test/pr/1")
    assert "await a disposition" not in out, out


# ------------------------------------------------------------------ unavailable attempts spend no round (v3.1)

@pytest.mark.review_contract("v3.1")
def test_v31_unavailable_code_review_spends_no_round(env):
    """The v3.1 round is a CHANGES_REQUIRED revision: an UNAVAILABLE attempt does not advance it, so the
    retry is round 1 and a later CHANGES_REQUIRED is the first round, not the second."""
    from office import gates
    _start(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], code_reviewer=[{"exit": 1}])
    env.office("dispatch", "T1", check=0)
    rows = _q(env, "SELECT round, verdict FROM gates WHERE kind='code_review' ORDER BY created_at")
    assert rows and rows[-1]["verdict"] == "UNAVAILABLE" and rows[-1]["round"] == 1, rows
    con = env.con()
    run_id = con.execute("SELECT id FROM runs").fetchone()[0]
    assert gates.current_round(con, run_id, "T1", "code_review") == 1
    con.close()
    env.script(code_reviewer=[{"reply": "VERDICT: PASS"}])
    producer = _q(env, "SELECT d.model FROM revisions r JOIN dispatches d ON d.id=r.dispatch_id")[0]["model"]
    reviewer = "codex/gpt-6-luna@xhigh" if "claude" in producer else "claude/claude-opus-5-5@high"
    env.office("dispatch", "T1", "--review-as", reviewer, check=0)
    rows = _q(env, "SELECT round, verdict FROM gates WHERE kind='code_review' ORDER BY created_at")
    assert [(r["round"], r["verdict"]) for r in rows] == [(1, "UNAVAILABLE"), (1, "PASS")], rows


def test_lane_retract_in_ingest_touches_only_the_current_finding(env):
    """convergence.ingest, not just its helpers: with two generations of F1 (the first dispositioned, the
    second its re-raise), a later RETRACT F1 changes the newest row only."""
    note = finding("F1", severity="low", blocking=False, where="calc.py:1", what="comment wording only")
    _start(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}, {"write": {"calc.py": GOOD_ADD2}, "submit": True},
                          {"write": {"calc.py": GOOD_ADD2 + "# again\n"}, "submit": True},
                          {"write": {"calc.py": GOOD_ADD2 + "# and again\n"}, "submit": True}],
           convergence_reviewer=[{"reply": approved_with(note)}, {"reply": approved_with(note)},
                                 {"reply": APPROVED + "\nRETRACT F1 | wrong"}])
    env.office("dispatch", "T1", check=0)
    env.office("disposition", "L-T1:F1", "dismissed", "--", "comment wording only", check=0)
    for n, widened in enumerate(("calc.py, a.py", "calc.py, a.py, b.py", "calc.py, b.py, c.py")):
        env.write_plan(PLAN_ONE.replace("scope: calc.py", f"scope: {widened}"))
        env.office("amend", "T1", "--contract", "--", f"widen {n}", check=0)
        env.office("rerun", "T1", "--fresh", check=0)
        if n == 0:
            first = _q(env, "SELECT * FROM findings WHERE scope='L-T1' ORDER BY rowid")
            assert len(first) == 2 and first[1]["disposition"] == "dismissed", first
    rows = _q(env, "SELECT * FROM findings WHERE scope='L-T1' ORDER BY rowid")
    assert len(_q(env, "SELECT 1 FROM gates WHERE kind='convergence_review'")) == 3
    assert [r["state"] for r in rows] == ["nonblocking", "retracted"], rows
    assert rows[0] == first[0], "the older generation is never rewritten"
    moved = {"state", "updated_at"}
    assert rows[1]["state"] == "retracted" and {k: v for k, v in rows[1].items() if k not in moved} == {
        k: v for k, v in first[1].items() if k not in moved}


@pytest.mark.review_contract("v3.1")
def test_v31_code_review_brief_lists_the_codes_the_task_already_used(env, monkeypatch):
    from office import briefs
    seen = []
    real = briefs.code_review_brief

    def spy(*args, **kwargs):
        seen.append(kwargs.get("used_codes"))
        return real(*args, **kwargs)

    monkeypatch.setattr(briefs, "code_review_brief", spy)
    _start(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}, {"write": {"calc.py": GOOD_ADD2}, "submit": True}],
           code_reviewer=[{"reply": "VERDICT: CHANGES_REQUIRED\nFINDING F1 | material | calc.py:1 | add is wrong | fix it"},
                          {"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    env.office("rerun", "T1", "--fresh", check=0)
    assert seen and seen[0] == [] and seen[-1] == ["F1"], seen
