"""The convergence reviewer states the commit it reviewed; a mismatch is INVALID_RESULT (#706).

A resumed reviewer can keep reading a checkout path from an earlier round. Its reply then
reviews another commit, so it is not ingested: the same reviewer is asked again and no
round or finding is recorded for it. Reviewers are scripted fake harnesses.
"""
from __future__ import annotations

import pytest

from conftest import GOOD_ADD
from test_convergence_contract import (APPROVED, _gates, _q, _roles, _scope, _start, finding, recheck)

from office import briefs, convergence

SHA = "0123456789abcdef0123456789abcdef01234567"
STALE = "REVIEWED_COMMIT deadbeefdeadbeef"
EXECUTOR = [{"write": {"calc.py": GOOD_ADD}, "submit": True}]


def _reviews(env):
    return _roles(env).count("convergence_reviewer")


def _findings(env):
    return _q(env, "SELECT code FROM findings")


# ------------------------------------------------------------------ the rule

@pytest.mark.parametrize("stated, ok", [
    (SHA, True), (SHA[:12], True), (SHA[:7], True), (SHA[:7].upper(), True),
    (SHA[:6], False), (None, False), ("", False),
    ("deadbeefdeadbeef", False), ("zzzzzzzzz", False), (SHA[:6] + "x", False), (SHA + "0", False),
])
def test_commit_problem_is_a_prefix_match_of_at_least_seven_hex_characters(stated, ok):
    assert (convergence.commit_problem(SHA, stated) is None) is ok


@pytest.mark.parametrize("reply, stated", [
    (f"REVIEWED_COMMIT {SHA}\nVERDICT: APPROVED", SHA),
    (f"REVIEWED_COMMIT: {SHA[:9]}\nVERDICT: APPROVED", SHA[:9]),
    (f"- `REVIEWED_COMMIT {SHA[:9]}`\nVERDICT: APPROVED", SHA[:9]),
    (f"REVIEWED_COMMIT {STALE[16:]}\nREVIEWED_COMMIT {SHA}", SHA),  # the last statement counts
    ("VERDICT: APPROVED\nNEXT proceed", None),
    ("the REVIEWED_COMMIT line is above\nVERDICT: APPROVED", None),
])
def test_reviewed_commit_is_read_from_the_reply(reply, stated):
    assert convergence.reviewed_commit(reply) == stated


def test_the_brief_names_the_commit_and_retires_earlier_checkouts():
    revision = {"id": "L-T1@0123456789", "commit_sha": SHA, "base_commit": None, "dispatch_id": None}
    scope = {"id": "L-T1", "tasks": ["T1"]}
    brief = briefs.convergence_review_brief({"id": "r", "requirements_version": 1}, scope,
                                            [{"id": "T1", "title": "add"}], revision, "diff", "T1 PASS", [],
                                            "/tmp/checkout-now", 2)
    assert f"REVIEWED_COMMIT {SHA} is the commit under review" in brief
    assert "REVIEWED_COMMIT <the full commit sha you reviewed" in brief  # the reply format
    assert "checkout path named in an earlier round or reply is stale and must not be used" in brief
    assert "/tmp/checkout-now" in brief


# ------------------------------------------------------------------ through the gate

def test_a_matching_commit_is_ingested_as_before(env):
    _start(env, executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    gate = _gates(env, "convergence_review")[0]
    assert gate["verdict"] == "APPROVED" and gate["review_status"] == "COMPLETED" and gate["round"] == 1
    assert _reviews(env) == 1


def test_a_short_prefix_of_seven_or_more_is_accepted(env):
    _start(env, executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED, "stamp_chars": 7}])
    env.office("dispatch", "T1", check=0)
    assert _gates(env, "convergence_review")[0]["verdict"] == "APPROVED"
    assert _reviews(env) == 1


def test_a_reply_about_another_commit_is_asked_again_and_records_nothing(env):
    stale = f"{STALE}\n" + recheck(finding("F1"))
    _start(env, executor=EXECUTOR, convergence_reviewer=[{"reply": stale}, {"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    gates = _gates(env, "convergence_review")
    assert len(gates) == 1 and gates[0]["verdict"] == "APPROVED" and gates[0]["round"] == 1
    assert _reviews(env) == 2
    assert _findings(env) == []  # the stale RECHECK's finding was never recorded
    assert _scope(env, "L-T1")["status"] == "approved"


def test_a_reply_without_the_line_is_asked_again(env):
    _start(env, executor=EXECUTOR,
           convergence_reviewer=[{"reply": APPROVED, "stamp": False}, {"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    gates = _gates(env, "convergence_review")
    assert gates[0]["verdict"] == "APPROVED" and gates[0]["round"] == 1 and _reviews(env) == 2


def test_a_reviewer_that_keeps_naming_another_commit_is_invalid_result_and_spends_no_round(env):
    stale = f"{STALE}\n" + recheck(finding("F1"))
    _start(env, executor=EXECUTOR, convergence_reviewer=[{"reply": stale}] * 4 + [{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    first = _gates(env, "convergence_review")[0]
    assert first["review_status"] == "INVALID_RESULT" and first["verdict"] is None
    assert "not the composed commit" in first["summary"]
    assert _reviews(env) == 4  # the first ask plus review_reprompt_max (3) more
    assert _findings(env) == []
    assert _scope(env, "L-T1")["status"] == "attention"
    env.office("resume", check=0)
    gates = _gates(env, "convergence_review")
    assert gates[-1]["verdict"] == "APPROVED" and gates[-1]["round"] == 1


def test_a_missing_line_that_never_appears_is_invalid_result(env):
    _start(env, executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED, "stamp": False}])
    env.office("dispatch", "T1", check=0)
    first = _gates(env, "convergence_review")[0]
    assert first["review_status"] == "INVALID_RESULT" and "no REVIEWED_COMMIT line" in first["summary"]
    assert _scope(env, "L-T1")["status"] == "attention"


def test_ingest_alone_rejects_a_stated_commit_that_is_not_the_gates(env):
    """The check lives in ingest, not only in the retry loop in front of it."""
    from office import contract, db, review_parse, state
    _start(env, executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    con = env.con()
    try:
        run = state.get_run(con, _q(env, "SELECT id FROM runs")[0]["id"])
        with db.transaction(con):
            scope = convergence.find_scope(con, run, "L-T1")
            st = convergence.scope_state(run, "L-T1")
            gid, _ = convergence._new_gate(con, run, scope, convergence._rev("L-T1", st["commit"]),
                                           "convergence_review", 1, 1)
            parsed = review_parse.parse(recheck(finding("F9")), contract=contract.CONVERGENCE)
            convergence.ingest(con, state.get_run(con, run["id"]), gid,
                               {"status": contract.COMPLETED, "verdict": "RECHECK", "parsed": parsed,
                                "reviewed_commit": "deadbeefdeadbeef"})
        gate = dict(con.execute("SELECT * FROM gates WHERE id=?", (gid,)).fetchone())
    finally:
        con.close()
    assert gate["review_status"] == "INVALID_RESULT" and gate["verdict"] is None
    assert _findings(env) == []
