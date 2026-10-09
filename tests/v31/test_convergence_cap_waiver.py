"""#423: specialist-first independence, a configurable round cap, and the
orchestrator's cap waiver, end to end through the real CLI. Reviewers are
scripted fake harnesses run in process; no real agent runs."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import GOOD_ADD
from test_convergence_contract import (APPROVED, _gates, _q, _run_row, _scope, _start, _status, finding, recheck)

REASON = "F1 is a comment-level wording gap in calc.add; the risk is cosmetic and tracked as a follow-up"


def _spent(env, extra=()):
    """A lane whose reviewer answers RECHECK until the round cap."""
    adds = [GOOD_ADD + f"# r{i}\n" for i in range(6)]
    _start(env, extra=extra, executor=[{"write": {"calc.py": a}, "submit": True} for a in adds],
           convergence_reviewer=[{"reply": recheck(finding("F1"))}])
    env.office("dispatch", "T1", check=0)


def _reach_cap(env, cap=3):
    for _ in range(cap - 1):
        env.office("rerun", "T1", "--fresh", check=0)
    assert _scope(env, "L-T1")["status"] == "escalated"


def _gates_json(env):
    return _run_row(env)["gates"]


def _set_producer_session(env, session):
    con = env.con()
    con.execute("UPDATE dispatches SET session_id=? WHERE role='executor'", (session,))
    con.commit()


def _unpin(env):
    """A convergence-v1 run started before #423: no pinned cap."""
    con = env.con()
    row = con.execute("SELECT id, gates_json FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()
    gates = json.loads(row["gates_json"])
    gates.pop("convergence_max_rounds")
    con.execute("UPDATE runs SET gates_json=? WHERE id=?", (json.dumps(gates), row["id"]))
    con.commit()


# ------------------------------------------------------------------ specialist first, independence

def test_specialist_reviewer_is_tried_first_and_orchestrator_is_not_used(env):
    _start(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    gate = _gates(env, "convergence_review")[0]
    assert gate["independence"] == "independent" and gate["reviewer_dispatch_id"]
    assert not _q(env, "SELECT 1 FROM gates WHERE independence='independent-orchestrator'")


def test_unavailable_specialists_allow_an_independent_orchestrator_fallback(env, tmp_path):
    _start(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], convergence_reviewer=[{"exit": 1}])
    env.office("dispatch", "T1", check=0)
    assert _scope(env, "L-T1")["fallback_available"]
    report = tmp_path / "r.txt"
    report.write_text(APPROVED)
    _set_producer_session(env, "producer-session")
    env.office("review", "L-T1:convergence", "--report", str(report),
               env={"OFFICE_HARNESS": "claude", "OFFICE_SESSION": "orchestrator-session"}, check=0)
    assert _gates(env, "convergence_review")[-1]["independence"] == "independent-orchestrator"
    assert _scope(env, "L-T1")["status"] == "approved"


def test_the_producing_session_cannot_review_its_own_work(env, tmp_path):
    _start(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], convergence_reviewer=[{"exit": 1}])
    env.office("dispatch", "T1", check=0)
    report = tmp_path / "r.txt"
    report.write_text(APPROVED)
    _set_producer_session(env, "shared-session")
    code, out = env.office("review", "L-T1:convergence", "--report", str(report),
                           env={"OFFICE_HARNESS": "claude", "OFFICE_SESSION": "shared-session"})
    assert code == 4 and "self-review-prohibited" in out, out
    assert _gates(env, "convergence_review")[-1]["verdict"] is None
    assert _scope(env, "L-T1")["status"] == "unavailable"


def test_a_pre_423_convergence_run_keeps_the_degraded_fallback_record(env, tmp_path):
    _start(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], convergence_reviewer=[{"exit": 1}])
    _unpin(env)
    env.office("dispatch", "T1", check=0)
    report = tmp_path / "r.txt"
    report.write_text(APPROVED)
    env.office("review", "L-T1:convergence", "--report", str(report), check=0)
    assert _gates(env, "convergence_review")[-1]["independence"] == "degraded-orchestrator"


# ------------------------------------------------------------------ the cap

def test_default_cap_is_three_and_pinned_in_the_run(env):
    _start(env)
    assert _gates_json(env)["convergence_max_rounds"] == 3


def test_cap_is_configurable_by_flag_and_config_and_validated(env):
    _start(env, extra=("--review-rounds", "2"))
    assert _gates_json(env)["convergence_max_rounds"] == 2
    code, out = env.office("start", "other", "--planner", "inline", "--review-rounds", "11")
    assert code == 2 and "bad-rounds" in out or "1 to 10" in out, out
    Path(env.tmp / "user-config.yaml").write_text("review:\n  max_rounds: 4\n")
    env.office("start", "third", "--gear", "direct+review", "--planner", "inline", check=0)
    assert _gates_json(env)["convergence_max_rounds"] == 4


def test_cap_of_two_stops_after_two_rounds_and_survives_resume(env):
    _spent(env, extra=("--review-rounds", "2"))
    _reach_cap(env, 2)
    assert len(_gates(env, "convergence_review")) == 2
    env.office("resume", check=0)
    assert _scope(env, "L-T1")["status"] == "escalated" and len(_gates(env, "convergence_review")) == 2
    assert _gates_json(env)["convergence_max_rounds"] == 2


def test_transport_failures_do_not_spend_a_round_toward_the_cap(env):
    _start(env, executor=[{"write": {"calc.py": GOOD_ADD + f"# r{i}\n"}, "submit": True} for i in range(4)],
           convergence_reviewer=[{"exit": 1}, {"exit": 1}, {"reply": recheck(finding("F1"))}])
    env.office("dispatch", "T1", check=0)
    assert _scope(env, "L-T1")["status"] == "recheck" and _scope(env, "L-T1")["round"] == 2


# ------------------------------------------------------------------ waive or escalate at the cap

def test_at_the_cap_next_offers_waive_or_escalate(env):
    _spent(env)
    _reach_cap(env)
    nxt = _status(env)["next"]
    assert "office waive L-T1 --reason" in nxt and "office decide L-T1" in nxt and "does not stay blocked" in nxt, nxt


def test_waiver_needs_a_substantive_reason(env):
    _spent(env)
    _reach_cap(env)
    for bad in ("", "ok", "ship it", "because I said so", "accepted risk, fine."):
        code, out = env.office("waive", "L-T1", "--reason", bad) if bad else env.office("waive", "L-T1")
        assert code == 2 and "waiver-reason-required" in out, (bad, out)
    assert _scope(env, "L-T1")["status"] == "escalated"


def test_waiver_only_at_the_cap_and_never_by_a_worker(env):
    _spent(env)
    code, out = env.office("waive", "L-T1", "--reason", REASON)
    assert code == 4 and "round-cap-not-reached" in out, out
    _reach_cap(env)
    code, out = env.office("waive", "L-T1", "--reason", REASON, env={"OFFICE_DISPATCH_ID": "D1"})
    assert code == 4 and "worker-cannot-waive" in out, out


def test_waiver_keeps_the_verdict_and_records_scope_findings_and_session(env):
    _spent(env)
    _reach_cap(env)
    env.office("waive", "L-T1", "--reason", REASON, env={"OFFICE_HARNESS": "claude", "OFFICE_SESSION": "orch-7"}, check=0)
    assert _scope(env, "L-T1")["status"] == "waived"
    assert _gates(env, "convergence_review")[-1]["verdict"] == "RECHECK", "never rewritten to APPROVED"
    assert _run_row(env)["landing"]["integration"]["status"] == "accepted"
    from office import convergence
    con = env.con()
    w = convergence.receipt(con, _run_row(env))["waivers"][0]
    assert w["target"].startswith("L-T1:convergence_review@") and w["commit"] and w["scope"] == "L-T1"
    assert w["reason"] == REASON and w["underlying"] == "RECHECK" and w["cap_waiver"] and w["cap"] == 3
    assert w["unresolved_findings"][0]["code"] == "F1"
    assert w["session"]["primary"] == "claude:orch-7"
    assert w["by"].startswith("orchestrator (round cap reached")
    lines = "\n".join(convergence.waiver_lines(con, _run_row(env)))
    assert "RECHECK stands" in lines or "RECHECK stand" in lines and "F1" in lines


def test_waiver_is_bound_to_the_composed_commit(env):
    _spent(env)
    _reach_cap(env)
    env.office("waive", "L-T1", "--reason", REASON, check=0)
    con = env.con()
    con.execute("UPDATE tasks SET status='changes_required' WHERE id='T1'")
    con.commit()
    env.office("rerun", "T1", "--fresh", check=0)
    assert _scope(env, "L-T1")["status"] != "waived"


def test_escalation_to_the_user_still_works_at_the_cap(env):
    _spent(env)
    _reach_cap(env)
    code, out = env.office("decide", "L-T1", "continue")
    assert code == 2 and "quote" in out
    env.office("decide", "L-T1", "continue", "--quote", "one more try", check=0)
    assert _scope(env, "L-T1")["status"] == "recheck" and _scope(env, "L-T1")["cycle"] == 2


def test_cap_waiver_is_unavailable_to_a_pre_423_run(env):
    _spent(env)
    _unpin(env)
    _reach_cap(env)
    code, out = env.office("waive", "L-T1", "--reason", REASON)
    assert code == 4 and "cap-waiver-unavailable" in out, out
    assert "office waive" not in _status(env)["next"] and "office decide L-T1" in _status(env)["next"]


# ------------------------------------------------------------------ waiver is not landing authority

def test_a_waiver_is_not_landing_authority(env):
    _spent(env)
    _reach_cap(env)
    env.office("waive", "L-T1", "--reason", REASON, check=0)
    from office import convergence
    con = env.con()
    run = _run_row(env)
    assert convergence.landing_authority(con, run, "orchestrator")[0] is False
    assert not _q(env, "SELECT 1 FROM authorizations WHERE kind IN ('merge','deploy')")
    code, out = env.office("land")
    assert "WAIVED" in out and "RECHECK stands" in out and "not landing authority" in out, out
    assert "ask the user" in out, "ask-mode landing still waits for the human"
    code, out = env.office("land", "--merge")
    assert code == 2 and "user-quote-required" in out, out
    assert not _q(env, "SELECT 1 FROM authorizations WHERE kind IN ('merge','deploy')")


def test_landing_authority_given_at_intake_is_kept_after_a_waiver(env):
    _spent(env, extra=("--end-state", "merge"))
    _reach_cap(env)
    env.office("waive", "L-T1", "--reason", REASON, check=0)
    from office import convergence
    assert convergence.landing_authority(env.con(), _run_row(env), "orchestrator")[0] is True
    assert not _q(env, "SELECT 1 FROM authorizations WHERE kind='merge'"), "no merge authority is inferred from the waiver"
