"""Review-only recovery (T8): a review that returned no verdict (UNAVAILABLE, ATTENTION, INVALID_RESULT)
re-runs only the review of the same immutable revision. No executor starts, no substantive round is spent,
a pinned reviewer stays pinned unless `office review --as` re-pins it, and the orchestrator's own review
is recorded degraded and non-independent, never as a waiver.

v3.1 tests pin the v3.1 review contract (task reviews); the rest run the default convergence-v1 (lane reviews).
Reviewers are scripted fake harnesses run in process; no real agent runs.
"""
from __future__ import annotations

import json

import pytest

from conftest import GOOD_ADD, approved_run, task_row
from test_convergence_contract import APPROVED, _gates, _q, _scope, _start, _status, recheck, finding

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
    assert _pin(env)["as"] == first
    assert "exclude_routes" not in json.loads(_q(env, "SELECT payload_json FROM outbox WHERE kind='review'")[-1]["payload_json"])
    other = "claude/claude-opus-5-5@high" if first.startswith("codex") else "codex/gpt-6-luna@xhigh"
    env.script(code_reviewer=[{"reply": PASS}])
    env.office("review", "T1", "--as", other, check=0)
    assert _pin(env)["as"] == other and task_row(env)["status"] == "accepted"


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
    assert _pin(env)["as"] == reviewer


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
    assert _pin(env)["as"] == first
    assert "exclude_routes" not in json.loads(
        _q(env, "SELECT payload_json FROM outbox WHERE kind='convergence_review'")[-1]["payload_json"])
    other = "claude/claude-opus-5-5@high" if first.startswith("codex") else "codex/gpt-6-luna@xhigh"
    env.script(convergence_reviewer=[{"reply": APPROVED}])
    env.office("review", "L-T1", "--as", other, check=0)
    assert _pin(env)["as"] == other and _scope(env, "L-T1")["status"] == "approved"


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


# ------------------------------------------------------------------ the re-prompt's pane identity fence

def _reprompt(env, monkeypatch, fence):
    from office import dispatch, gates, review_parse, state
    monkeypatch.setattr(gates, "_agent_alive", lambda name: True)
    sent = []
    monkeypatch.setattr(dispatch, "submit_prompt", lambda name, text, pane=None: sent.append((name, text, pane)) or "landed")
    monkeypatch.setattr(dispatch, "fenced_pane", fence)
    monkeypatch.setenv("OFFICE_REVIEW_REPROMPT_WAIT", "0")
    monkeypatch.setenv("OFFICE_REVIEW_REPROMPT_POLL", "0")
    con = env.con()
    try:
        run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
        run = {**run, "gates": {"review_reprompt_max": 1}}
        d = {"id": "D1", "launcher": "herdr", "pane_id": "w1:p7", "role": "code_reviewer", "task_id": "T1",
             "triple": "codex/test@high", "gate_id": None}
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

    @contextlib.contextmanager
    def fence(run, d):
        entered.append(d["id"])
        yield "w1:p7"

    sent, _, events = _reprompt(env, monkeypatch, fence)
    assert entered == ["D1"] and len(sent) == 1 and sent[0][2] == "w1:p7"
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
