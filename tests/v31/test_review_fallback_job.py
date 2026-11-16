"""T4: a reviewer attempt is one dispatch on one route, chosen once and recorded on the dispatch row. A failed attempt
queues one `review_fallback` job that re-runs the review on the next eligible route, and route exclusions are read
from the gate's recorded failed dispatches. A review that returned no verdict is re-dispatched a bounded number of
times, spending no round. A revoked reviewer is never re-dispatched and never counted against its route.

Lane reviews run the default convergence-v1 contract against scripted fake harnesses; no real agent runs.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from test_convergence_contract import APPROVED, _gates, _q, _run_row, _start
from test_review_reroute import EXECUTOR

EMPTY = {"reply": ""}
SCOPE = "L-T1"


def _jobs(env, kind="review_fallback"):
    return [{**r, "payload": json.loads(r["payload_json"])}
            for r in _q(env, "SELECT * FROM outbox WHERE kind=? ORDER BY created_at", (kind,))]


def _attempts(env, gate_id):
    from office import gates
    con = env.con()
    try:
        return gates.review_attempts(con, gate_id)
    finally:
        con.close()


def _lane(env):
    return _gates(env, "convergence_review")[-1]


def _set_gates(env, **fields):
    con = env.con()
    run = _run_row(env)
    con.execute("UPDATE runs SET gates_json=? WHERE id=?", (json.dumps({**run["gates"], **fields}), run["id"]))
    con.commit()
    con.close()


# ------------------------------------------------------------------ one attempt, one dispatch, one fallback job

def test_a_failed_attempt_queues_one_fallback_job_and_the_failure_is_recorded_on_its_dispatch(env):
    _start(env, executor=EXECUTOR, convergence_reviewer=[EMPTY, {"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    gate = _lane(env)
    first, second = _attempts(env, gate["id"])
    assert first["triple"] != second["triple"], "the next route runs, not the same one"
    assert first["failure"]["class"] == "silent" and "no reply after re-prompts" in first["failure"]["reason"]
    assert second["failure"] is None
    (job,) = _jobs(env)
    assert job["status"] == "done" and job["payload"]["gate_id"] == gate["id"]
    assert job["payload"]["origin"]["kind"] == "convergence_review"
    assert job["payload"]["fallback"]["epoch"] == 0, "the next attempt of the same chain, not a re-dispatch"
    assert gate["review_status"] == "COMPLETED" and gate["verdict"] == "APPROVED"
    dispatches = _q(env, "SELECT outcome, attribution, route_json FROM dispatches WHERE gate_id=? ORDER BY started_at, rowid",
                    (gate["id"],))
    assert [d["outcome"] for d in dispatches][0] == "environment_failure" and dispatches[0]["attribution"] == "adapter"
    assert json.loads(dispatches[0]["route_json"])["review_attempt"]["failure"]["class"] == "silent"
    assert gate["env_failures"] == 1


def test_exclusions_come_from_the_recorded_failed_dispatches_not_from_memory(env):
    from office import gates
    _start(env, executor=EXECUTOR, convergence_reviewer=[EMPTY, {"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    gate = _lane(env)
    attempts = _attempts(env, gate["id"])
    first = attempts[0]
    # a silent harness is walled for every model on it: its route and the whole harness are excluded
    assert gates.route_exclusions(attempts) == {first["triple"], f"harness:{first['harness']}"}
    # the exclusion is exactly what the dispatch row recorded: take the record away and nothing is excluded
    con = env.con()
    row = con.execute("SELECT route_json FROM dispatches WHERE id=?", (first["id"],)).fetchone()
    route = json.loads(row["route_json"])
    route["review_attempt"].pop("failure")
    con.execute("UPDATE dispatches SET route_json=? WHERE id=?", (json.dumps(route), first["id"]))
    con.commit()
    try:
        assert gates.route_exclusions(gates.review_attempts(con, gate["id"])) == set()
    finally:
        con.close()


def test_a_launch_failure_queues_a_fallback_job_and_excludes_only_that_route(env):
    from office import gates
    _start(env, executor=EXECUTOR, convergence_reviewer=[{"reply": "", "exit": 3}, {"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    gate = _lane(env)
    first, second = _attempts(env, gate["id"])
    assert first["failure"]["class"] == "launch" and "with no reply" in first["failure"]["reason"]
    assert gates.route_exclusions([first]) == {first["triple"]}
    assert len(_jobs(env)) == 1 and gate["review_status"] == "COMPLETED"
    assert second["triple"] != first["triple"]


def test_a_quota_or_auth_wall_excludes_the_whole_harness():
    from office import gates
    walled = {"id": "D1", "triple": "codex@1/gpt-6-luna@low", "harness": "codex", "epoch": 0,
              "failure": {"class": "wall", "reason": "x [quota]", "harness_wide": True}}
    assert gates.route_exclusions([walled]) == {"codex@1/gpt-6-luna@low", "harness:codex"}  # R13
    plain = {**walled, "failure": {"class": "launch", "reason": "exit 3"}}
    assert gates.route_exclusions([plain]) == {"codex@1/gpt-6-luna@low"}


def test_a_pointer_that_never_landed_is_retried_through_the_job_without_counting_a_route_failure(env, monkeypatch):
    from office import gates
    real = gates._launch_spec
    seen = []

    def spec(ddir):
        if seen and ddir.name not in seen[:1]:
            return real(ddir)
        seen.append(ddir.name)
        return {**real(ddir), "retry_same_route": "held-pointer"}

    monkeypatch.setattr(gates, "_launch_spec", spec)
    _start(env, executor=EXECUTOR, convergence_reviewer=[EMPTY, {"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    gate = _lane(env)
    first, second = _attempts(env, gate["id"])
    assert first["triple"] == second["triple"] and first["failure"]["class"] == "pointer"
    assert gates.route_exclusions([first]) == set(), "the route is not at fault"
    assert gate["env_failures"] == 0 and gate["review_status"] == "COMPLETED"
    assert len(_jobs(env)) == 1


def test_a_worktree_guard_degrade_moves_to_the_next_route_through_the_job(env, monkeypatch):
    from office import gates
    guarded = []

    def guard_for(d, profile_kind, cwd):
        if profile_kind != "reviewer":
            return None
        guarded.append(d["id"])  # asked twice per attempt (the shared checkout, then its snapshot)
        first = d["id"] == guarded[0]
        return lambda before: ({} if before is None else ("tracked diff" if first else None))

    monkeypatch.setattr(gates, "_worktree_guard", guard_for)
    env.hide_agy = True  # the patch only reaches review jobs that run in this process
    _start(env, executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    gate = _lane(env)
    first, second = _attempts(env, gate["id"])
    assert first["failure"]["class"] == "degraded" and "the worktree changed during the review" in first["failure"]["reason"]
    assert second["failure"] is None and second["triple"] != first["triple"]
    assert len(_jobs(env)) == 1 and gate["review_status"] == "COMPLETED"


# ------------------------------------------------------------------ a no-verdict review is re-dispatched, bounded

def _events(env, kind):
    return _q(env, "SELECT * FROM events WHERE kind=? ORDER BY seq", (kind,))


def _redispatches(env):
    return [e for e in _events(env, "review.fallback_queued") if json.loads(e["payload_json"])["how"] == "redispatch"]


def test_a_review_no_route_answered_is_redispatched_up_to_the_bound_then_status_names_the_manual_step(env):
    _start(env, executor=EXECUTOR, convergence_reviewer=[EMPTY])
    env.office("dispatch", "T1", check=0)
    gate = _lane(env)
    assert gate["review_status"] == "UNAVAILABLE" and gate["verdict"] is None, gate
    assert len(_redispatches(env)) == 1, "the default bound is one re-dispatch per gate"
    attempts = _attempts(env, gate["id"])
    assert {a["epoch"] for a in attempts} == {0, 1}, "the re-dispatch launched a reviewer on a new chain"
    # the new chain starts afresh: routes the exhausted chain failed on run again, but within the chain a harness
    # walled by a failure stays excluded
    assert {a["triple"] for a in attempts if a["epoch"] == 1} & {a["triple"] for a in attempts if a["epoch"] == 0}
    walled = set()
    for a in (a for a in attempts if a["epoch"] == 1):
        assert a["harness"] not in walled
        if a["failure"] and a["failure"].get("harness_wide"):
            walled.add(a["harness"])
    assert json.loads(_redispatches(env)[0]["payload_json"])["epoch"] == 1
    assert int(gate["round"]) == 1, "no substantive round was spent"
    assert _run_row(env)["gates"].get("review_redispatch_max") is None
    code, status = env.office("status")
    assert f"office review {SCOPE}:convergence" in status, status


@pytest.mark.parametrize("bound", [0, 2])
def test_the_redispatch_bound_is_configurable_per_gate(env, bound):
    _start(env, executor=EXECUTOR, convergence_reviewer=[EMPTY])
    # the run pinned no bound: set it on the stored run, then re-run the review (a manual re-run is a new gate)
    _set_gates(env, review_redispatch_max=bound)
    env.office("dispatch", "T1", check=0)
    assert len(_redispatches(env)) == bound
    assert _lane(env)["review_status"] == "UNAVAILABLE"


def test_a_redispatch_that_finds_a_working_route_settles_the_review(env, monkeypatch):
    from office import gates

    state = {"n": 0}
    real = gates._review_attempt

    def flaky(con, run, gate, role, brief, **kw):
        # every attempt of the first chain fails on its route; the fresh chain's first attempt answers
        if kw["epoch"] == 0 and role == "code_reviewer" and "runtime self-review subagent" not in brief:
            state["n"] += 1
            return gates._unavailable(con, True, gate, ["every route walled"], None), \
                {"redispatch": True, "reason": "every route walled", "dispatch_id": None}
        return real(con, run, gate, role, brief, **kw)

    monkeypatch.setattr(gates, "_review_attempt", flaky)
    _start(env, executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    gate = _lane(env)
    assert state["n"] == 1 and gate["review_status"] == "COMPLETED" and gate["verdict"] == "APPROVED", gate
    (event,) = _redispatches(env)
    assert json.loads(event["payload_json"])["epoch"] == 1
    assert [j["payload"]["fallback"]["redispatches"] for j in _jobs(env)] == [1]


# ------------------------------------------------------------------ REVOKED

def test_a_revoked_reviewer_is_never_redispatched_never_counted_and_its_route_stays_eligible(env, monkeypatch):
    from office import db, dispatch, gates, state
    real = dispatch.launch

    def launch(run, d, kind, ddir, **kw):
        if kind != "reviewer":
            return real(run, d, kind, ddir, **kw)
        con = db.connect()
        try:  # the race: the revoke marker, then the process end recorded by stop_dispatch as a signal exit
            with db.transaction(con):
                state.emit(con, run, dispatch.REVOKED_KIND, f"{d['id']} revoked", audience="runtime", dispatch_id=d["id"])
                con.execute("UPDATE dispatches SET status='failed', terminal_classification='signal', "
                            "ended_at='2026-01-01T00:00:00+00:00' WHERE id=?", (d["id"],))
        finally:
            con.close()
        return {}

    env.hide_agy = True
    monkeypatch.setattr(dispatch, "launch", launch)
    _start(env, executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}] * 3)
    env.office("dispatch", "T1", check=0)
    gate = _lane(env)
    assert gate["review_status"] == "REVOKED"
    assert _jobs(env) == [] and not _redispatches(env), "REVOKED is never auto re-dispatched"
    (attempt,) = _attempts(env, gate["id"])
    assert attempt["failure"] is None, "a revoked dispatch records no route failure"
    assert gates.route_exclusions([attempt]) == set()
    assert gate["env_failures"] == 0
    (d,) = _q(env, "SELECT outcome, attribution FROM dispatches WHERE gate_id=?", (gate["id"],))
    assert d["outcome"] != "environment_failure" and d["attribution"] != "adapter", "no demotion input"
    con = env.con()
    try:
        tried = gates.tried_reviewer_routes(con, "g.id=?", (gate["id"],))
    finally:
        con.close()
    assert tried == [], "a review-only re-run does not start away from a revoked route"


# ------------------------------------------------------------------ REVIEWED_COMMIT re-run through the job

WRONG = "REVIEWED_COMMIT deadbeefdeadbeef\nVERDICT: APPROVED\nNEXT proceed"


def test_a_wrong_reviewed_commit_is_rerun_through_the_job_resuming_the_same_reviewer(env):
    _start(env, executor=EXECUTOR, convergence_reviewer=[{"reply": WRONG, "stamp": False}, {"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    gate = _lane(env)
    (job,) = _jobs(env)
    fb = job["payload"]["fallback"]
    assert fb["commit_retries"] == 1 and fb["epoch"] == 1 and fb["resume_from"], fb
    assert "REVIEWED_COMMIT" in fb["correction"] and "is not the composed commit" in fb["correction"]
    first, second = _attempts(env, gate["id"])
    assert (first["epoch"], second["epoch"]) == (0, 1) and first["failure"] is None
    assert gate["review_status"] == "COMPLETED"
    row = _q(env, "SELECT resumed_from FROM dispatches WHERE id=?", (second["id"],))[0]
    assert row["resumed_from"] in (None, first["id"])  # resumed when the harness can, else a fresh session


# ------------------------------------------------------------------ the job itself

def test_the_fallback_job_reruns_the_origin_job_with_its_state(monkeypatch):
    from office import gates, jobs
    seen = []
    monkeypatch.setattr(jobs, "handler", lambda kind: (lambda con, run, job: seen.append((kind, job)) or {"ok": 1}))
    origin = {"kind": "plan_review", "payload": {"gate_id": "G1", "plan_version": 2, "office_version": "x"}}
    job = {"id": "J1", "run_id": "r", "kind": "review_fallback",
           "payload": {"gate_id": "G1", "origin": origin, "fallback": {"epoch": 2, "redispatches": 1}}}
    assert gates.job_review_fallback(None, {"id": "r"}, job) == {"ok": 1}
    ((kind, inner),) = seen
    assert kind == "plan_review" and inner["kind"] == "plan_review"
    assert inner["payload"] == {"gate_id": "G1", "plan_version": 2, "office_version": "x",
                                "review_fallback": {"epoch": 2, "redispatches": 1}}


def test_the_fallback_job_is_registered_and_owns_its_gate():
    from office import jobs
    handlers = jobs._handlers()
    assert {"review_fallback", "integration_review"} <= set(handlers)
    assert jobs.handler("review_fallback") is handlers["review_fallback"]


# ------------------------------------------------------------------ the subagent and integration seams

def _subagent_ctx(env):
    from office import state
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    return con, run, [state.get_task(con, run["id"], "T1")]


def _subagent_rows(con, commit):
    return con.execute("SELECT id, status, review_status FROM gates WHERE kind='self_review_subagent' "
                       f"AND input_key LIKE '%:{commit}:%' ORDER BY created_at, rowid").fetchall()


def test_a_subagent_that_was_not_launched_is_retried_through_the_bounded_fallback_not_blocked(env):
    from office import contract, self_review_subagents as srs

    env.hide_agy = True
    _start(env, gear="S", executor=EXECUTOR, code_reviewer=[{"reply": APPROVED}], convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    con, run, tasks = _subagent_ctx(env)
    job = {"id": "Jconv", "kind": "convergence_review", "payload": {"gate_id": "Gconv"}}
    calls = []
    answers = [{"fallback_queued": "Jfb"}, {"status": contract.UNAVAILABLE, "verdict": None, "parsed": None,
                                            "summary": "no route"}]

    def reviewer(con_, run_, gate, role, brief, **kw):
        calls.append((gate["id"], kw.get("job")))
        return answers.pop(0)

    try:
        queued = srs.audit_scope(con, run, tasks, Path(env.repo), "d" * 40, reviewer=reviewer, job=job)
        (open_gate,) = _subagent_rows(con, "d" * 40)
        spent = srs.audit_scope(con, run, tasks, Path(env.repo), "c" * 40, reviewer=reviewer, job=job)
        (blocked,) = _subagent_rows(con, "c" * 40)
    finally:
        con.close()
    assert queued["status"] == srs.WAITING, "a subagent whose next route is queued is settled by its own jobs"
    assert open_gate["status"] == "queued", "the subagent's gate stays open for the re-run"
    assert [sub["payload"]["gate_id"] for _, sub in calls] == [open_gate["id"], blocked["id"]]
    assert all(sub["payload"]["subagent"]["parent"]["payload"] == job["payload"] for _, sub in calls), \
        "the owning job reaches the reviewer through the subagent's own job: its fallback lifecycle settles it"
    assert spent["status"] == contract.EVIDENCE_BLOCKED and "no route" in spent["reason"], "a spent bound blocks"
    assert blocked["review_status"] == contract.EVIDENCE_BLOCKED


def test_a_finished_subagent_gate_is_history_a_later_retry_starts_a_clean_gate(env):
    from office import contract, self_review_subagents as srs

    env.hide_agy = True
    _start(env, gear="S", executor=EXECUTOR, code_reviewer=[{"reply": APPROVED}], convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    con, run, tasks = _subagent_ctx(env)
    gate_ids = []

    def reviewer(con_, run_, gate, role, brief, **kw):
        gate_ids.append(gate["id"])
        return {"status": contract.UNAVAILABLE, "verdict": None, "parsed": None, "summary": "no route"}

    try:
        srs.audit_scope(con, run, tasks, Path(env.repo), "f" * 40, reviewer=reviewer)
        srs.audit_scope(con, run, tasks, Path(env.repo), "f" * 40, reviewer=reviewer)
        rows = _subagent_rows(con, "f" * 40)
    finally:
        con.close()
    assert len(rows) == 2 and gate_ids[0] != gate_ids[1], (
        "the exhausted chain's failures live on the finished gate: the retry's routes are all eligible")


def test_a_subagent_that_ran_and_left_a_bad_report_stays_blocked(env):
    from office import contract, self_review_subagents as srs

    def reviewer(con, run, gate, role, brief, **kw):
        return {"status": contract.EVIDENCE_BLOCKED, "verdict": None, "parsed": None, "summary": "bad", "dispatch_id": "D1"}

    _start(env, gear="S", executor=EXECUTOR, code_reviewer=[{"reply": APPROVED}], convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    con, run, tasks = _subagent_ctx(env)
    try:
        out = srs.audit_scope(con, run, tasks, Path(env.repo), "e" * 40, reviewer=reviewer,
                              job={"id": "J", "kind": "convergence_review", "payload": {"gate_id": "G"}})
    finally:
        con.close()
    assert out["status"] == contract.EVIDENCE_BLOCKED


def test_a_subagents_fallback_job_names_the_owning_gate_so_the_owner_reruns(env):
    from office import gates, state
    _start(env, gear="S", executor=EXECUTOR, code_reviewer=[{"reply": APPROVED}], convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    con, run, _ = _subagent_ctx(env)
    owner = {"id": "Jconv", "kind": "convergence_review", "payload": {"gate_id": "Gconv", "office_version": "x"}}
    try:
        job_id = gates.queue_review_fallback(con, run, {"id": "Gsub", "task_id": "T1"}, owner, "no route", redispatch=True,
                                             dispatch_id="Dx")
    finally:
        con.close()
    (job,) = [j for j in _jobs(env) if j["id"] == job_id]
    assert job["payload"]["gate_id"] == "Gconv", "the owner re-runs on its own gate, not the subagent's"
    assert job["payload"]["origin"]["kind"] == "convergence_review"
    assert job["payload"]["fallback"]["chain_epoch"] == 1 and job["payload"]["fallback"]["epoch"] == 1


# ------------------------------------------------------------------ a fresh chain starts clean

def _job_with(fallback, **payload):
    return {"id": "J", "kind": "plan_review", "payload": {"gate_id": "G", "review_fallback": fallback, **payload}}


def _capture_attempts(monkeypatch):
    from office import gates
    seen = []

    def attempt(con, run, gate, role, brief, **kw):
        seen.append(kw)
        return {"status": "COMPLETED", "verdict": "APPROVED"}, None

    monkeypatch.setattr(gates, "_review_attempt", attempt)
    return seen


def test_a_redispatch_epoch_drops_the_jobs_resume_from_and_a_commit_rerun_keeps_its_own(monkeypatch):
    from office import gates
    seen = _capture_attempts(monkeypatch)
    gate = {"id": "G"}
    # the plan job's own resume_from (a plan-defect redirect with --reviewer same) is for its first round
    gates.run_reviewer(None, {}, gate, "plan_reviewer", "b", cwd=Path("."), resume_from="Dold", job=_job_with({}))
    # the fresh chain of a redispatch starts from routing, not from the exhausted chain's dispatch
    redispatched = {"epoch": 1, "chain_epoch": 1, "redispatches": 1, "resume_from": None}
    gates.run_reviewer(None, {}, gate, "plan_reviewer", "b", cwd=Path("."), resume_from="Dold",
                       job=_job_with(redispatched, resume_from="Dold"))
    # a REVIEWED_COMMIT re-run continues the reviewer it names, in the same chain
    recheck = {"epoch": 1, "chain_epoch": 0, "commit_retries": 1, "resume_from": "Dnew", "correction": "c"}
    gates.run_reviewer(None, {}, gate, "plan_reviewer", "b", cwd=Path("."), resume_from="Dold", job=_job_with(recheck))
    assert [kw["resume_from"] for kw in seen] == ["Dold", None, "Dnew"]
    assert [(kw["epoch"], kw["chain_epoch"]) for kw in seen] == [(0, 0), (1, 1), (1, 0)]


def test_a_call_outside_the_job_flow_starts_a_fresh_chain(monkeypatch):
    from office import gates
    seen = _capture_attempts(monkeypatch)
    monkeypatch.setattr(gates, "review_attempts", lambda con, gate_id: [
        {"id": "D0", "triple": "t", "harness": "h", "epoch": 0, "failure": {"class": "launch", "reason": "x"}},
        {"id": "D1", "triple": "u", "harness": "h", "epoch": 1, "failure": None}])
    gates.run_reviewer(None, {}, {"id": "G"}, "code_reviewer", "b", cwd=Path("."))
    assert (seen[0]["epoch"], seen[0]["chain_epoch"]) == (2, 2), "no earlier call's failure is excluded"


def test_only_the_current_chains_failures_are_excluded(env, monkeypatch):
    from office import gates
    _start(env, executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    gate = _lane(env)
    con, run, _ = _subagent_ctx(env)
    failed = {"id": "D0", "triple": "codex@1/spent@low", "harness": "codex", "epoch": 0,
              "failure": {"class": "wall", "reason": "quota", "harness_wide": True}}
    monkeypatch.setattr(gates, "review_attempts", lambda con_, gate_id: [failed])
    excluded = []

    def route_role(con_, config, run_, role, **kw):
        excluded.append(kw.get("exclude"))
        return {"status": "none"}

    monkeypatch.setattr(gates.candidates, "route_role", route_role)
    try:
        for epoch, chain_epoch in ((1, 1), (1, 0)):
            gates._review_attempt(con, run, gate, "code_reviewer", "b", cwd=Path(env.repo), plan_review=False,
                                  visual=False, images=None, include_dirs=None, exclude=None, kind=None,
                                  resume_from=None, prefer_not=None, epoch=epoch, chain_epoch=chain_epoch)
    finally:
        con.close()
    fresh, same = excluded
    assert not fresh, "a fresh chain starts clean: the spent chain's routes and walled harness may have recovered"
    assert same == {"codex@1/spent@low", "harness:codex"}, "a commit re-run stays in its chain"


def test_an_integration_review_with_no_verdict_queues_the_bounded_fallback_instead_of_finalizing(env, monkeypatch):
    from office import db, gates, integration, state
    _start(env, executor=EXECUTOR, convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])
    commit = env.git("rev-parse", "HEAD").strip()
    ctx = {"key": "k", "commit": commit, "base": commit, "branch": "office/x/integration",
           "integ_rev": {"id": "I" + commit[:7], "commit_sha": commit, "tree_sha": commit, "base_commit": commit,
                         "dispatch_id": None},
           "run_checks": [], "executed": [], "checks_gate_id": None, "results": {"checks": "PASS"}}
    with db.transaction(con):
        con.execute("INSERT INTO gates(id, run_id, subject, revision_id, plan_version, kind, input_key, status, created_at) "
                    "VALUES('GINT', ?, 'integration', ?, ?, 'integration_review', ?, 'running', '2026-01-01')",
                    (run["id"], ctx["integ_rev"]["id"], run["plan_version"], f"integration:{commit}"))
    answers = [{"fallback_queued": "Jq"}, {"verdict": "UNAVAILABLE", "summary": "no route", "parsed": None}]
    calls = []

    def reviewer(con_, run_, gate, role, brief, **kw):
        calls.append((role, kw["job"]["kind"], kw["job"]["payload"]["gate_id"]))
        return answers.pop(0)

    monkeypatch.setattr(gates, "run_reviewer", reviewer)
    monkeypatch.setattr(gates, "detached_checkout", lambda *a, **k: Path(env.repo))
    monkeypatch.setattr(gates, "remove_checkout", lambda *a, **k: None)
    monkeypatch.setattr(integration.briefs, "code_review_brief", lambda *a, **k: "brief")
    job = {"id": "Jint", "kind": "integration_review", "payload": {"gate_id": "GINT"}}
    queued = integration._review_composed(con, run, job, "GINT", ctx)
    assert queued == {"fallback": "Jq"} and calls == [("integration_reviewer", "integration_review", "GINT")]
    assert con.execute("SELECT status FROM gates WHERE id='GINT'").fetchone()[0] == "running", "the gate stays open"
    landing = state.get_run(con, run["id"])["landing"]["integration"]
    assert landing["status"] == "verifying" and landing["review_ctx"]["commit"] == commit
    # the bound is spent: the gate finalizes UNAVAILABLE as before, and the pending review state is cleared
    final = integration._review_composed(con, state.get_run(con, run["id"]), job, "GINT", ctx)
    assert final["integration_review"] == "UNAVAILABLE"
    row = con.execute("SELECT status, verdict FROM gates WHERE id='GINT'").fetchone()
    assert (row[0], row[1]) == ("done", "UNAVAILABLE")
    landing = state.get_run(con, run["id"])["landing"]["integration"]
    assert landing["status"] == "unavailable" and landing["review_ctx"] is None
    con.close()


def test_a_subagent_that_never_answers_runs_through_the_owning_jobs_fallbacks_then_blocks_convergence(env):
    _start(env, gear="S", executor=EXECUTOR, code_reviewer=[EMPTY], convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    subagents = _q(env, "SELECT id, review_status FROM gates WHERE kind='self_review_subagent'")
    assert len(subagents) == 1, "every fallback continued on the one subagent gate"
    assert subagents[0]["review_status"] == "EVIDENCE_BLOCKED", "the spent bound blocks as before"
    owner = _lane(env)
    jobs = _jobs(env)
    assert subagents[0]["id"] in {j["payload"]["gate_id"] for j in jobs} <= {subagents[0]["id"], owner["id"]}, \
        "the subagent's fallbacks re-run it on its own gate, supervised apart from the lane's convergence job"
    assert {j["payload"]["origin"]["kind"] for j in jobs} == {"convergence_review"}
    assert len([e for e in _redispatches(env)]) == 1, "the default bound is one re-dispatch per gate"
    assert _attempts(env, subagents[0]["id"]) and not _attempts(env, owner["id"]), "convergence never ran without its evidence"
    assert int(owner["round"]) == 1
