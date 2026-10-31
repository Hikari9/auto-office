"""Review-only recovery (T8): a review that returned no verdict (UNAVAILABLE, ATTENTION, INVALID_RESULT)
re-runs only the review of the same immutable revision. No executor starts, no substantive round is spent,
a pinned reviewer stays pinned unless `office review --as` re-pins it, and the orchestrator's own review
is recorded degraded and non-independent, never as a waiver.

v3.1 tests pin the v3.1 review contract (task reviews); the rest run the default convergence-v1 (lane reviews).
Reviewers are scripted fake harnesses run in process; no real agent runs.
"""
from __future__ import annotations

import json
import re

import pytest

from conftest import GOOD_ADD, approved_run, task_row
from test_convergence_contract import (APPROVED, PLAN_VISUAL, _fake_capture, _gates, _q, _scope, _start, _status, finding,
                                       recheck)

V31 = pytest.mark.review_contract("v3.1")
PASS = "VERDICT: PASS"
EXECUTOR = [{"write": {"calc.py": GOOD_ADD}, "submit": True}]
GARBAGE = {"reply": "looks fine to me"}  # headless and unreadable: ATTENTION (v3.1) / INVALID_RESULT (convergence)


def _executor_calls(env):
    return [c for c in env.calls() if c["role"] == "executor"]


def _reviewer(env):
    """A reviewer route that is not the producer's model family (a pin may also be the producer's)."""
    producer = env.con().execute("SELECT d.model FROM revisions r JOIN dispatches d ON d.id=r.dispatch_id").fetchone()[0]
    return "codex/gpt-6-luna@xhigh" if "claude" in producer else "claude/claude-opus-5-5@high"


def _blocked(env, review):
    approved_run(env, executor=EXECUTOR, code_reviewer=[review])
    env.office("dispatch", "T1", check=0)
    t = task_row(env)
    assert t["status"] == "blocked", t
    return t


def _code_gates(env):
    return _q(env, "SELECT * FROM gates WHERE kind='code_review' ORDER BY created_at")


def _triples(env, role="code_reviewer"):
    """The route of every reviewer dispatch, in launch order, without the harness build number (codex@1/...)."""
    return [re.sub(r"^(\w+)@\d+/", r"\1/", r["triple"])
            for r in _q(env, "SELECT triple FROM dispatches WHERE role=? ORDER BY started_at", (role,))]


def _last_triple(env, role="code_reviewer"):
    return _q(env, "SELECT triple FROM dispatches WHERE role=? ORDER BY started_at DESC LIMIT 1", (role,))[0]["triple"]


def _pin(env, tid="T1"):
    raw = task_row(env, tid)["review_override_json"]
    return json.loads(raw) if raw else None


# ------------------------------------------------------------------ v3.1 task reviews

@V31
@pytest.mark.parametrize("review, shown", [({"reply": "", "exit": 1}, "UNAVAILABLE"), (GARBAGE, "ATTENTION")])
def test_review_only_reruns_the_review_of_the_same_revision_for_every_unreviewed_kind(env, review, shown):
    t = _blocked(env, review)
    assert _code_gates(env)[0]["verdict"] == shown
    executors = _executor_calls(env)
    reviewer = _reviewer(env)
    env.script(code_reviewer=[{"reply": PASS}])
    code, out = env.office("review", "T1", "--as", reviewer)
    assert code == 0 and "no executor launched" in out and reviewer in out, out
    after = task_row(env)
    assert after["status"] == "accepted" and after["current_revision_id"] == t["current_revision_id"]
    assert after["current_dispatch_id"] == t["current_dispatch_id"]
    assert _executor_calls(env) == executors
    assert [g["verdict"] for g in _code_gates(env)] == [shown, "PASS"]
    assert {g["revision_id"] for g in _code_gates(env)} == {t["current_revision_id"]}
    assert _pin(env)["as"] == reviewer and _pin(env)["by"] == "user"
    assert _triples(env)[-1] == reviewer, _triples(env)  # the review ran on the route that was named


@V31
def test_no_unavailable_or_attention_attempt_spends_a_round(env):
    _blocked(env, {"reply": "", "exit": 1})
    env.script(code_reviewer=[GARBAGE])
    env.office("review", "T1", "--as", _reviewer(env), check=0)
    assert task_row(env)["status"] == "blocked"
    env.script(code_reviewer=[{"reply": "VERDICT: CHANGES_REQUIRED\nFINDING F1 | material | calc.py:1 | add is wrong | fix it"}])
    env.office("review", "T1", "--as", _reviewer(env), check=0)
    rounds = [(g["verdict"], g["round"]) for g in _code_gates(env)]
    assert rounds == [("UNAVAILABLE", 1), ("ATTENTION", 1), ("CHANGES_REQUIRED", 1)], rounds


@V31
def test_review_only_without_as_starts_on_a_route_the_earlier_attempts_did_not_use(env):
    _blocked(env, {"reply": "", "exit": 1})
    tried = {r["triple"] for r in _q(env, "SELECT triple FROM dispatches WHERE role='code_reviewer'")}
    assert tried
    env.script(code_reviewer=[{"reply": PASS}])
    code, out = env.office("review", "T1")
    assert code == 0 and "next fallback reviewer route" in out, out
    payloads = [json.loads(r["payload_json"]) for r in _q(env, "SELECT payload_json FROM outbox WHERE kind='review'")]
    assert set(payloads[-1]["exclude_routes"]) == tried, payloads
    assert _pin(env) is None
    assert _last_triple(env) not in tried and task_row(env)["status"] == "accepted", _triples(env)


@V31
def test_review_only_keeps_a_pinned_reviewer_until_as_re_pins_it(env):
    approved_run(env, executor=EXECUTOR, code_reviewer=[{"reply": "", "exit": 1}])
    env.office("dispatch", "T1", check=0)
    first = _reviewer(env)
    # the user pinned this reviewer at dispatch; it is unavailable
    con = env.con()
    con.execute("UPDATE tasks SET review_override_json=? WHERE id='T1'",
                (json.dumps({"as": first, "cli": None, "external": False, "by": "user"}),))
    con.commit()
    env.script(code_reviewer=[GARBAGE])
    code, out = env.office("review", "T1")
    assert code == 0 and "pinned by the user and kept" in out, out
    assert _pin(env)["as"] == first and _triples(env)[-1] == first
    assert "exclude_routes" not in json.loads(_q(env, "SELECT payload_json FROM outbox WHERE kind='review'")[-1]["payload_json"])
    other = "claude/claude-opus-5-5@high" if first.startswith("codex") else "codex/gpt-6-luna@xhigh"
    env.script(code_reviewer=[{"reply": PASS}])
    env.office("review", "T1", "--as", other, check=0)
    assert _pin(env)["as"] == other and _triples(env)[-1] == other and task_row(env)["status"] == "accepted"


@V31
def test_review_only_refuses_a_task_that_is_not_blocked_on_its_review(env):
    approved_run(env, executor=EXECUTOR, code_reviewer=[{"reply": PASS}])
    env.office("dispatch", "T1", check=0)
    code, out = env.office("review", "T1", "--as", _reviewer(env))
    assert code == 4 and "nothing-to-review" in out, out
    code, out = env.office("review", "T9")
    assert code == 2 and "bad-review-target" in out, out


@V31
def test_dispatch_of_a_review_only_block_never_launches_an_executor_and_points_to_review(env):
    _blocked(env, GARBAGE)
    executors = _executor_calls(env)
    for flags in (["--as", "claude/claude-sonnet-5-5@high"], ["--external"], ["--route", "claude/claude-sonnet-5-5@high"]):
        code, out = env.office("dispatch", "T1", *flags)
        assert code == 4 and "review-only-block" in out and "office review T1" in out, (flags, out)
    assert _executor_calls(env) == executors
    code, out = env.office("dispatch", "T1")
    assert code == 0 and "no executor launched" in out and "office review T1 --as" in out, out
    assert _executor_calls(env) == executors


@V31
@pytest.mark.parametrize("flags", [[], ["--fresh"], ["--resume"]])
def test_rerun_review_as_on_a_review_only_block_reruns_the_review_only(env, flags):
    t = _blocked(env, GARBAGE)
    executors = _executor_calls(env)
    env.script(code_reviewer=[{"reply": PASS}])
    code, out = env.office("rerun", "T1", *flags, "--review-as", _reviewer(env))
    assert code == 0 and "no executor launched" in out, out
    assert (("ignored" in out) == bool(flags)), out
    after = task_row(env)
    assert after["status"] == "accepted" and after["current_revision_id"] == t["current_revision_id"]
    assert _executor_calls(env) == executors
    assert _pin(env)["as"] == _reviewer(env) and _triples(env)[-1] == _reviewer(env)


@V31
def test_rerun_fresh_without_review_as_is_still_an_executor_rerun(env):
    _blocked(env, GARBAGE)
    before = len(_executor_calls(env))
    env.script(executor=EXECUTOR, code_reviewer=[{"reply": PASS}])
    code, out = env.office("rerun", "T1", "--fresh")
    assert "code review re-run" not in out and "no executor launched" not in out, out
    assert _pin(env) is None and len(_executor_calls(env)) > before


@V31
def test_rerun_without_review_as_still_needs_a_mode(env):
    _blocked(env, GARBAGE)
    code, out = env.office("rerun", "T1")
    assert code == 2 and "rerun-mode" in out, out


@V31
@pytest.mark.parametrize("review", [{"reply": "", "exit": 1}, GARBAGE])
def test_resume_retries_an_unavailable_or_attention_task_review_on_the_same_revision(env, review):
    t = _blocked(env, review)
    executors = _executor_calls(env)
    env.script(code_reviewer=[{"reply": PASS}])
    env.office("resume", check=0)
    after = task_row(env)
    assert after["status"] == "accepted" and after["current_revision_id"] == t["current_revision_id"]
    assert _executor_calls(env) == executors
    assert [g["round"] for g in _code_gates(env)] == [1, 1]
    assert _pin(env) is None  # resume re-pins nothing


@V31
def test_the_unavailable_next_text_names_the_fallback_reviewer_and_the_orchestrator_before_any_waiver(env):
    _blocked(env, GARBAGE)
    nxt = _status(env)["next"]
    assert nxt.index("office review T1 [--as") < nxt.index("office review T1 --report") < nxt.index("waive"), nxt
    assert "degraded" in nxt and "non-independent" in nxt, nxt


# ------------------------------------------------------------------ v3.1: the orchestrator's own review

def _set_producer_session(env, session):
    con = env.con()
    con.execute("UPDATE dispatches SET session_id=? WHERE role='executor'", (session,))
    con.commit()


@V31
def test_orchestrator_task_review_is_degraded_and_never_a_waiver(env, tmp_path):
    t = _blocked(env, {"reply": "", "exit": 1})
    report = tmp_path / "review.txt"
    report.write_text(PASS)
    _set_producer_session(env, "producer-session")
    code, out = env.office("review", "T1", "--report", str(report),
                           env={"OFFICE_HARNESS": "claude", "OFFICE_SESSION": "orchestrator-session"})
    assert code == 0 and "degraded, non-independent" in out and "not a waiver" in out, out
    gate = _code_gates(env)[-1]
    assert gate["verdict"] == "PASS" and gate["independence"] == "degraded-orchestrator" and gate["round"] == 1
    assert gate["revision_id"] == t["current_revision_id"] and "orchestrator" in gate["route"]
    assert task_row(env)["status"] == "accepted"
    assert not _q(env, "SELECT 1 FROM authorizations WHERE kind='waiver'")
    ev = _q(env, "SELECT meta_json FROM evidence WHERE gate_id=?", (gate["id"],))[0]
    meta = json.loads(ev["meta_json"])
    assert meta["independence"] == "degraded-orchestrator" and meta["revision"] == t["current_revision_id"]
    assert meta["by"] == "claude:orchestrator-session" and meta["replaces_gate"]
    assert _q(env, "SELECT 1 FROM events WHERE kind='review.degraded_fallback'")


@V31
def test_the_producing_session_cannot_review_its_own_task(env, tmp_path):
    _blocked(env, {"reply": "", "exit": 1})
    report = tmp_path / "review.txt"
    report.write_text(PASS)
    _set_producer_session(env, "shared-session")
    code, out = env.office("review", "T1", "--report", str(report),
                           env={"OFFICE_HARNESS": "claude", "OFFICE_SESSION": "shared-session"})
    assert code == 4 and "self-review-prohibited" in out, out
    assert task_row(env)["status"] == "blocked" and len(_code_gates(env)) == 1


@V31
def test_orchestrator_task_review_refuses_a_worker_a_bad_report_and_a_healthy_task(env, tmp_path):
    _blocked(env, {"reply": "", "exit": 1})
    report = tmp_path / "review.txt"
    report.write_text(PASS)
    code, out = env.office("review", "T1", "--report", str(report), env={"OFFICE_DISPATCH_ID": "D123"})
    assert code == 4 and "worker-cannot-review" in out, out
    report.write_text("no verdict here")
    code, out = env.office("review", "T1", "--report", str(report))
    assert code == 4 and "report-invalid" in out, out
    code, out = env.office("review", "T1", "--as", "claude/claude-opus-5-5@high", "--report", str(report))
    assert code == 2 and "invalid-override" in out, out
    assert task_row(env)["status"] == "blocked"


# ------------------------------------------------------------------ convergence-v1 lane reviews

@pytest.mark.parametrize("review, status", [({"exit": 1}, "UNAVAILABLE"), (GARBAGE, "INVALID_RESULT")])
def test_lane_review_only_reruns_the_lane_review_on_the_named_route(env, review, status):
    _start(env, executor=EXECUTOR, convergence_reviewer=[review])
    env.office("dispatch", "T1", check=0)
    assert _gates(env, "convergence_review")[0]["review_status"] == status
    executors = _executor_calls(env)
    reviewer = _reviewer(env)
    env.script(convergence_reviewer=[{"reply": APPROVED}])
    code, out = env.office("review", "L-T1", "--as", reviewer)
    assert code == 0 and "no executor launched" in out and "no round spent" in out and reviewer in out, out
    gates = _gates(env, "convergence_review")
    assert [(g["round"], g["review_status"]) for g in gates] == [(1, status), (1, "COMPLETED")], gates
    assert gates[1]["verdict"] == "APPROVED" and _scope(env, "L-T1")["status"] == "approved"
    assert _executor_calls(env) == executors
    assert _pin(env)["as"] == reviewer and _triples(env)[-1] == reviewer


def test_lane_review_only_without_as_excludes_the_routes_already_tried(env):
    _start(env, executor=EXECUTOR, convergence_reviewer=[{"exit": 1}])
    env.office("dispatch", "T1", check=0)
    tried = {r["triple"] for r in _q(env, "SELECT triple FROM dispatches WHERE role='code_reviewer'")}
    assert tried
    env.script(convergence_reviewer=[{"reply": APPROVED}])
    code, out = env.office("review", "L-T1:convergence")
    assert code == 0 and "next fallback reviewer route" in out, out
    payloads = [json.loads(r["payload_json"]) for r in _q(env, "SELECT payload_json FROM outbox WHERE kind='convergence_review'")]
    assert set(payloads[-1]["exclude_routes"]) == tried, payloads
    assert _pin(env) is None
    assert _last_triple(env) not in tried and _scope(env, "L-T1")["status"] == "approved", _triples(env)


def test_lane_review_only_keeps_a_pinned_reviewer_until_as_re_pins_it(env):
    _start(env, executor=EXECUTOR, convergence_reviewer=[{"exit": 1}])
    env.office("dispatch", "T1", check=0)
    first = _reviewer(env)
    con = env.con()
    con.execute("UPDATE tasks SET review_override_json=? WHERE id='T1'",
                (json.dumps({"as": first, "cli": None, "external": False, "by": "user"}),))
    con.commit()
    env.script(convergence_reviewer=[GARBAGE])
    code, out = env.office("review", "L-T1")
    assert code == 0 and "pinned by the user and kept" in out, out
    assert _pin(env)["as"] == first and _triples(env)[-1] == first
    assert "exclude_routes" not in json.loads(
        _q(env, "SELECT payload_json FROM outbox WHERE kind='convergence_review'")[-1]["payload_json"])
    other = "claude/claude-opus-5-5@high" if first.startswith("codex") else "codex/gpt-6-luna@xhigh"
    env.script(convergence_reviewer=[{"reply": APPROVED}])
    env.office("review", "L-T1", "--as", other, check=0)
    assert _pin(env)["as"] == other and _triples(env)[-1] == other and _scope(env, "L-T1")["status"] == "approved"


def test_lane_review_only_spends_no_round_when_the_rerun_is_a_recheck(env):
    _start(env, executor=EXECUTOR, convergence_reviewer=[GARBAGE])
    env.office("dispatch", "T1", check=0)
    env.script(convergence_reviewer=[{"reply": recheck(finding("F1"))}])
    env.office("review", "L-T1", "--as", _reviewer(env), check=0)
    gates = _gates(env, "convergence_review")
    assert [(g["round"], g["review_status"]) for g in gates] == [(1, "INVALID_RESULT"), (1, "COMPLETED")], gates
    assert int(_scope(env, "L-T1")["round"]) == 2


def test_lane_review_only_refusals(env):
    _start(env, executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    code, out = env.office("review", "L-T1", "--as", _reviewer(env))
    assert code == 4 and "nothing-to-review" in out, out  # approved: nothing returned no verdict
    code, out = env.office("review", "L-T9")
    assert code == 2 and "unknown-scope" in out, out
    code, out = env.office("review", "L-T1:bogus")
    assert code == 2 and "bad-review-target" in out, out
    code, out = env.office("review", "T1", "--as", _reviewer(env))
    assert code == 2 and "bad-review-target" in out and "L-" in out, out
    code, out = env.office("review", "L-T1", "--cli", "echo hi")
    assert code == 2 and "invalid-override" in out, out
    code, out = env.office("review", "L-T1", "--as", _reviewer(env), env={"OFFICE_DISPATCH_ID": "D123"})
    assert code == 4 and "worker-cannot-review" in out, out


def test_lane_next_text_names_the_fallback_reviewer_and_the_orchestrator_before_any_waiver(env):
    _start(env, executor=EXECUTOR, convergence_reviewer=[{"exit": 1}])
    env.office("dispatch", "T1", check=0)
    nxt = _status(env)["next"]
    assert nxt.index("office review L-T1:convergence [--as") < nxt.index("--report") < nxt.index("waiver"), nxt


# ------------------------------------------------------------------ guards, validation and double runs

MANUAL = {"OFFICE_JOBS": "manual"}  # queue the review, do not run it


@V31
def test_the_same_review_only_command_twice_queues_one_review(env):
    _blocked(env, GARBAGE)
    code, out = env.office("review", "T1", "--as", _reviewer(env), env=MANUAL)
    assert code == 0, out
    code, out = env.office("review", "T1", "--as", _reviewer(env), env=MANUAL)
    assert code == 4 and "nothing-to-review" in out, out
    assert len(_code_gates(env)) == 2


def test_the_same_lane_review_only_command_twice_queues_one_review(env):
    _start(env, executor=EXECUTOR, convergence_reviewer=[{"exit": 1}])
    env.office("dispatch", "T1", check=0)
    code, out = env.office("review", "L-T1", "--as", _reviewer(env), env=MANUAL)
    assert code == 0, out
    code, out = env.office("review", "L-T1", "--as", _reviewer(env), env=MANUAL)
    assert code == 4 and "nothing-to-review" in out, out
    assert len(_gates(env, "convergence_review")) == 2


@V31
def test_review_only_validates_the_route_before_it_pins_anything(env):
    _blocked(env, GARBAGE)
    for flags in (["--as", "not-a-route"], ["--cli", "echo hi"], ["--external"],
                  ["--as", _reviewer(env), "--cli", "echo hi", "--external"]):
        code, out = env.office("review", "T1", *flags)
        assert code == 2, (flags, out)
    assert _pin(env) is None and len(_code_gates(env)) == 1
    env.office("review", "T1", "--as", _reviewer(env), "--cli", "echo hi", env=MANUAL, check=0)
    assert _pin(env)["cli"] == "echo hi" and not _pin(env)["external"]
    _blocked_again = _q(env, "SELECT 1 FROM gates WHERE kind='code_review' AND status='queued'")
    assert _blocked_again


@V31
def test_review_only_pins_an_external_reviewer(env):
    _blocked(env, GARBAGE)
    env.office("review", "T1", "--as", _reviewer(env), "--external", env=MANUAL, check=0)
    assert _pin(env)["external"] is True


@V31
def test_review_only_refuses_a_terminal_run(env):
    from office import gates, state
    _blocked(env, GARBAGE)
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    with pytest.raises(state.Refused) as err:
        gates.review_only(con, {**run, "phase": "abandoned"}, "T1")
    assert err.value.category == "run-terminal" and len(_code_gates(env)) == 1


def test_a_task_id_under_convergence_points_to_its_lane(env):
    _start(env, executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    report = env.tmp / "r.txt"
    report.write_text(APPROVED)
    code, out = env.office("review", "T1", "--report", str(report))
    assert code == 2 and "bad-review-target" in out and "L-T1:convergence --report" in out, out
    code, out = env.office("review", "l-t1", "--as", _reviewer(env))  # a lane id in any case is a lane
    assert code == 4 and "nothing-to-review" in out, out
    code, out = env.office("review", "T1:convergence")
    assert code == 2 and "bad-review-target" in out, out


@V31
@pytest.mark.parametrize("role_env", [{"OFFICE_ROLE": "reviewer"}, {"OFFICE_DISPATCH_ID": "D123"}])
def test_a_reviewer_or_worker_session_cannot_record_the_orchestrators_review(env, tmp_path, role_env):
    _blocked(env, {"reply": "", "exit": 1})
    report = tmp_path / "review.txt"
    report.write_text(PASS)
    code, out = env.office("review", "T1", "--report", str(report), env=role_env)
    assert code == 4 and "worker-cannot-review" in out, out
    assert task_row(env)["status"] == "blocked" and len(_code_gates(env)) == 1


@pytest.mark.parametrize("role_env", [{"OFFICE_ROLE": "reviewer"}, {"OFFICE_DISPATCH_ID": "D123"}])
def test_a_reviewer_or_worker_session_cannot_review_a_lane_for_the_orchestrator(env, tmp_path, role_env):
    _start(env, executor=EXECUTOR, convergence_reviewer=[{"exit": 1}])
    env.office("dispatch", "T1", check=0)
    report = tmp_path / "review.txt"
    report.write_text(APPROVED)
    code, out = env.office("review", "L-T1:convergence", "--report", str(report), env=role_env)
    assert code == 4 and "worker-cannot-review" in out, out
    assert _scope(env, "L-T1")["status"] == "unavailable"


@V31
def test_the_orchestrator_cannot_review_a_task_that_is_not_blocked_on_its_review(env, tmp_path):
    approved_run(env, executor=EXECUTOR, code_reviewer=[{"reply": PASS}])
    env.office("dispatch", "T1", check=0)
    assert task_row(env)["status"] == "accepted"
    report = tmp_path / "review.txt"
    report.write_text(PASS)
    code, out = env.office("review", "T1", "--report", str(report))
    assert code == 4 and "fallback-not-allowed" in out, out
    assert len(_code_gates(env)) == 1


@V31
@pytest.mark.parametrize("review", [{"reply": "", "exit": 1}, GARBAGE])
def test_an_orchestrator_changes_required_review_is_recorded_degraded_and_does_not_accept(env, tmp_path, review):
    _blocked(env, review)
    report = tmp_path / "review.txt"
    report.write_text("VERDICT: CHANGES_REQUIRED\nFINDING F1 | material | calc.py:1 | add is wrong | fix it")
    env.office("review", "T1", "--report", str(report), check=0)
    gate = _code_gates(env)[-1]
    assert gate["verdict"] == "CHANGES_REQUIRED" and gate["independence"] == "degraded-orchestrator", gate
    assert gate["round"] == 1 and task_row(env)["status"] != "accepted"
    assert not _q(env, "SELECT 1 FROM authorizations WHERE kind='waiver'")


@V31
def test_an_orchestrator_pass_after_an_attention_review_is_degraded(env, tmp_path):
    _blocked(env, GARBAGE)
    report = tmp_path / "review.txt"
    report.write_text(PASS)
    env.office("review", "T1", "--report", str(report), check=0)
    assert _code_gates(env)[-1]["independence"] == "degraded-orchestrator" and task_row(env)["status"] == "accepted"


@V31
def test_resume_re_runs_the_review_on_the_pinned_reviewer_and_excludes_no_route(env):
    _blocked(env, GARBAGE)
    first = _reviewer(env)
    con = env.con()
    con.execute("UPDATE tasks SET review_override_json=? WHERE id='T1'",
                (json.dumps({"as": first, "cli": None, "external": False, "by": "user"}),))
    con.commit()
    env.script(code_reviewer=[{"reply": PASS}])
    env.office("resume", check=0)
    assert _pin(env)["as"] == first and _triples(env)[-1] == first and task_row(env)["status"] == "accepted"
    assert "exclude_routes" not in json.loads(_q(env, "SELECT payload_json FROM outbox WHERE kind='review'")[-1]["payload_json"])


def test_a_lane_review_only_on_the_named_route_never_resumes_the_earlier_reviewer(env):
    # Round 1 completed on route A; round 2's review returned no verdict. --as B must run on B, not resume A.
    adds = [GOOD_ADD + f"# r{i}\n" for i in range(3)]
    _start(env, executor=[{"write": {"calc.py": a}, "submit": True} for a in adds],
           convergence_reviewer=[{"reply": recheck(finding("F1"))}, {"exit": 1}])
    env.office("dispatch", "T1", check=0)
    env.office("rerun", "T1", "--fresh", check=0)
    assert [g["review_status"] for g in _gates(env, "convergence_review")][:1] == ["COMPLETED"]
    first = _triples(env)[0]
    other = "claude/claude-opus-5-5@high" if first.startswith("codex") else "codex/gpt-6-luna@xhigh"
    assert _gates(env, "convergence_review")[-1]["review_status"] == "UNAVAILABLE"
    env.script(convergence_reviewer=[{"reply": APPROVED}])
    env.office("review", "L-T1", "--as", other, check=0)
    assert _triples(env)[-1] == other, _triples(env)


def test_a_visual_review_only_takes_the_next_fallback_route_and_never_a_user_pin(env, monkeypatch, tmp_path):
    _fake_capture(monkeypatch, tmp_path)
    _start(env, plan=PLAN_VISUAL, executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}],
           visual_reviewer=[{"exit": 1}], probe=[{"reply": "auto"}])
    env.office("dispatch", "T1", check=0)
    assert _gates(env, "visual")[0]["review_status"] == "UNAVAILABLE"
    tried = {r["triple"] for r in _q(env, "SELECT triple FROM dispatches WHERE role='visual_reviewer'")}
    code, out = env.office("review", "L-T1:visual", "--as", _reviewer(env))
    assert code == 4 and "no-visual-override" in out and _pin(env) is None, out
    env.script(visual_reviewer=[{"reply": "EVIDENCE_STATUS: COMPARABLE\n" + APPROVED}])
    code, out = env.office("review", "L-T1:visual", env=MANUAL)
    assert code == 0 and "visual review re-run" in out and "next fallback reviewer route" in out, out
    assert _pin(env) is None
    payload = json.loads(_q(env, "SELECT payload_json FROM outbox WHERE kind='lane_visual'")[-1]["payload_json"])
    assert set(payload["exclude_routes"]) == tried and "review_override" not in payload
    assert [g["kind"] for g in _gates(env, "visual")] == ["visual", "visual"] and len(_gates(env, "convergence_review")) == 1
    env.office("resume", check=0)  # the queued re-run starts as the visual reviewer
    assert _scope(env, "L-T1")["status"] == "approved", _triples(env, "visual_reviewer")
    # Since #912 every eligible vision route qualifies, not just the first four, so the re-run starts away
    # from every route the failed round tried and takes the next fallback instead.
    assert _last_triple(env, "visual_reviewer") not in tried, _triples(env, "visual_reviewer")


@V31
def test_review_only_refuses_a_route_that_cannot_review_before_it_pins_anything(env):
    _blocked(env, GARBAGE)
    code, out = env.office("review", "T1", "--as", "pi/some-model@high")  # a builder-only harness
    assert code == 4 and "capability-missing" in out, out
    assert _pin(env) is None and len(_code_gates(env)) == 1


def test_a_lane_review_only_refuses_a_route_that_cannot_review_before_it_pins_anything(env):
    _start(env, executor=EXECUTOR, convergence_reviewer=[{"exit": 1}])
    env.office("dispatch", "T1", check=0)
    code, out = env.office("review", "L-T1", "--as", "pi/some-model@high")
    assert code == 4 and "capability-missing" in out, out
    assert _pin(env) is None and len(_gates(env, "convergence_review")) == 1


def test_a_lane_id_in_any_case_records_the_orchestrators_review(env, tmp_path):
    _start(env, executor=EXECUTOR, convergence_reviewer=[{"exit": 1}])
    env.office("dispatch", "T1", check=0)
    report = tmp_path / "r.txt"
    report.write_text(APPROVED)
    env.office("review", "l-t1:convergence", "--report", str(report), check=0)
    assert _scope(env, "L-T1")["status"] == "approved"


@V31
def test_review_only_reuses_a_tried_route_only_when_no_other_route_qualifies(env, monkeypatch):
    from office import candidates
    _blocked(env, {"reply": "", "exit": 1})
    real, calls = candidates.route_role, []

    def only_the_tried_route(con, config, run, role, **kw):
        calls.append(sorted(kw.get("exclude") or []))
        if kw.get("exclude"):  # every other route is out: the preference cannot be met
            return {"status": "no_qualifying_route", "skipped": []}
        return real(con, config, run, role, **kw)

    monkeypatch.setattr(candidates, "route_role", only_the_tried_route)
    env.script(code_reviewer=[{"reply": PASS}])
    env.office("review", "T1", check=0)
    assert task_row(env)["status"] == "accepted", calls
    assert any(calls) and calls[-1] == [], calls  # tried away from the earlier route, then fell back to it


def _hint(env, gate_id):
    from office import gates
    con = env.con()
    try:
        return gates._review_only_hint(con, {"gate_id": gate_id})
    finally:
        con.close()


@V31
def test_the_reprompt_failure_hint_names_the_task_to_re_review(env):
    _blocked(env, GARBAGE)
    assert _hint(env, _code_gates(env)[0]["id"]).startswith("office review T1 --as <harness>/<model>")
    assert _hint(env, None) == "office resume"


def test_the_reprompt_failure_hint_names_the_lane_gate_to_re_review(env):
    _start(env, executor=EXECUTOR, convergence_reviewer=[GARBAGE])
    env.office("dispatch", "T1", check=0)
    gid = _gates(env, "convergence_review")[0]["id"]
    assert _hint(env, gid).startswith("office review L-T1:convergence --as <harness>/<model>")


def _round_two_unavailable(env):
    """Round 1 completed (RECHECK) on route A; the round-2 review of the repaired lane returned no verdict."""
    adds = [GOOD_ADD + f"# r{i}\n" for i in range(3)]
    _start(env, executor=[{"write": {"calc.py": a}, "submit": True} for a in adds],
           convergence_reviewer=[{"reply": recheck(finding("F1"))}, {"exit": 1}])
    env.office("dispatch", "T1", check=0)
    env.office("rerun", "T1", "--fresh", check=0)
    assert [g["review_status"] for g in _gates(env, "convergence_review")][0] == "COMPLETED"
    assert _gates(env, "convergence_review")[-1]["review_status"] == "UNAVAILABLE"


def _spy_resume(monkeypatch):
    from office import gates
    real, seen = gates.run_reviewer, []

    def spy(con, run, gate, role, brief, **kw):
        seen.append(kw.get("resume_from"))
        return real(con, run, gate, role, brief, **kw)

    monkeypatch.setattr(gates, "run_reviewer", spy)
    return seen


def test_a_lane_review_only_without_as_never_resumes_the_earlier_reviewer(env, monkeypatch):
    _round_two_unavailable(env)
    seen = _spy_resume(monkeypatch)
    env.script(convergence_reviewer=[{"reply": APPROVED}])
    env.office("review", "L-T1", check=0)
    assert seen == [None], seen
    assert _scope(env, "L-T1")["status"] == "approved"


def test_a_pinned_reviewer_that_is_unchanged_keeps_resuming_its_session_across_rounds(env, monkeypatch):
    adds = [GOOD_ADD + f"# r{i}\n" for i in range(3)]
    _start(env, executor=[{"write": {"calc.py": a}, "submit": True} for a in adds],
           convergence_reviewer=[{"reply": recheck(finding("F1"))}])
    env.office("dispatch", "T1", "--review-as", _reviewer_name(), check=0)
    seen = _spy_resume(monkeypatch)
    env.office("rerun", "T1", "--fresh", check=0)
    assert seen and seen[-1] is not None, seen  # a normal pinned round resumes; only a review-only re-run does not


def test_a_re_pin_to_another_route_is_honored_over_resuming_the_earlier_reviewer(env):
    adds = [GOOD_ADD + f"# r{i}\n" for i in range(3)]
    _start(env, executor=[{"write": {"calc.py": a}, "submit": True} for a in adds],
           convergence_reviewer=[{"reply": recheck(finding("F1"))}, {"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    first = _triples(env)[0]
    other = "claude/claude-opus-5-5@high" if first.startswith("codex") else "codex/gpt-6-luna@xhigh"
    env.office("rerun", "T1", "--fresh", "--review-as", other, check=0)
    assert _triples(env)[-1] == other, _triples(env)


def test_a_pinned_lane_review_only_rerun_never_resumes_the_earlier_reviewer(env, monkeypatch):
    _round_two_unavailable(env)
    first = _triples(env)[0]
    con = env.con()
    con.execute("UPDATE tasks SET review_override_json=? WHERE id='T1'",
                (json.dumps({"as": first, "cli": None, "external": False, "by": "user"}),))
    con.commit()
    seen = _spy_resume(monkeypatch)
    env.script(convergence_reviewer=[{"reply": APPROVED}])
    env.office("review", "L-T1", check=0)
    assert seen == [None], seen
    assert _pin(env)["as"] == first


@V31
@pytest.mark.parametrize("stale", [True, False])
def test_every_review_only_path_refuses_a_terminal_run(env, tmp_path, stale):
    """`stale`: the caller's run dict still says active while the database already says it ended."""
    from office import convergence, gates, state
    approved_run(env, executor=EXECUTOR, code_reviewer=[{"reply": "", "exit": 1}])
    env.office("dispatch", "T1", check=0)
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    con.execute("UPDATE runs SET phase='abandoned'")
    con.commit()
    caller = run if stale else {**run, "phase": "abandoned"}
    report = tmp_path / "r.txt"
    report.write_text(PASS)
    for call in (lambda: gates.review_only(con, caller, "T1"),
                 lambda: gates.orchestrator_task_review(con, caller, "T1", report)):
        with pytest.raises(state.Refused) as err:
            call()
        assert err.value.category == "run-terminal"
    assert len(_code_gates(env)) == 1 and task_row(env)["status"] == "blocked"


def test_a_lane_fallback_and_review_only_refuse_a_terminal_run(env, tmp_path):
    from office import convergence, gates, state
    _start(env, executor=EXECUTOR, convergence_reviewer=[{"exit": 1}])
    env.office("dispatch", "T1", check=0)
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    con.execute("UPDATE runs SET phase='abandoned'")
    con.commit()
    report = tmp_path / "r.txt"
    report.write_text(APPROVED)
    for call in (lambda: convergence.fallback_review(con, run, "L-T1:convergence", report, inspected=[]),
                 lambda: gates.review_only(con, run, "L-T1")):
        with pytest.raises(state.Refused) as err:
            call()
        assert err.value.category == "run-terminal"
    assert len(_gates(env, "convergence_review")) == 1 and _scope(env, "L-T1")["status"] == "unavailable"


def test_rerun_review_as_needs_a_mode_when_the_task_is_not_blocked_on_its_review(env):
    approved_run(env)
    code, out = env.office("rerun", "T1", "--review-as", _reviewer_name())
    assert code == 2 and "rerun-mode" in out, out
    assert _pin(env) is None and not _executor_calls(env)


def test_rerun_validates_the_review_route_before_it_pins_anything(env):
    approved_run(env)
    code, out = env.office("rerun", "T1", "--fresh", "--review-as", "pi/some-model@high")
    assert code == 4 and "capability-missing" in out, out
    assert _pin(env) is None


def _reviewer_name():
    return "claude/claude-opus-5-5@high"


# ------------------------------------------------------------------ the re-prompt's pane identity fence

def _reprompt(env, monkeypatch, fence, *, in_fence=(), gate_id=None, pane_id="w1:p7", landed="landed"):
    from office import dispatch, gates, review_parse, state
    monkeypatch.setattr(gates, "_agent_alive", lambda name: True)
    sent = []
    monkeypatch.setattr(dispatch, "submit_prompt",
                        lambda name, text, *, pane=None, guard=None, max_enter_tries=None: sent.append((name, text, pane, bool(in_fence), max_enter_tries)) or landed)
    monkeypatch.setattr(dispatch, "fenced_pane", fence)
    monkeypatch.setenv("OFFICE_REVIEW_REPROMPT_WAIT", "0")
    monkeypatch.setenv("OFFICE_REVIEW_REPROMPT_POLL", "0")
    con = env.con()
    try:
        run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
        run = {**run, "gates": {"review_reprompt_max": 1}}
        d = {"id": "D1", "launcher": "herdr", "pane_id": pane_id, "role": "code_reviewer", "task_id": "T1",
             "harness": "codex", "triple": "codex/test@high", "gate_id": gate_id}
        got = gates._reprompt_until_valid(con, run, d, env.tmp, env.tmp / "reply.txt", review_parse.parse(""),
                                          plan_review=False, visual=False)
        events = [r["summary"] for r in con.execute("SELECT summary FROM events WHERE kind='review.reprompt'")]
    finally:
        con.close()
    return sent, got, events


def test_the_review_reprompt_is_sent_only_inside_the_pane_identity_fence(env, monkeypatch):
    import contextlib
    _start(env, executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])
    entered = []

    inside = []

    @contextlib.contextmanager
    def fence(run, d):
        entered.append(d["id"])
        inside.append(True)
        try:
            yield "FENCED:p9"
        finally:
            inside.clear()

    sent, _, events = _reprompt(env, monkeypatch, fence, in_fence=inside)
    assert entered == ["D1"] and len(sent) == 1 and sent[0][2] == "FENCED:p9" and sent[0][3] is True
    assert sent[0][4] == 1, "codex reviewer recovery must be bounded to 1 Enter"
    assert events and "re-prompted 1/1" in events[0]


def test_a_pane_that_is_not_the_reviewers_is_never_prompted(env, monkeypatch):
    import contextlib
    from office import dispatch
    _start(env, executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])

    @contextlib.contextmanager
    def fence(run, d):
        raise dispatch.PaneMismatch("pane w1:p7 belongs to dispatch D9, not D1")
        yield

    sent, (_, _, reason), events = _reprompt(env, monkeypatch, fence)
    assert sent == []
    assert "belongs to dispatch D9" in reason and "Office did not re-prompt it" in reason and "office resume" in reason, reason
    assert events and "re-prompt refused" in events[0]


def _fenced(run, d):
    import contextlib

    @contextlib.contextmanager
    def fence():
        yield d["pane_id"]
    return fence()


def test_a_reprompt_that_stays_unsubmitted_names_the_review_only_command(env, monkeypatch):
    _start(env, executor=EXECUTOR, convergence_reviewer=[GARBAGE])
    env.office("dispatch", "T1", check=0)
    gid = _gates(env, "convergence_review")[0]["id"]
    sent, (_, _, reason), _ = _reprompt(env, monkeypatch, _fenced, gate_id=gid, landed="held")
    assert len(sent) == 1 and "typed but unsubmitted" in reason, reason
    assert sent[0][4] == 1, "codex reviewer recovery must be bounded to 1 Enter"
    assert "office review L-T1:convergence --as <harness>/<model>" in reason and "waive" not in reason, reason


def test_a_pane_mismatch_names_the_review_only_command_for_its_gate(env, monkeypatch):
    from office import dispatch
    _start(env, executor=EXECUTOR, convergence_reviewer=[GARBAGE])
    env.office("dispatch", "T1", check=0)
    gid = _gates(env, "convergence_review")[0]["id"]

    def refuse(run, d):
        raise dispatch.PaneMismatch("pane w1:p7 belongs to dispatch D9, not D1")

    sent, (_, _, reason), _ = _reprompt(env, monkeypatch, refuse, gate_id=gid)
    assert sent == [] and "office review L-T1:convergence --as <harness>/<model>" in reason, reason


@pytest.mark.parametrize("snapshot", [
    ({}, {}),
    ({"agent": "claude"}, {}),
    ({"agent": {}}, {}),
    ({"agent": "claude"}, {"name": "someone-else", "pane_id": "w1:p7", "agent": "claude"}),
    ({"agent": "claude"}, {"name": "office-D1", "pane_id": "w1:p9", "agent": "claude"}),
], ids=["empty", "kind-only", "empty-agent", "other-name", "other-pane"])
def test_an_empty_or_partial_herdr_report_never_receives_the_review_reprompt(env, monkeypatch, snapshot):
    from office import dispatch
    monkeypatch.setattr(dispatch, "pane_snapshot", lambda *a, **k: snapshot)
    monkeypatch.setattr(dispatch, "herdr_agent_name", lambda _id: "office-D1")
    _start(env, executor=EXECUTOR, convergence_reviewer=[GARBAGE])
    env.office("dispatch", "T1", check=0)
    gid = _gates(env, "convergence_review")[0]["id"]
    sent, (_, _, reason), events = _reprompt(env, monkeypatch, dispatch.fenced_pane, gate_id=gid)
    assert sent == [] and "Office did not re-prompt it" in reason, reason
    assert "office review L-T1:convergence --as <harness>/<model>" in reason, reason
    assert events and "re-prompt refused" in events[0]


def test_a_reviewer_with_no_recorded_pane_is_not_prompted(env, monkeypatch):
    entered = []

    def fence(run, d):
        entered.append(d)
        return _fenced(run, d)

    _start(env, executor=EXECUTOR, convergence_reviewer=[GARBAGE])
    sent, _, _ = _reprompt(env, monkeypatch, fence, pane_id=None)
    assert sent == [] and entered == []
