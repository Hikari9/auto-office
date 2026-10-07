"""T3: a reviewer that cannot finish is rerouted, never waived; the review alone re-runs.

Covers the review-only rerun (`office rerun <task> --review --review-as <route>`), reviewer
pins (a task, a live worker, a lane gate id), quota-stall classification, the orchestrator's
recorded non-independent fallback and its limits, and a check that already fails on the base
(#306) under both review contracts.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import BAD_ADD, GOOD_ADD, PLAN_ONE, approved_run, task_row
from test_convergence_contract import APPROVED, _gates, _q, _run_row, _scope, _start, _status

QUOTA = {"stderr": "You've hit your usage limit. Try again later.\n", "exit": 1}
SILENT = {"reply": "", "exit": 1}
SUBMIT = [{"write": {"calc.py": GOOD_ADD}, "submit": True}]
CODEX = "codex/gpt-6-luna@xhigh"
CLAUDE = "claude/claude-opus-5-5@high"
EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}


def _executors(env):
    return [r["id"] for r in _q(env, "SELECT id FROM dispatches WHERE role='executor' ORDER BY started_at")]


def _calls(env, role):
    return [c for c in env.calls() if c["role"] == role]


def _pin(env, tid="T1"):
    raw = task_row(env, tid)["review_override_json"]
    return json.loads(raw) if raw else None


# ------------------------------------------------------------------ quota stall


def test_a_quota_wall_is_a_quota_stall_and_the_chain_moves_to_the_next_route(env):
    _start(env, executor=SUBMIT, convergence_reviewer=[QUOTA, {"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    gate = _gates(env, "convergence_review")[-1]
    assert gate["verdict"] == "APPROVED" and gate["env_failures"] == 1, gate
    stalled = _q(env, "SELECT * FROM dispatches WHERE role='code_reviewer' ORDER BY started_at")[0]
    assert stalled["outcome"] == "environment_failure" and stalled["stall_kind"] == "usage_limit", stalled


def test_every_route_walled_says_quota_stall_not_an_empty_reply(env):
    _start(env, executor=SUBMIT, convergence_reviewer=[QUOTA])
    env.office("dispatch", "T1", check=0)
    gate = _gates(env, "convergence_review")[-1]
    assert gate["review_status"] == "UNAVAILABLE" and "quota stall" in gate["summary"], gate["summary"]
    assert "no reply" not in gate["summary"] and "empty" not in gate["summary"].lower(), gate["summary"]
    assert _scope(env, "L-T1")["status"] == "unavailable"


def test_a_pinned_reviewer_that_hits_a_wall_names_the_review_only_reroute(env):
    _start(env, executor=SUBMIT, **{"codex:convergence_reviewer": [QUOTA], "claude:convergence_reviewer": [
        {"reply": APPROVED}]}, convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", "--review-as", CODEX, check=0)
    gate = _gates(env, "convergence_review")[-1]
    assert gate["review_status"] == "UNAVAILABLE", gate
    assert "pinned reviewer" in gate["summary"] and "quota wall" in gate["summary"], gate["summary"]
    assert "office rerun L-T1:convergence --review --review-as " in gate["summary"], gate["summary"]
    named = gate["summary"].split("--review --review-as ")[1].split()[0].rstrip(";")
    assert "<harness" not in named and not named.startswith("codex/"), f"a usable route on another harness: {named}"
    assert [c["harness"] for c in _calls(env, "convergence_reviewer")] == ["codex"], "the pin was not substituted"


# ------------------------------------------------------------------ the two options


def test_a_gate_whose_reviewer_cannot_finish_offers_exactly_two_options_and_never_a_waiver(env):
    _start(env, executor=SUBMIT, **{"codex:convergence_reviewer": [QUOTA]}, convergence_reviewer=[QUOTA])
    env.office("dispatch", "T1", "--review-as", CODEX, check=0)
    nxt = _status(env)["next"]
    assert nxt.count("(1)") == 1 and nxt.count("(2)") == 1 and "(3)" not in nxt, nxt
    assert "office rerun L-T1:convergence --review --review-as " in nxt, nxt
    assert "office review L-T1:convergence --report <file>" in nxt, nxt
    assert "non-independent" in nxt and "route orchestrator" in nxt and "revision" in nxt, nxt
    assert "waive" not in nxt.lower(), nxt


def test_with_no_other_route_the_first_option_says_so(env):
    _start(env, executor=SUBMIT, convergence_reviewer=[QUOTA])
    env.office("dispatch", "T1", check=0)
    nxt = _status(env)["next"]
    assert "(1) the next fallback reviewer: none qualifies now" in nxt and "(2) review it yourself" in nxt, nxt
    assert "waive" not in nxt.lower(), nxt


def test_the_next_route_is_the_one_the_runner_could_use_and_it_runs_review_only(env):
    _start(env, executor=SUBMIT, **{"codex:convergence_reviewer": [QUOTA]}, convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", "--review-as", CODEX, check=0)
    nxt = _status(env)["next"]
    route = nxt.split("--review --review-as ")[1].split(";")[0].split()[0]
    before = _executors(env)
    env.office("rerun", "L-T1:convergence", "--review", "--review-as", route, check=0)
    assert _executors(env) == before, "a review-only rerun never creates an executor dispatch"
    assert _calls(env, "convergence_reviewer")[-1]["harness"] == route.split("/")[0], "the named route ran"
    assert _gates(env, "convergence_review")[-1]["verdict"] == "APPROVED"
    assert _scope(env, "L-T1")["status"] == "approved"


# ------------------------------------------------------------------ the orchestrator's review and its limits


def _walled(env, **extra):
    _start(env, executor=SUBMIT, convergence_reviewer=[QUOTA], **extra)
    env.office("dispatch", "T1", check=0)
    assert _scope(env, "L-T1")["status"] == "unavailable"


def test_option_two_names_who_route_and_revision(env):
    _walled(env)
    nxt = _status(env)["next"]
    commit = _scope(env, "L-T1")["commit"][:10]
    second = nxt.split("(2)")[1]
    assert "non-independent fallback by orchestrator on route orchestrator for revision " + commit in second, second


def test_the_orchestrator_review_is_recorded_as_non_independent_with_who_route_and_revision(env, tmp_path):
    _walled(env)
    report = tmp_path / "review.txt"
    report.write_text(APPROVED)
    env.office("review", "L-T1:convergence", "--report", str(report), check=0, env={"OFFICE_HARNESS": "claude",
                                                                                    "OFFICE_SESSION": "s-1"})
    gate = _gates(env, "convergence_review")[-1]
    assert gate["independence"] == "degraded-orchestrator" and gate["verdict"] == "APPROVED", gate
    commit = _scope(env, "L-T1")["commit"][:10]
    assert "claude:s-1" in gate["route"] and "claude:s-1" in gate["summary"] and commit in gate["summary"], gate
    event = _q(env, "SELECT payload_json FROM events WHERE kind='review.degraded_fallback'")[0]
    payload = json.loads(event["payload_json"])
    assert payload["route"] == "orchestrator" and "claude:s-1" in payload["who"] and payload["commit"].startswith(commit)
    assert _scope(env, "L-T1")["status"] == "approved", "recorded as the fallback, it satisfies the gate"
    assert payload["revision"] == gate["revision_id"]
    from office import convergence, state
    con = env.con()
    receipt = convergence.receipt(con, state.get_run(con, _q(env, "SELECT id FROM runs")[0]["id"]))
    assert receipt["degraded"] == ["L-T1:convergence_review"]
    recorded = receipt["scopes"][0]["reviews"][-1]["recorded_as"]
    assert "orchestrator" in recorded and "non-independent" in recorded and "claude:s-1" in recorded, recorded


def test_the_orchestrator_review_is_refused_while_the_reviewer_has_a_verdict(env, tmp_path):
    _start(env, executor=SUBMIT, convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    report = tmp_path / "review.txt"
    report.write_text(APPROVED)
    code, out = env.office("review", "L-T1:convergence", "--report", str(report))
    assert code != 0 and "fallback-not-allowed" in out and "could not finish" in out, out
    assert _gates(env, "convergence_review")[-1]["independence"] != "degraded-orchestrator"


def test_the_orchestrator_review_is_refused_for_work_the_orchestrator_produced(env, tmp_path):
    _walled(env)
    con = env.con()
    con.execute("UPDATE dispatches SET launcher='external' WHERE role='executor'")
    con.commit()
    report = tmp_path / "review.txt"
    report.write_text(APPROVED)
    code, out = env.office("review", "L-T1:convergence", "--report", str(report))
    assert code != 0 and "orchestrator-produced-work" in out, out
    assert _scope(env, "L-T1")["status"] == "unavailable"
    nxt = _status(env)["next"]
    assert "(2) your own review is not allowed" in nxt and "waive" not in nxt.lower(), nxt


def test_the_orchestrator_review_never_comes_from_a_worker(env, tmp_path):
    _walled(env)
    report = tmp_path / "review.txt"
    report.write_text(APPROVED)
    code, out = env.office("review", "L-T1:convergence", "--report", str(report), env={"OFFICE_DISPATCH_ID": "D1"})
    assert code != 0 and "worker-cannot-review" in out, out
    # An agent that dropped its environment still stands in its task worktree.
    worktree = Path(_q(env, "SELECT worktree FROM dispatches WHERE role='executor'")[0]["worktree"])
    con = env.con()
    con.execute("UPDATE dispatches SET status='running', ended_at=NULL WHERE role='executor'")
    con.execute("UPDATE tasks SET current_dispatch_id=(SELECT id FROM dispatches WHERE role='executor') WHERE id='T1'")
    con.commit()
    code, out = env.office("review", "L-T1:convergence", "--report", str(report), cwd=worktree)
    assert code != 0 and "worker-cannot-review" in out, out
    assert _scope(env, "L-T1")["status"] == "unavailable"


@pytest.mark.parametrize("status", ["running", "EVIDENCE_BLOCKED"])
def test_the_orchestrator_review_is_refused_while_the_review_is_running_or_blocked_on_evidence(env, tmp_path, status):
    _walled(env)
    con = env.con()
    if status == "running":
        con.execute("UPDATE gates SET status='running', verdict=NULL WHERE kind='convergence_review'")
    else:
        con.execute("UPDATE gates SET review_status='EVIDENCE_BLOCKED' WHERE kind='convergence_review'")
    con.commit()
    report = tmp_path / "review.txt"
    report.write_text(APPROVED)
    code, out = env.office("review", "L-T1:convergence", "--report", str(report))
    assert code != 0 and "fallback-not-allowed" in out, out
    assert not [g for g in _gates(env, "convergence_review") if g["independence"] == "degraded-orchestrator"]


@pytest.mark.review_contract("v3.1")
def test_v31_orchestrator_review_satisfies_the_gate_only_as_a_recorded_fallback(env, tmp_path):
    approved_run(env, executor=SUBMIT, code_reviewer=[SILENT])
    env.office("dispatch", "T1", check=0)
    assert task_row(env)["status"] == "blocked"
    nxt = _status(env)["next"]
    assert nxt.count("(1)") == 1 and nxt.count("(2)") == 1 and "waive" not in nxt.lower(), nxt
    assert "office review T1 --report <file>" in nxt, nxt
    report = tmp_path / "review.txt"
    report.write_text("VERDICT: PASS")
    env.office("review", "T1", "--report", str(report), check=0)
    assert task_row(env)["status"] == "accepted"
    gate = _gates(env, "code_review")[-1]
    assert gate["independence"] == "degraded-orchestrator" and gate["verdict"] == "PASS", gate
    assert "orchestrator" in gate["route"] and task_row(env)["current_revision_id"] in gate["summary"], gate


@pytest.mark.review_contract("v3.1")
@pytest.mark.parametrize("verdict", ["UNAVAILABLE", "ATTENTION"])
def test_v31_a_reviewer_that_could_not_finish_in_either_way_gets_the_two_options(env, tmp_path, verdict):
    approved_run(env, executor=SUBMIT, code_reviewer=[SILENT])
    env.office("dispatch", "T1", check=0)
    con = env.con()
    con.execute("UPDATE gates SET verdict=? WHERE kind='code_review'", (verdict,))
    con.commit()
    assert task_row(env)["status"] == "blocked"
    nxt = _status(env)["next"]
    assert nxt.count("(1)") == 1 and nxt.count("(2)") == 1 and "waive" not in nxt.lower(), nxt
    report = tmp_path / "review.txt"
    report.write_text("VERDICT: PASS")
    code, out = env.office("review", "T1", "--report", str(report), env={"OFFICE_DISPATCH_ID": "D1"})
    assert code != 0 and "worker-cannot-review" in out and task_row(env)["status"] == "blocked", out
    env.office("review", "T1", "--report", str(report), check=0)
    assert task_row(env)["status"] == "accepted"


@pytest.mark.review_contract("v3.1")
def test_v31_orchestrator_review_is_refused_when_produced_externally_or_not_blocked(env, tmp_path):
    approved_run(env, executor=SUBMIT, code_reviewer=[SILENT])
    env.office("dispatch", "T1", check=0)
    report = tmp_path / "review.txt"
    report.write_text("VERDICT: PASS")
    con = env.con()
    con.execute("UPDATE dispatches SET launcher='external' WHERE role='executor'")
    con.commit()
    code, out = env.office("review", "T1", "--report", str(report))
    assert code != 0 and "orchestrator-produced-work" in out and task_row(env)["status"] == "blocked", out
    con.execute("UPDATE dispatches SET launcher='sync' WHERE role='executor'")
    con.commit()
    env.script(executor=SUBMIT, code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("rerun", "T1", "--review", check=0)
    assert task_row(env)["status"] == "accepted"
    code, out = env.office("review", "T1", "--report", str(report))
    assert code != 0 and "fallback-not-allowed" in out, out


# ------------------------------------------------------------------ review-only rerun


@pytest.mark.review_contract("v3.1")
def test_rerun_review_runs_only_the_reviewer_of_the_current_revision(env):
    approved_run(env, executor=SUBMIT, code_reviewer=[SILENT])
    env.office("dispatch", "T1", check=0)
    blocked = task_row(env)
    assert blocked["status"] == "blocked"
    executors = _executors(env)
    env.script(executor=SUBMIT, code_reviewer=[{"reply": "VERDICT: PASS"}])
    code, out = env.office("rerun", "T1", "--review", "--review-as", CODEX)
    assert code == 0 and "code review re-run" in out and "no executor launched" in out, out
    t = task_row(env)
    assert t["status"] == "accepted" and t["current_revision_id"] == blocked["current_revision_id"], t
    assert _executors(env) == executors and [c["role"] for c in env.calls()].count("executor") == 1
    assert _pin(env)["as"] == CODEX
    assert _calls(env, "code_reviewer")[-1]["harness"] == "codex", "the re-run reviewer is the pinned route"
    assert [g["verdict"] for g in _gates(env, "code_review")] == ["UNAVAILABLE", "PASS"]


@pytest.mark.review_contract("v3.1")
def test_rerun_review_refuses_what_a_second_review_cannot_answer(env):
    approved_run(env, executor=SUBMIT, code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    assert task_row(env)["status"] == "accepted"
    before = _executors(env)
    code, out = env.office("rerun", "T1", "--review")
    assert code != 0 and "nothing-to-rerun" in out and "already" in out, out
    code, out = env.office("rerun", "T1", "--review", "--fresh")
    assert code != 0 and "never launches an executor" in out, out
    assert _executors(env) == before


def test_rerun_review_under_the_convergence_contract_reruns_the_lane_reviewer(env):
    _start(env, executor=SUBMIT, convergence_reviewer=[SILENT])
    env.office("dispatch", "T1", check=0)
    assert _scope(env, "L-T1")["status"] in ("unavailable", "attention")
    executors = _executors(env)
    env.script(executor=SUBMIT, convergence_reviewer=[{"reply": APPROVED}])
    env.office("rerun", "T1", "--review", "--review-as", CLAUDE, check=0)
    assert _executors(env) == executors
    assert _calls(env, "convergence_reviewer")[-1]["harness"] == "claude", "the re-run reviewer is the pinned route"
    assert _scope(env, "L-T1")["status"] == "approved"
    assert (_scope(env, "L-T1")["review_pins"]["convergence_review"]["as"]) == CLAUDE


def test_rerun_review_refuses_a_lane_review_that_has_a_verdict(env):
    _start(env, executor=SUBMIT, convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    code, out = env.office("rerun", "L-T1:convergence", "--review")
    assert code != 0 and "nothing-to-rerun" in out and "already has a verdict" in out, out


@pytest.mark.review_contract("v3.1")
def test_dispatch_review_as_on_a_submitted_revision_reruns_review_only(env):
    approved_run(env, executor=SUBMIT, code_reviewer=[SILENT])
    env.office("dispatch", "T1", check=0)
    executors = _executors(env)
    env.script(executor=SUBMIT, code_reviewer=[{"reply": "VERDICT: PASS"}])
    code, out = env.office("dispatch", "T1", "--review-as", CODEX)
    assert code == 0 and "no executor launched" in out, out
    assert _executors(env) == executors and task_row(env)["status"] == "accepted"
    assert _calls(env, "code_reviewer")[-1]["harness"] == "codex", "the re-run reviewer is the pinned route"


def test_dispatch_review_as_on_a_lane_whose_review_could_not_finish_reruns_review_only(env):
    _start(env, executor=SUBMIT, convergence_reviewer=[SILENT])
    env.office("dispatch", "T1", check=0)
    executors = _executors(env)
    env.script(executor=SUBMIT, convergence_reviewer=[{"reply": APPROVED}])
    code, out = env.office("dispatch", "T1", "--review-as", CLAUDE)
    assert code == 0 and "no executor launched" in out, out
    assert _executors(env) == executors
    assert _calls(env, "convergence_reviewer")[-1]["harness"] == "claude", "the re-run reviewer is the pinned route"
    assert _scope(env, "L-T1")["status"] == "approved"


# ------------------------------------------------------------------ pins


def test_dispatch_review_as_never_launches_an_executor_for_work_that_exists(env):
    _start(env, executor=SUBMIT, convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    assert task_row(env)["status"] == "accepted"
    before = _executors(env)
    code, out = env.office("dispatch", "T1", "--review-as", CODEX)
    assert code == 0 and "no review to re-run now" in out and "already has a verdict" in out, out
    assert _executors(env) == before and _pin(env)["as"] == CODEX
    code, out = env.office("dispatch", "T1", "--review-as", CODEX, "--as", CLAUDE)
    assert code == 0 and "already accepted" in out and _executors(env) == before, out


def test_dispatch_review_as_still_starts_a_task_that_has_not_started(env):
    approved_run(env, executor=SUBMIT, convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", "--review-as", CODEX, check=0)
    assert len(_executors(env)) == 1 and _pin(env)["as"] == CODEX
    assert task_row(env)["status"] == "accepted"


@pytest.mark.parametrize("argv", [
    ("dispatch", "T1", "--review-as", CODEX), ("dispatch", "L-T1:visual", "--review-as", CODEX),
    ("rerun", "T1", "--review-as", CODEX), ("rerun", "T1", "--review"), ("rerun", "L-T1:convergence", "--review"),
    ("rerun", "plan", "--review")])
def test_a_dispatched_agent_cannot_pin_or_rerun_a_reviewer(env, argv):
    _start(env, executor=SUBMIT, convergence_reviewer=[{"reply": APPROVED}])
    pins = (_q(env, "SELECT review_override_json FROM tasks"), _scope(env, "L-T1").get("review_pins"))
    code, out = env.office(*argv, env={"OFFICE_DISPATCH_ID": "D123"})
    assert code != 0 and "worker-cannot-pin-reviewer" in out, out
    assert (_q(env, "SELECT review_override_json FROM tasks"), _scope(env, "L-T1").get("review_pins")) == pins
    assert not _executors(env)


def test_a_review_pin_changes_while_the_worker_is_live_and_leaves_it_alone(env):
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": False}], convergence_reviewer=[
        {"reply": APPROVED}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    running = task_row(env)
    assert running["status"] in ("running", "launching")
    before = _executors(env)
    code, out = env.office("dispatch", "T1", "--review-as", CODEX, env=EXTERNAL)
    assert code == 0 and "next review" in out, out
    assert _pin(env)["as"] == CODEX, "dispatch --review-as pinned it"
    code, out = env.office("rerun", "T1", "--review-as", CLAUDE, env=EXTERNAL)
    assert code == 0 and "next review" in out and "no worker" in out, out
    t = task_row(env)
    assert t["current_dispatch_id"] == running["current_dispatch_id"] and t["status"] == running["status"]
    assert _executors(env) == before
    assert _pin(env)["as"] == CLAUDE


def test_a_lane_gate_id_takes_the_same_pin(env):
    _start(env, executor=SUBMIT, convergence_reviewer=[{"reply": APPROVED}])
    code, out = env.office("dispatch", "L-T1:visual", "--review-as", CODEX)
    assert code == 0 and "L-T1:visual reviewer pinned to" in out, out
    from office import convergence, state
    con = env.con()
    run = state.get_run(con, _q(env, "SELECT id FROM runs")[0]["id"])
    assert convergence.scope_pin(run, "L-T1", "visual")["as"] == CODEX
    assert convergence.scope_pin(run, "L-T1", "convergence_review") is None
    code, out = env.office("dispatch", "L-T1:visual")
    assert code != 0 and "review-as" in out, out
    code, out = env.office("dispatch", "L-T1:bogus", "--review-as", CODEX)
    assert code != 0, out


def test_the_lane_visual_reviewer_runs_on_its_pin(env, monkeypatch, tmp_path):
    """A running run pins its visual reviewer by gate id; the first reviewer that runs is that route, which is
    not the one the router would pick first."""
    from test_convergence_contract import PLAN_VISUAL, _fake_capture
    _fake_capture(monkeypatch, tmp_path)
    vis = "EVIDENCE_STATUS: COMPARABLE\n" + APPROVED
    scripts = {"claude:visual_reviewer": [{"reply": vis}], "codex:visual_reviewer": [{"reply": vis}],
               "visual_reviewer": [{"reply": vis}]}
    _start(env, plan=PLAN_VISUAL, executor=SUBMIT, convergence_reviewer=[{"reply": APPROVED}], **scripts,
           probe=[{"reply": "auto"}])
    env.office("dispatch", "T1", check=0)
    default_first = _calls(env, "visual_reviewer")[0]["harness"]
    pinned_to = "codex" if default_first == "claude" else "claude"
    route = CODEX if pinned_to == "codex" else CLAUDE
    env.office("dispatch", "L-T1:visual", "--review-as", route, check=0)
    assert default_first != pinned_to
    from office import convergence, state
    con = env.con()
    run = state.get_run(con, _q(env, "SELECT id FROM runs")[0]["id"])
    assert convergence.scope_pin(run, "L-T1", "visual")["as"] == route
    assert set(_scope(env, "L-T1")["review_pins"]) == {"visual"}, "pinning the visual gate leaves the convergence gate alone"
    # The visual gate is approved; re-run it with the pin through the review-only command.
    con.execute("UPDATE gates SET review_status='UNAVAILABLE', verdict=NULL WHERE kind='visual'")
    con.commit()
    before = len(_calls(env, "visual_reviewer"))
    env.office("rerun", "L-T1:visual", "--review", check=0)
    assert _calls(env, "visual_reviewer")[before]["harness"] == pinned_to, "the pin decided the first route"


# ------------------------------------------------------------------ rerun --as and --review-as


def test_rerun_accepts_as_and_review_as_like_dispatch(env):
    _start(env, executor=[{}], convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    assert task_row(env)["status"] == "blocked", "the executor ended without submitting"
    env.script(executor=SUBMIT, convergence_reviewer=[{"reply": APPROVED}])
    env.office("rerun", "T1", "--fresh", "--as", CODEX, "--review-as", CLAUDE, check=0)
    second = _q(env, "SELECT * FROM dispatches WHERE role='executor' ORDER BY started_at")[-1]
    assert "gpt-6-luna" in second["triple"] and json.loads(second["override_json"])["declared"], second
    assert _pin(env)["as"] == CLAUDE
    assert task_row(env)["status"] == "accepted"
    for flags, why in ((["--resume", "--as", CODEX], "keeps its own"), (["--fresh", "--reroute", "--as", CODEX], "names the route"),
                       (["--fresh", "--cli", "x"], "--cli needs --as"), (["--fresh", "--review-cli", "x"], "need --review-as")):
        code, out = env.office("rerun", "T1", *flags)
        assert code != 0 and why in out, (flags, out)


# ------------------------------------------------------------------ #306: a check that already fails on the base


PLAN_LEGACY = PLAN_ONE.replace("scope: calc.py", "scope: util.py")
LEGACY_SCRIPT = {"write": {"util.py": "X = 1\n"}, "submit": True}


@pytest.mark.review_contract("v3.1")
def test_v31_a_check_that_fails_on_the_base_too_is_pre_existing_and_review_still_runs(env):
    approved_run(env, plan=PLAN_LEGACY, executor=[LEGACY_SCRIPT], code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    checks = _gates(env, "checks")[-1]
    assert checks["verdict"] == "PASS" and "pre-existing" in checks["summary"], checks
    assert [c["role"] for c in env.calls()].count("code_reviewer") == 1, "independent review still ran"
    assert _gates(env, "code_review")[-1]["verdict"] == "PASS"
    assert any(e["kind"] == "gate.preexisting" for e in _q(env, "SELECT kind FROM events"))
    assert task_row(env)["status"] == "accepted"


@pytest.mark.review_contract("v3.1")
def test_v31_a_check_the_task_broke_is_still_the_producers(env):
    approved_run(env, executor=[{"write": {"calc.py": BAD_ADD}, "submit": True}],
                 code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    checks = _gates(env, "checks")[-1]
    assert checks["verdict"] == "CHANGES_REQUIRED" and "pre-existing" not in (checks["summary"] or ""), checks
    assert task_row(env)["status"] == "changes_required"


@pytest.mark.review_contract("v3.1")
def test_v31_waiving_failed_checks_never_accepts_a_task_with_no_independent_review(env):
    """#306: the failure cancelled the review; the waiver must not leave the task accepted without one."""
    approved_run(env, executor=[{"write": {"calc.py": BAD_ADD}, "submit": True}],
                 code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    assert [c["role"] for c in env.calls()].count("code_reviewer") == 0
    env.office("approve", "waive", "T1:checks", "--quote", "that check is known broken", check=0)
    assert [c["role"] for c in env.calls()].count("code_reviewer") == 1, "the waiver revived the review"
    reviews = _gates(env, "code_review")
    assert [g["verdict"] for g in reviews if g["status"] == "done"] == ["PASS"]
    assert task_row(env)["status"] == "accepted"
    assert any(e["kind"] == "gate.revived" for e in _q(env, "SELECT kind FROM events"))


@pytest.mark.review_contract("v3.1")
def test_v31_a_waiver_does_not_revive_a_review_the_user_waived(env):
    approved_run(env, executor=[{"write": {"calc.py": BAD_ADD}, "submit": True}],
                 code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    env.office("approve", "waive", "T1:code", "--quote", "no review needed", check=0)
    env.office("approve", "waive", "T1:checks", "--quote", "known broken", check=0)
    assert [c["role"] for c in env.calls()].count("code_reviewer") == 0
    assert task_row(env)["status"] == "accepted"


def test_convergence_a_check_that_fails_on_the_base_too_is_pre_existing_and_the_lane_is_reviewed(env):
    _start(env, plan=PLAN_LEGACY, executor=[LEGACY_SCRIPT], convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    checks = _gates(env, "checks")[-1]
    assert checks["verdict"] == "APPROVED" and "pre-existing" in checks["summary"], checks
    assert _gates(env, "convergence_review")[-1]["verdict"] == "APPROVED"
    assert _scope(env, "L-T1")["status"] == "approved" and task_row(env)["status"] == "accepted"


def test_convergence_a_check_the_task_broke_is_a_recheck(env):
    _start(env, executor=[{"write": {"calc.py": BAD_ADD}, "submit": True}], convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    assert _gates(env, "checks")[-1]["verdict"] == "RECHECK"
    assert task_row(env)["status"] == "changes_required"


def test_convergence_a_checks_waiver_accepts_the_task_but_the_lane_review_still_runs(env):
    _start(env, executor=[{"write": {"calc.py": BAD_ADD}, "submit": True}], convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    env.office("approve", "waive", "T1:checks", "--quote", "that check is known broken", check=0)
    assert task_row(env)["status"] == "accepted"
    review = _gates(env, "convergence_review")[-1]
    assert review["verdict"] == "APPROVED" and review["independence"] == "independent", review
    assert [c["role"] for c in env.calls()].count("convergence_reviewer") == 1


# ------------------------------------------------------------------ the plan review


def test_an_inline_plan_reviewer_that_cannot_finish_offers_two_options_and_refuses_the_author(env, tmp_path):
    _start(env, gear="express", approve=False, plan_reviewer=[QUOTA])
    assert _run_row(env)["plan_review"]["status"] == "unavailable"
    nxt = _status(env)["next"]
    assert nxt.count("(1)") == 1 and nxt.count("(2)") == 1 and "waive" not in nxt.lower(), nxt
    assert "(2) your own review is not allowed: you wrote this plan inline" in nxt, nxt
    report = tmp_path / "review.txt"
    report.write_text(APPROVED)
    code, out = env.office("review", "plan", "--report", str(report))
    assert code != 0 and "orchestrator-produced-work" in out, out
    assert not _run_row(env)["plan_review"].get("ended")


def test_a_dedicated_planners_plan_may_get_the_orchestrators_recorded_review(env, tmp_path):
    from conftest import PLAN_ONE
    env.trust()
    env.script(planner=[{"plan": PLAN_ONE, "submit": True}], plan_reviewer=[QUOTA])
    env.office("start", "add numbers", "--gear", "full", check=0)
    assert _run_row(env)["plan_review"]["status"] == "unavailable"
    nxt = _status(env)["next"]
    assert "(2) review it yourself, recorded as a non-independent fallback" in nxt, nxt
    assert "office review plan --report <file>" in nxt and "waive" not in nxt.lower(), nxt
    report = tmp_path / "review.txt"
    report.write_text(APPROVED)
    env.office("review", "plan", "--report", str(report), check=0)
    pr = _run_row(env)["plan_review"]
    assert pr["ended"] and pr["status"] == "approved", pr
    gate = _gates(env, "plan_review")[-1]
    assert gate["independence"] == "degraded-orchestrator" and "orchestrator" in gate["route"], gate
    code, out = env.office("review", "plan", "--report", str(report))
    assert code != 0 and "fallback-not-allowed" in out, out


def test_rerun_plan_review_runs_only_the_reviewer_and_spends_no_round(env):
    _start(env, gear="express", approve=False, plan_reviewer=[QUOTA])
    assert _run_row(env)["plan_review"]["status"] == "unavailable"
    rounds_before = [g["round"] for g in _gates(env, "plan_review")]
    env.script(plan_reviewer=[{"reply": APPROVED}])
    env.office("rerun", "plan", "--review", "--review-as", CLAUDE, check=0)
    assert _gates(env, "plan_review")[-1]["round"] == rounds_before[-1], "no round was spent"
    assert _calls(env, "plan_reviewer")[-1]["harness"] == "claude", "the pin decided the route"
    pr = _run_row(env)["plan_review"]
    assert pr["ended"] and pr["status"] == "approved" and pr["review_pin"]["as"] == CLAUDE, pr
    assert [g["review_status"] for g in _gates(env, "plan_review")] == ["UNAVAILABLE", "COMPLETED"]
    code, out = env.office("rerun", "plan", "--review")
    assert code != 0 and "nothing-to-rerun" in out, out


# ------------------------------------------------------------------ what counts as the same failure on the base


def test_the_same_failure_on_the_base_needs_the_same_tests_counts_and_error():
    from office import gates
    base = "FAILED tests/test_a.py::test_old - boom\n=== 1 failed, 4 passed in 0.31s ===\n"
    same = "FAILED tests/test_a.py::test_old - boom\n=== 1 failed, 6 passed in 1.90s ===\n"
    newer = "FAILED tests/test_a.py::test_old - boom\nFAILED tests/test_a.py::test_new - bang\n=== 2 failed in 0.4s ===\n"
    assert gates.same_failure(same, base)
    assert not gates.same_failure(newer, base), "a test only the head fails is the producer's"
    same_file = "FAIL src/a.test.js\nTests: 3 failed, 5 passed\n"
    assert not gates.same_failure(same_file, "FAIL src/a.test.js\nTests: 1 failed, 5 passed\n"), "more failures in a failing file"
    assert gates.same_failure("FAIL src/a.test.js\nTests: 1 failed, 7 passed\n", "FAIL src/a.test.js\nTests: 1 failed, 5 passed\n")
    tb = 'Traceback (most recent call last):\n  File "/w/a/calc.py", line 2, in add\nNotImplementedError\n'
    assert gates.same_failure(tb, tb.replace("/w/a", "/base/checkout"))
    assert not gates.same_failure(tb, tb.replace("NotImplementedError", "AssertionError"))
    assert not gates.same_failure("cargo: 5 passed; 2 failed\n", "cargo: 5 passed; 1 failed\n"), "counts are not normalised away"
    assert gates.same_failure("done in 0.12s at 0x7f12\nexit\n", "done in 3.40s at 0x9a00\nexit\n")
    unit = "FAIL: test_a (m.T)\nAssertionError: 1 != 2\n\nRan 10 tests in 0.5s\n\nFAILED (failures=1)"
    assert gates.failure_ids(unit) == {"test_a (m.T)"}, "a summary line is not a failing test"
    assert not gates.same_failure(unit.replace("test_a", "test_b"), unit), "fixing one failure by breaking another"


def test_scanning_a_long_blank_run_is_linear():
    import time
    from office import gates
    start = time.time()
    assert gates.failure_ids("   \n" * 20_000 + "done\n") == set()
    assert gates.failure_ids("   \n" * 20_000 + "FAILED t::x - y\n") == {"t::x"}
    assert gates.failure_count("1" * 20_000 + " x") is None, "a long digit run is scanned once"
    assert gates.failure_count("noise 12 failed, 3 passed") == 12
    assert time.time() - start < 4.0


def test_a_worker_is_known_by_standing_in_its_task_worktree_even_without_its_environment(env, monkeypatch):
    from office import gates, state
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": False}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    con = env.con()
    run = state.get_run(con, _q(env, "SELECT id FROM runs")[0]["id"])
    worktree = Path(_q(env, "SELECT worktree FROM dispatches WHERE role='executor'")[0]["worktree"])
    monkeypatch.delenv("OFFICE_DISPATCH_ID", raising=False)
    monkeypatch.chdir(worktree)
    with pytest.raises(state.Refused) as refused:
        gates.require_orchestrator(con, run, "review in a reviewer's place")
    assert refused.value.category == "worker-cannot-review" and "T1" in refused.value.message
    monkeypatch.chdir(env.repo)
    gates.require_orchestrator(con, run, "review in a reviewer's place")


# ------------------------------------------------------------------ pins that must win, and reviewers that must not be dropped


def test_a_pin_beats_the_reviewer_a_recheck_cycle_would_resume(env):
    """Round 2 is reviewed by the round-1 reviewer when it can; a pin names another reviewer and wins."""
    recheck = "VERDICT: RECHECK\n" + finding_line() + "\nNEXT fix F1"
    _start(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True},
                          {"write": {"calc.py": GOOD_ADD + "# fixed\n"}, "submit": True}],
           **{"codex:convergence_reviewer": [{"reply": recheck}, QUOTA, QUOTA],
              "claude:convergence_reviewer": [QUOTA, {"reply": APPROVED}]}, convergence_reviewer=[QUOTA])
    env.office("dispatch", "T1", check=0)
    env.office("rerun", "T1", "--fresh", check=0)
    assert _scope(env, "L-T1")["status"] == "unavailable", _scope(env, "L-T1")
    before = len(_calls(env, "convergence_reviewer"))
    env.office("rerun", "L-T1:convergence", "--review", "--review-as", CLAUDE, check=0)
    assert [c["harness"] for c in _calls(env, "convergence_reviewer")][before:][:1] == ["claude"], "the pin decided"
    assert _scope(env, "L-T1")["status"] == "approved"


def finding_line():
    return "FINDING F1 | high | blocking | calc.py:1 | add is wrong | fix it | owner: T1"


def test_the_newest_pin_command_wins_over_an_older_lane_pin(env):
    _start(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": False}], convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "L-T1:convergence", "--review-as", CODEX, check=0)
    assert _scope(env, "L-T1")["review_pins"]["convergence_review"]["as"] == CODEX
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    env.office("dispatch", "T1", "--review-as", CLAUDE, env=EXTERNAL, check=0)
    assert _pin(env)["as"] == CLAUDE
    assert _scope(env, "L-T1")["review_pins"]["convergence_review"]["as"] == CLAUDE, "the lane's older pin no longer outranks it"


@pytest.mark.review_contract("v3.1")
def test_dispatch_review_as_on_a_task_blocked_for_another_reason_still_relaunches_the_executor(env):
    approved_run(env, executor=[{"write": {"calc.py": BAD_ADD}, "submit": True}, {}],
                 code_reviewer=[{"reply": "VERDICT: PASS"}])
    env.office("dispatch", "T1", check=0)
    assert task_row(env)["status"] == "changes_required"  # the check failed
    env.office("rerun", "T1", "--fresh", check=0)
    assert task_row(env)["status"] == "blocked" and task_row(env)["current_revision_id"], "ended without submitting"
    before = _executors(env)
    env.office("dispatch", "T1", "--review-as", CODEX, check=0)
    assert len(_executors(env)) == len(before) + 1 and _pin(env)["as"] == CODEX


def test_a_route_pinned_without_naming_a_gate_pins_the_code_review_only(env, monkeypatch, tmp_path):
    from test_convergence_contract import PLAN_VISUAL, _fake_capture
    _fake_capture(monkeypatch, tmp_path)
    _start(env, plan=PLAN_VISUAL, executor=SUBMIT, convergence_reviewer=[QUOTA], visual_reviewer=[SILENT],
           probe=[{"reply": "auto"}])
    env.office("dispatch", "T1", check=0)
    assert {g["review_status"] for g in _gates(env, "convergence_review") + _gates(env, "visual")} == {"UNAVAILABLE"}
    env.office("rerun", "T1", "--review", "--review-as", CODEX, check=0)
    assert set(_scope(env, "L-T1")["review_pins"]) == {"convergence_review"}
    env.office("rerun", "L-T1:visual", "--review", "--review-as", CLAUDE, check=0)
    assert _scope(env, "L-T1")["review_pins"]["visual"]["as"] == CLAUDE


def test_a_prompt_left_typed_in_a_live_reviewer_is_not_abandoned_for_the_next_route(env, monkeypatch):
    """A held re-prompt means its agent is waiting for Enter, not silent: no other route is tried."""
    from office import gates
    held = f"reviewer D1 (x): Office's re-prompt is {gates.HELD_PROMPT} in herdr agent a; submit it"

    def reprompt(con, run, d, ddir, output, parsed, **kw):
        return "", parsed, held

    monkeypatch.setattr(gates, "_reprompt_until_valid", reprompt)
    _start(env, executor=SUBMIT, convergence_reviewer=[{"reply": ""}])
    env.office("dispatch", "T1", check=0)
    gate = _gates(env, "convergence_review")[-1]
    assert gate["review_status"] == "INVALID_RESULT" and gates.HELD_PROMPT in gate["summary"], gate
    assert len(_calls(env, "convergence_reviewer")) == 1, "no other route was tried"


def test_checks_that_fail_on_an_empty_submission_are_the_producers_not_the_bases(env):
    """Nothing was changed, so nothing is inherited: a submission that does no work does not pass its checks."""
    _start(env, executor=[{"submit": True}], convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    checks = _gates(env, "checks")[-1]
    assert checks["verdict"] == "RECHECK" and "pre-existing" not in (checks["summary"] or ""), checks


def test_a_failure_in_a_file_the_task_changed_is_the_tasks():
    from office import gates
    out = 'Traceback ...\n  File "/w/pkg/calc.py", line 2, in add\nNotImplementedError\n'
    assert gates._names_a_file(out, ["pkg/calc.py"]) and gates._names_a_file(out, ["calc.py"])
    assert not gates._names_a_file(out, ["util.py", "README.md"])
    assert not gates._names_a_file(out, ["a.c"]), "a name too short to mean anything is ignored"


@pytest.mark.review_contract("v3.1")
def test_v31_waiving_the_visual_gate_while_checks_fail_revives_nothing(env, monkeypatch, tmp_path):
    from test_convergence_contract import PLAN_VISUAL, _fake_capture
    _fake_capture(monkeypatch, tmp_path)
    approved_run(env, plan=PLAN_VISUAL, executor=[{"write": {"calc.py": BAD_ADD}, "submit": True}],
                 code_reviewer=[{"reply": "VERDICT: PASS"}], visual_reviewer=[{"reply": "EVIDENCE_STATUS: COMPARABLE\nVERDICT: PASS"}],
                 probe=[{"reply": "auto"}])
    env.office("dispatch", "T1", check=0)
    cancelled = {g["kind"] for g in _gates_v31(env) if g["status"] == "cancelled"}
    assert cancelled == {"code_review", "visual"}, "the failed checks cancelled both waiting reviews"
    env.office("approve", "waive", "T1:visual", "--quote", "no ui review needed", check=0)
    assert [c["role"] for c in env.calls()].count("code_reviewer") == 0, "checks still fail: no review is revived"
    assert task_row(env)["status"] == "changes_required"
    env.office("approve", "waive", "T1:checks", "--quote", "known broken", check=0)
    assert [c["role"] for c in env.calls()].count("code_reviewer") == 1, "the checks waiver revives the code review"
    assert [c["role"] for c in env.calls()].count("visual_reviewer") == 0, "the visual review stays waived"
    assert task_row(env)["status"] == "accepted"


def _gates_v31(env):
    return _q(env, "SELECT kind, status, verdict FROM gates WHERE task_id='T1' ORDER BY created_at")


def test_a_plan_review_is_not_re_run_on_a_finished_run(env):
    from office import rerun, state
    _start(env, gear="express", approve=False, plan_reviewer=[QUOTA])
    con = env.con()
    con.execute("UPDATE runs SET phase='abandoned'")
    con.commit()
    run = state.get_run(con, _q(env, "SELECT id FROM runs")[0]["id"])
    with pytest.raises(state.Refused) as refused:
        rerun.rerun(con, run, "plan", resume=False, fresh=False, review=True)
    assert refused.value.category == "run-terminal"
    assert [g["review_status"] for g in _gates(env, "plan_review")] == ["UNAVAILABLE"]


def test_reviewer_signatures_name_walls_not_words():
    from office import gates
    for text in ("commit 3a4291fbc", "ctx 14290 tokens", "the quota docs are exhausted of examples", "port 4290",
                 "see v1.429", "GET /api/429/items", "version 4.29", "src/office/gates.py:429: bug", "line 429 of x",
                 "quota will reset at noon in the docs"):
        assert not gates._quota_signature(text), text
    for text in ("HTTP 429", "Error: 429.", "You've hit your usage limit", "Error: rate limit exceeded",
                 "quota exceeded for project", "Too Many Requests", "RESOURCE_EXHAUSTED", "You exceeded your current quota",
                 "You have exhausted your capacity on this model. Your quota will reset after 3s.", "Your quota is exhausted",
                 "monthly limit reached", "out of credits", "insufficient quota"):
        assert gates._quota_signature(text), text


def test_the_pinned_hint_never_names_a_route_already_walled_on_the_gate(env):
    from office import gates, state
    _walled(env)  # every harness walled on this gate
    con = env.con()
    run = state.get_run(con, _q(env, "SELECT id FROM runs")[0]["id"])
    gate = _gates(env, "convergence_review")[-1]
    tried = gates.reviewer_tried(con, run["id"], [g["id"] for g in _gates(env, "convergence_review")])
    assert {f"harness:{h}" for h in ("codex", "claude", "gemini", "agy")} & tried, tried
    hint = gates._pinned_stall_hint(con, run, gate, "code_reviewer", ("codex@1/luna@high", {"kind": "quota"}), set())
    assert "--review-as <harness/model[@effort]>" in hint, f"no route is left to name: {hint}"
    assert gates.next_reviewer_route(con, run, "code_reviewer", None, tried) is None
    assert gates.next_reviewer_route(con, run, "code_reviewer", None, set()) is not None, "the control names one"


# ------------------------------------------------------------------ inherited failures, end to end


def _runner_plan(env, body):
    script = env.tmp / "runner.sh"
    script.write_text(body)
    return PLAN_ONE.replace('checks: python3 -c "import calc; assert calc.add(2, 3) == 5"', f"checks: sh {script}")


def test_a_failure_that_names_a_file_the_task_changed_is_the_tasks(env):
    plan = _runner_plan(env, "echo 'FAIL calc.py: add is wrong'\nexit 1\n")
    _start(env, plan=plan, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], convergence_reviewer=[
        {"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    checks = _gates(env, "checks")[-1]
    assert checks["verdict"] == "RECHECK" and "pre-existing" not in (checks["summary"] or ""), checks


def test_a_failure_that_names_nothing_the_task_changed_and_matches_the_base_is_pre_existing_with_evidence(env):
    plan = _runner_plan(env, "echo 'FAIL legacy.test: flaky since forever'\nexit 1\n").replace("scope: calc.py", "scope: util.py")
    _start(env, plan=plan, executor=[LEGACY_SCRIPT], convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    checks = _gates(env, "checks")[-1]
    assert checks["verdict"] == "APPROVED" and "pre-existing" in checks["summary"], checks
    row = _q(env, "SELECT path, meta_json FROM evidence WHERE kind='check_output_base'")[0]
    assert "legacy.test" in Path(row["path"]).read_text()
    assert json.loads(row["meta_json"])["base_commit"] == _q(env, "SELECT base_commit FROM revisions")[0]["base_commit"]


def test_a_check_that_exits_differently_on_the_base_is_not_the_same_failure(env):
    body = "echo 'same words'\nif [ -f util.py ]; then exit 1; fi\nexit 2\n"
    plan = _runner_plan(env, body).replace("scope: calc.py", "scope: util.py")
    _start(env, plan=plan, executor=[LEGACY_SCRIPT], convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    checks = _gates(env, "checks")[-1]
    assert checks["verdict"] == "RECHECK" and "pre-existing" not in (checks["summary"] or ""), checks


def test_a_base_that_cannot_be_checked_out_leaves_the_failure_the_producers_and_nothing_behind(env, monkeypatch):
    from office import gates, paths, state
    _start(env, executor=[{}], convergence_reviewer=[{"reply": APPROVED}])
    con = env.con()
    run = state.get_run(con, _q(env, "SELECT id FROM runs")[0]["id"])
    base = paths.git(Path(run["repo_root"]), "rev-parse", "HEAD")
    (env.repo / "extra.txt").write_text("changed\n")
    env.git("add", "-A")
    env.git("commit", "-qm", "work")
    rev = {"id": "R1", "base_commit": base, "commit_sha": paths.git(Path(run["repo_root"]), "rev-parse", "HEAD")}
    gate, failure = {"id": "Gx", "task_id": "T1"}, {"code": "C1", "location": "true", "_output": "boom", "_exit": 1}
    made = paths.run_dir(run["id"]) / "checkouts" / "base-Gx"

    tried = []

    def half_made(run_, commit, name, purpose):
        tried.append(commit)
        made.mkdir(parents=True)
        raise OSError("disk full while setting up")

    monkeypatch.setattr(gates, "detached_checkout", half_made)
    # The revision's own delta (changed_json) is empty, as a resubmission of the same tree is; what it changed
    # since its base is not, so the base is still consulted.
    rev["changed_json"] = "[]"
    assert gates._split_preexisting(con, run, gate, rev, [failure], 5, env.tmp) == ([failure], [])
    assert tried == [base], "the files changed since the base decide, not the last revision's delta"
    assert not made.exists(), "a checkout that failed half way is removed"


def test_a_deleted_working_directory_is_not_a_worker(env, monkeypatch):
    from office import gates, state
    approved_run(env, executor=SUBMIT, convergence_reviewer=[{"reply": APPROVED}])
    con = env.con()
    run = state.get_run(con, _q(env, "SELECT id FROM runs")[0]["id"])

    def gone():
        raise FileNotFoundError("cwd was removed")

    monkeypatch.setattr(Path, "cwd", staticmethod(gone))
    gates.require_orchestrator(con, run, "pin a reviewer")


def test_a_pin_or_plan_rerun_called_directly_by_a_worker_is_refused(env, monkeypatch):
    from office import convergence, plans, state
    approved_run(env, executor=SUBMIT, convergence_reviewer=[{"reply": APPROVED}])
    con = env.con()
    run = state.get_run(con, _q(env, "SELECT id FROM runs")[0]["id"])
    monkeypatch.setenv("OFFICE_DISPATCH_ID", "D1")
    with pytest.raises(state.Refused) as refused:
        convergence.set_review_pin(con, run, "T1", CODEX)
    assert refused.value.category == "worker-cannot-pin-reviewer"
    with pytest.raises(state.Refused) as refused:
        plans.rerun_review(con, run, pin=None)
    assert refused.value.category == "worker-cannot-pin-reviewer"


def test_a_task_pin_leaves_a_lane_without_pins_and_its_visual_pin_alone(env):
    _start(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": False}], convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    env.office("dispatch", "T1", "--review-as", CODEX, env=EXTERNAL, check=0)
    assert not _scope(env, "L-T1").get("review_pins"), "a task pin does not invent a lane pin"
    env.office("dispatch", "L-T1:visual", "--review-as", CLAUDE, check=0)
    env.office("dispatch", "L-T1:convergence", "--review-as", CODEX, check=0)
    env.office("dispatch", "T1", "--review-as", CLAUDE, env=EXTERNAL, check=0)
    pins = _scope(env, "L-T1")["review_pins"]
    assert pins["convergence_review"]["as"] == CLAUDE and pins["visual"]["as"] == CLAUDE


def test_a_pin_that_names_another_route_never_resumes_the_earlier_reviewers_session(env, monkeypatch):
    from office import dispatch, gates
    recheck = "VERDICT: RECHECK\n" + finding_line() + "\nNEXT fix F1"
    _start(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True},
                          {"write": {"calc.py": GOOD_ADD + "# fixed\n"}, "submit": True}],
           **{"codex:convergence_reviewer": [{"reply": recheck}, QUOTA, QUOTA],
              "claude:convergence_reviewer": [QUOTA, {"reply": APPROVED}]}, convergence_reviewer=[QUOTA])
    env.office("dispatch", "T1", check=0)
    env.office("rerun", "T1", "--fresh", check=0)
    assert _scope(env, "L-T1")["status"] == "unavailable"
    # The runtime could resume the round-1 reviewer's session (a herdr agent): its route is codex.
    monkeypatch.setattr(gates, "_reviewer_resume", lambda con, run, parent, kind, cwd: (
        {"parent": parent, "session_id": "s-1", "argv": ["--resume"], "herdr_kind": "codex"}, "codex@1/luna@high"))
    resumed = []
    real = dispatch.launch

    def launch(run, d, kind, *a, **kw):
        resumed.append((d["harness"], kw.get("resume")))
        if kw.get("resume"):
            raise AssertionError(f"{d['harness']} reviewer would resume another route's session")  # not a hang
        return real(run, d, kind, *a, **kw)

    monkeypatch.setattr(dispatch, "launch", launch)
    env.office("rerun", "L-T1:convergence", "--review", "--review-as", CLAUDE, check=0)
    reviewers = [r for r in resumed if r[0] in ("claude", "codex")]
    assert reviewers and reviewers[0] == ("claude", None), "the pinned route starts fresh, not in codex's session"


def test_a_route_pinned_without_a_gate_says_so_when_only_the_visual_gate_is_left(env, monkeypatch, tmp_path):
    from test_convergence_contract import PLAN_VISUAL, _fake_capture
    _fake_capture(monkeypatch, tmp_path)
    _start(env, plan=PLAN_VISUAL, executor=SUBMIT, convergence_reviewer=[{"reply": APPROVED}], visual_reviewer=[SILENT],
           probe=[{"reply": "auto"}])
    env.office("dispatch", "T1", check=0)
    assert _gates(env, "convergence_review")[-1]["verdict"] == "APPROVED"
    assert _gates(env, "visual")[-1]["review_status"] == "UNAVAILABLE"
    code, out = env.office("rerun", "T1", "--review", "--review-as", CODEX)
    assert code == 0 and "pinned to" not in out and "named by id" in out, out
    assert not _scope(env, "L-T1").get("review_pins")


def test_a_reply_wrapped_title_case_records_and_padded_captures_are_read_as_they_are():
    from office import review_parse
    long = "x" * 45
    text = (f"VERDICT: CHANGES_REQUIRED\nFINDING F1 | material | src/a.py:1 | {long}\n"
            f"Finding F2 | material | src/b.py:2 | {long}\nFinding F3 | minor | c | d\n")
    assert [f["code"] for f in review_parse.parse(text).findings] == ["F1", "F2", "F3"]
    lines = ["VERDICT: CHANGES_REQUIRED", "FINDING F1 | material | src/a.py:1 | short one", "FINDING F2 | minor | b | other",
             "some trailing prose line"]
    padded = "\n".join(line.ljust(80) for line in lines)
    assert review_parse.join_wrapped(padded).count("\n") == 3, "a pane-padded capture has nothing to rejoin"
    p = review_parse.parse(padded)
    assert p.valid and [f["code"] for f in p.findings] == ["F1", "F2"]


def test_a_changed_file_with_a_space_or_a_non_ascii_name_is_still_recognised(env):
    from office import gates, paths, state
    _start(env, executor=[{}], convergence_reviewer=[{"reply": APPROVED}])
    con = env.con()
    run = state.get_run(con, _q(env, "SELECT id FROM runs")[0]["id"])
    repo = Path(run["repo_root"])
    base = paths.git(repo, "rev-parse", "HEAD")
    (env.repo / "caf\u00e9 \u00fcn\u00ef.txt").write_text("changed\n")
    env.git("add", "-A")
    env.git("commit", "-qm", "work")
    rev = {"id": "R1", "base_commit": base, "commit_sha": paths.git(repo, "rev-parse", "HEAD")}
    out = "FAIL caf\u00e9 \u00fcn\u00ef.txt\n"
    failure = {"code": "C1", "location": "sh -c 'echo FAIL caf\u00e9 \u00fcn\u00ef.txt; exit 1'", "_output": out, "_exit": 1}
    got = gates._split_preexisting(con, run, {"id": "Gu", "task_id": "T1"}, rev, [failure], 30, env.tmp)
    assert got == ([failure], []), "the failure names a file the task changed, whatever its name looks like"
    assert not (paths.run_dir(run["id"]) / "checkouts" / "base-Gu").exists()


def test_a_malformed_record_is_not_swallowed_by_the_line_above_it():
    from office import review_parse
    long = "FINDING F1 | material | src/a.py:1 | " + "x" * 60
    text = f"VERDICT: CHANGES_REQUIRED\n{long}\nFINDING F2 missing pipes here\nDEFECT D1 no pipes\nNEXT: fix\n"
    assert review_parse.join_wrapped(text).splitlines() == text.splitlines()
    convergence = f"VERDICT: RECHECK\n{long.replace('material', 'high | blocking')}\nFINDING F2 missing pipes here\nNEXT fix\n"
    p = review_parse.parse(convergence, contract="convergence-v1")
    assert not p.valid and any("unreadable FINDING line" in e for e in p.errors), p.errors
    second = "NEXT: " + "y" * (len(long) - 6)
    wrapped = f"VERDICT: PASS\n{long}\nFinding the cause took a while\n{second}\nfix it\n"
    assert review_parse.join_wrapped(wrapped).count("\n") == 3, "in a wrapped text, prose that starts with the word is a tail"
