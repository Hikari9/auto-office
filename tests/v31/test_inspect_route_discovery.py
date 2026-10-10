"""#494 T5: what `office inspect route`, `inspect learner`, the plan diagram and `status` disclose about discovery.

Decisions come from T3's isolated world (one scripted fake `codex`, a real runs.db, no model).
Trial rows are seeded the way T4's dispatch writes them. The last tests drive the `office` CLI
through the `env` fixture, so the suite's tiering marks them integration: they run with `--all`.
"""
import json
import re

import pytest

from office import candidates, inspect_cmd, plan_view, route_learning, route_probe, route_policy, scoring
from conftest import approved_run
from test_route_discovery_consumer_contract import World, categories as decision_categories
from test_route_learning_discovery import ALLOC, DIGEST, FALLBACK, Seed, at, probe_key

TRIPLE = "codex@2/gpt-6.1-sol@high"


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    w.con.execute("INSERT INTO runs(id, playbook, phase, risk_json) VALUES('run-A','Change','executing','{}')")
    yield w


def role_view(world, role="executor"):
    return inspect_cmd._route(world.con, world.run, role)


def block(lines, heading):
    """The indented lines under `heading`."""
    start = next(i for i, line in enumerate(lines) if line.startswith(heading))
    out = []
    for line in lines[start + 1:]:
        if not line.startswith(" "):
            break
        out.append(line.strip())
    return out


def listed(lines):
    """`candidates by category` as {candidate: category}."""
    return {row.split()[1]: row.split()[0] for row in block(lines, "candidates by category")}


# ------------------------------------------------------------------ inspect route <role>

def test_a_cold_role_view_names_the_probe_candidate_its_caps_and_the_fallback(world):
    res = role_view(world)
    lines = res.lines
    head = next(line for line in lines if line.startswith("discovery intent"))
    disc = res.data["discovery"]
    assert disc["intent"] == "probe" and f"candidate {disc['candidate']}" in head and f"fallback {disc['fallback']}" in head
    assert "blocked" not in head
    assert any(f"primary {res.data['selected']}" in line and "no fresh exact probe on record" in line for line in lines)
    assert "  caps: probes 0/2 used, 2 left | trials 0/1 used, 1 left | rolling 0/1 of the last 1 decisions" in lines
    cats = listed(lines)
    assert cats[disc["candidate"]] == "probe-candidate"
    assert set(cats.values()) == {"probe-candidate", "untried"}
    assert sum(c == "untried" for c in cats.values()) == 4  # the four other efforts of the same model
    assert "also out of the pool:" in "\n".join(lines)  # not installed, no profile: counted, not listed
    assert {c["category"] for c in res.data["categories"]} >= {"probe-candidate", "untried"}


def test_a_model_the_user_denied_or_marked_overkill_is_listed_with_the_tier_that_set_it(world):
    world.write_policy("user", denied=["codex/gpt-6.1-sol@low"],
                       overkill=["{route: codex/gpt-6.1-sol@max, roles: [executor]}"])
    lines = role_view(world).lines
    cats = listed(lines)
    assert cats["codex@0/gpt-6.1-sol@low"] == "denied" and cats["codex@0/gpt-6.1-sol@max"] == "overkill"
    assert any(line.strip().startswith("denied") and "denied by user routing.user_policy.denied_models" in line
               for line in lines)
    assert "preference source tiers: denied_models user | overkill_rules user | budget ceiling shipped" in lines
    world.write_policy("repo", denied=["codex/gpt-6.1-sol@low"])
    # the repo tier overrides the user tier for the key it sets; the overkill rules still come from the user tier
    assert "preference source tiers: denied_models repo | overkill_rules user | budget ceiling shipped" \
        in role_view(world).lines


@pytest.mark.parametrize("mode,category", [("unsupported_effort", "unsupported"), ("auth", "probe-failed"),
                                           ("transient", "probe-failed"), ("malformed", "probe-failed")])
def test_a_failed_probe_is_unsupported_only_for_an_unsupported_model_effort(world, mode, category):
    world.script(mode=mode)
    world.preflight("high")
    cats = listed(role_view(world).lines)
    assert cats[f"codex@0/gpt-6.1-sol@high"] == category
    assert {c for route, c in cats.items() if route != "codex@0/gpt-6.1-sol@high"} <= {"untried", "probe-candidate"}


def test_a_passing_probe_that_is_not_being_tried_now_is_probe_passed(world):
    world.preflight("high")
    lines = role_view(world).lines
    assert listed(lines)["codex@0/gpt-6.1-sol@high"] == "probe-passed"


def test_an_in_flight_probe_is_probe_pending(world):
    first = world.decide("high")
    disc = first["discovery"]
    world.con.execute(
        "INSERT INTO route_probes(key, harness, harness_version, adapter_hash, profile, invocation_model_id, effort, result, "
        "probed_at, run_id, attempt_id) VALUES(?,?,?,?,?,?,?,'pending',?,?,?)",
        (disc["probe_key"], "codex", "0.162.0", "x", "worker", "gpt-6.1-sol", "high", route_probe._utcnow().isoformat(),
         "run-A", "Apending"))
    after = world.decide("high")
    cats = {c["candidate"]: c["category"] for c in inspect_cmd._categories(after)}
    assert cats[disc["candidate"]] == "probe-pending"
    assert decision_categories(after)[disc["candidate"]] == "untried"  # the router's own word; inspect refines it


def test_a_candidate_whose_probe_passed_and_whose_gates_hold_is_trial_eligible_with_its_fallback(world):
    first, attempt, _, second = world.preflight("high")
    cats = inspect_cmd._categories(second)
    (eligible,) = [c for c in cats if c["category"] == "trial-eligible"]
    assert eligible["candidate"] == second["discovery"]["candidate"] and second["discovery"]["fallback"] in eligible["reason"]
    lines = inspect_cmd._discovery_lines(second, cats, world.config, [])
    head = next(line for line in lines if line.startswith("discovery intent"))
    assert "intent trial" in head and f"fallback {second['discovery']['fallback']}" in head
    assert f"  primary {second['selected']} | probe: pass, fresh, probed " in "\n".join(lines)
    assert "  caps: probes 1/2 used, 1 left | trials 0/1 used, 1 left" in "\n".join(lines)
    assert listed(lines)[second["discovery"]["candidate"]] == "trial-eligible"


def test_a_blocked_decision_names_why_it_was_blocked(world):
    world.script(mode="unsupported_effort")
    _, _, _, blocked = world.preflight("high")
    lines = inspect_cmd._discovery_lines(blocked, inspect_cmd._categories(blocked), world.config, [])
    head = next(line for line in lines if line.startswith("discovery intent"))
    assert "intent none | blocked: probe-failed:unsupported-model-effort" in head
    assert f"fallback {blocked['slate'][1]['route']}" in head  # nothing is tried: the primary's own fallback stands


def test_trial_caps_show_what_is_used_and_what_remains(world):
    seed = Seed(world.con)
    seed.task("run-A", "T1", status="running")
    seed.dispatch("run-A", "D1", "T1")
    seed.trial("A1", "run-A", "T1", "D1")
    lines = role_view(world).lines
    (caps,) = [line for line in lines if line.startswith("  caps:")]
    assert caps == "  caps: probes 0/2 used, 2 left | trials 1/1 used, 0 left | rolling 1/1 of the last 1 decisions"


def test_discovery_off_keeps_a_role_view_free_of_discovery_sections(world):
    off = world.policy(enabled=False)
    res = inspect_cmd._route(world.con, world.make_run(off), "executor")
    text = "\n".join(res.lines)
    assert "discovery intent" not in text and "trials:" not in text and "attempts (" not in text
    assert "discovery" not in res.data and "trials" not in res.data
    assert inspect_cmd._discovery_lines({}, [], off, []) == []  # nothing to say: not even a blank line


# ------------------------------------------------------------------ trials and per-attempt history

def seeded_trials(world):
    """T1: a trial that launched and was accepted. T2: a trial whose launch failed, recovered by the fallback."""
    seed = Seed(world.con)
    seed.task("run-A", "T1", accepted="V1")
    seed.dispatch("run-A", "D1", "T1")
    seed.revision("run-A", "V1", "D1", "T1")
    seed.trial("A1", "run-A", "T1", "D1")
    seed.task("run-A", "T2", accepted="V3")
    seed.dispatch("run-A", "D2", "T2", term="nonzero", exit_code=2)
    seed.trial("A2", "run-A", "T2", "D2", ending="fell-back", reason_class="transient")
    seed.dispatch("run-A", "D3", "T2", day=2, harness="claude", model="claude-sonnet-5-5")
    seed.revision("run-A", "V3", "D3", "T2")
    return seed


def test_the_role_view_lists_each_trial_with_its_fallback_recovery_and_outcome(world):
    seeded_trials(world)
    res = role_view(world)
    rows = {t["attempt_id"]: t for t in res.data["trials"]}
    assert rows["A1"]["outcome"] == "accepted, not yet recorded" and rows["A1"]["recovered_launch"] is False
    assert rows["A2"]["recovered_launch"] is True and rows["A2"]["status"] == "fell-back"
    assert rows["A2"]["outcome"] == "none: ended before any work (transient)"
    block_ = block(res.lines, "trials:")
    assert any(row.startswith("A1 T1 dispatch D1") and f"fallback {FALLBACK}" in row and "outcome accepted, not yet recorded" in row
               for row in block_)
    assert any(row.startswith("A2 T2 dispatch D2") and row.endswith("recovered launch: the fallback ran") for row in block_)
    # once the learner has observed the gate result, the recorded outcome is what is shown
    from office import db
    with db.transaction(world.con):
        route_learning.record_trial_outcomes(world.con)
    rows = {t["attempt_id"]: t for t in role_view(world).data["trials"]}
    assert rows["A1"]["outcome"] == "accepted (recorded)"


def test_inspecting_never_writes_an_event_or_a_trial_row(world):
    seeded_trials(world)
    before = (world.events(), [dict(r) for r in world.con.execute("SELECT * FROM route_trials")])
    role_view(world)
    inspect_cmd._route(world.con, world.run, "T1")
    inspect_cmd._learner(world.con, world.run)
    assert (world.events(), [dict(r) for r in world.con.execute("SELECT * FROM route_trials")]) == before


def test_the_task_view_renders_each_attempt_with_its_audit_fields(world):
    seeded_trials(world)
    lines = inspect_cmd._route(world.con, world.run, "T2").lines
    text = "\n".join(lines)
    assert any(row.startswith("A2 T2 dispatch D2") for row in block(lines, "trials:"))
    assert "A1 T1" not in text  # the other task's attempt is not here
    assert [line for line in lines if line.startswith("  attempt ")] == [
        f"  attempt A2 run run-A plan p3 dispatch D2 origin preflight digest {DIGEST[:19]}"]
    head = next(line for line in lines if line.startswith("  attempt A2"))
    assert head == f"  attempt A2 run run-A plan p3 dispatch D2 origin preflight digest {DIGEST[:19]}"
    attempt = lines[lines.index(head):]
    assert attempt[1] == f"    fingerprint {probe_key()}"
    assert attempt[2] == "    route codex@2/gpt-6.1-sol@high | reason: discovery: untried candidate"
    events = [line.split()[1:] for line in attempt[3:] if re.match(r"\s+\d\d:\d\d:\d\d ", line)]
    kinds = [e[0] for e in events]
    assert kinds == ["probe-reserved", "probe-result", "dispatch-linked", "trial-reserved", "trial-launched",
                     "trial-launch-failed", "trial-fell-back"]
    assert events[0][1:] == ["freshness", "none", "reserved"] and events[1][1:] == ["freshness", "fresh-run", "pass"]
    assert events[5][1:] == ["freshness", "fresh-run", "launch-failed", "(transient)"]


def test_an_unbound_manual_probe_shows_dispatch_none(world):
    first = world.decide("high")
    cand = world.candidate(first, first["discovery"]["candidate"])
    attempt_id = route_policy.new_attempt_id()
    outcome = route_probe.ensure(world.con, None, cand, attempt_id=attempt_id, context={"origin": "manual"})
    assert outcome["result"] == "pass"
    lines = role_view(world).lines
    head = next(line for line in lines if line.startswith(f"  attempt {attempt_id}"))
    assert head.startswith(f"  attempt {attempt_id} run none plan none dispatch none origin manual digest sha256:")
    row = lines[lines.index(head) + 3:]
    assert any("probe-result" in r and "fresh-run" in r and r.rstrip().endswith("pass") for r in row)
    assert any("reason: manual: office doctor --probe-route" in line for line in lines)


def test_a_cache_hit_attempt_names_the_attempt_that_proved_the_route(world):
    first, attempt, _, _ = world.preflight("high")
    later = world.decide("high")
    second, _ = world.ensure(later)
    text = "\n".join(role_view(world).lines)
    assert f"probe-cache-hit" in text and f"cache-hit:pass from {attempt}" in text and f"attempt {second}" in text


# ------------------------------------------------------------------ decisions recorded before this change

def test_a_decision_recorded_before_this_change_renders_exactly_as_before(world):
    """No discovery block, no events, no trial rows: the task view is the lines it always printed."""
    audit = {"phase": "dispatch", "task_id": "T1", "role": "executor", "decision_hash": "sha256:old", "policy_version": "p",
             "learner_version": "l", "disclosure_json": {}, "dispatch": {"source": "router", "fallbacks_taken": []}}
    world.con.execute("INSERT INTO tasks(run_id, id, title, role, scope_json, depends_json, accept_json, checks_json, status, "
                      "introduced_plan_version, contract_version, acceptance_version, created_at, updated_at) "
                      "VALUES('run-A','T1','T1','executor','[]','[]','[]','[]','running',1,1,1,'t','t')")
    route_learning.ensure_schema(world.con)
    world.con.execute("INSERT INTO route_audit(id, run_id, task_id, role, phase, plan_version, decision_hash, policy_version, "
                      "learner_version, primary_route, dispatched_route, explored, disclosure_json, created_at) "
                      "VALUES('RA1','run-A','T1','executor','dispatch',3,'sha256:old','p','l','a/b@1','a@2/b@high',0,?,?)",
                      (json.dumps({"dispatch": {"source": "router", "fallbacks_taken": [
                          {"route": "x@1/y@low", "reason": "quota"}]}}), "2026-10-01T00:00:00+00:00"))
    res = inspect_cmd._route(world.con, world.run, "T1")
    assert res.lines == ["dispatch 2026-10-01T00:00 -> a@2/b@high (router) after fallback: x@1/y@low: quota"]
    assert set(res.data) == {"task", "audits", "legacy", "effective_route", "route_changes"}


def test_a_learner_view_with_no_trials_has_no_trial_section(world):
    res = inspect_cmd._learner(world.con, world.run)
    assert res.lines == ["learner route-learner-2-task: 0 dispatch outcomes, 0 task-route episodes"]
    assert res.data["trial_evidence"] == {} and res.data["unsupported"] == {}


# ------------------------------------------------------------------ inspect learner

def test_the_learner_view_shows_trial_evidence_apart_from_trust(world):
    seed = Seed(world.con)
    for i, ending in enumerate(("launched", "launched", "launch-failed"), start=1):
        rid, did = f"R{i}", f"D{i}"
        seed.run(rid)
        seed.task(rid, "T1", accepted=f"V{i}" if ending == "launched" else "VF")
        seed.dispatch(rid, did, "T1", term="success" if ending == "launched" else "nonzero",
                      exit_code=0 if ending == "launched" else 2)
        if ending == "launched":
            seed.revision(rid, f"V{i}", did, "T1")
        seed.trial(f"A{i}", rid, "T1", did, ending=ending, reason_class="unsupported-model-effort" if ending != "launched" else None,
                   effort="high")
    # the fallback of R3 landed it
    seed.dispatch("R3", "DF", "T1", day=2, harness="claude", model="claude-sonnet-5-5")
    seed.revision("R3", "VF", "DF", "T1")
    seed.event("probe-result", "Amed", None, None, None, effort="medium", outcome="fail", reason_class="transient",
               origin="manual")
    trust_before = scoring.evaluate_trust_state(world.con, TRIPLE)
    res = inspect_cmd._learner(world.con, world.run)
    lines = res.lines
    assert "trial evidence (quality only; trials never change adapter trust):" in lines
    row = next(line for line in lines if "trial dispatches landed" in line)
    assert row.startswith("  codex/gpt-6.1-sol@high") and "2/3 trial dispatches landed" in row and "1 not the model's" in row and "failed on the route" not in row
    assert row.endswith("| trust valid-unverified")
    assert not any("trial episodes" in line for line in lines)
    assert any(line.startswith("  unsupported codex/gpt-6.1-sol@high (this exact effort only; attempt A3)") for line in lines)
    assert not any("gpt-6.1-sol@medium" in line for line in lines if "unsupported" in line)
    assert res.data["trial_evidence"]["codex/gpt-6.1-sol@high"] == {
        "dispatches": 3, "landed": 2, "not_the_model": 1, "failed_on_route": 0, "trust": "valid-unverified"}
    assert scoring.evaluate_trust_state(world.con, TRIPLE) == trust_before  # reading it changed nothing


# ------------------------------------------------------------------ plan diagram

def pv_task(**entry):
    base = {"title": "Build it", "depends": [], "wave": 1, "base": None, "needs": [], "route": "codex/gpt-6.1-sol@high",
            "dispatched_route": None, "why": None, "gates": [], "lane": None, "converge": [], "visual_gate": False,
            "review": None, "review_why": None}
    return {**base, **entry}


def render(**entry):
    pv = {"tasks": {"T1": pv_task(**entry)}, "end_state": "ask", "deploy": {}, "prs": {"enabled": False, "reason": "off"}}
    run = {"gates": {}, "contract": None}
    return plan_view.render(run, 1, pv)


def slate(*routes):
    return [{"rank": rank, "route": route, "label": route, "utility": 0.5, "reason": "why", "strength": "s", "weakness": "w"}
            for rank, route in zip(("PRIMARY", "FALLBACK 1"), routes)]


def test_the_diagram_names_a_trial_route_as_a_trial_with_its_fallback():
    lines = render(slate=slate("codex/gpt-6.1-sol@high", FALLBACK),
                   discovery={"intent": "trial", "candidate": "codex/gpt-6.1-sol@high", "fallback": FALLBACK})
    assert f"ROUTING  (trial route codex/gpt-6.1-sol@high, fallback {FALLBACK})" in "\n".join(lines)


def test_the_diagram_says_a_probe_candidate_is_not_yet_a_trial():
    lines = render(slate=slate(FALLBACK),
                   discovery={"intent": "probe", "candidate": "codex/gpt-6.1-sol@high", "fallback": FALLBACK})
    assert (f"ROUTING  (probe candidate codex/gpt-6.1-sol@high: a trial only if a fresh exact probe passes, "
            f"fallback {FALLBACK})") in "\n".join(lines)


def test_a_dispatched_trial_is_shown_as_a_trial_with_its_fallback():
    lines = render(dispatched_route=TRIPLE, trial={"route": TRIPLE, "fallback": FALLBACK, "status": "launched"})
    assert f"dispatched: {TRIPLE} (trial, fallback {FALLBACK})" in "\n".join(lines)
    unslated = render(discovery={"intent": "trial", "candidate": "x", "fallback": FALLBACK})
    assert f"codex/gpt-6.1-sol@high (trial, fallback {FALLBACK})" in "\n".join(unslated)


def test_a_diagram_entry_recorded_before_this_change_renders_as_before():
    head = f"  {'T1  Build it':<20}"
    assert render(slate=slate("codex/gpt-6.1-sol@high")) == [
        "plan p1 diagram (routes are previews until dispatched; actual route shown after dispatch)", "",
        "wave 1", f"{head}  off base", "      ROUTING", "      PRIMARY     codex/gpt-6.1-sol@high  why",
        "                  + s   - w", "", "checkpoints: wave 1 {T1 accepted} -> integration review -> "
        "handoff PR (task PRs off: off)", "end state: ask"]
    assert render(dispatched_route=TRIPLE)[3] == f"{head}  dispatched: {TRIPLE}  off base"
    assert render()[3] == f"{head}  codex/gpt-6.1-sol@high  off base"


def test_the_preview_tracks_a_live_trial_by_its_dispatch(world):
    seed = Seed(world.con)
    seed.task("run-A", "T1", status="running")
    seed.dispatch("run-A", "D1", "T1")
    seed.trial("A1", "run-A", "T1", "D1")
    world.con.execute("UPDATE tasks SET current_dispatch_id='D1' WHERE id='T1'")
    routes = {"T1": TRIPLE}
    assert plan_view._dispatched_trials(world.con, "run-A", routes) == {
        "T1": {"route": TRIPLE, "fallback": FALLBACK, "status": "launched"}}
    world.con.execute("UPDATE route_trials SET status='launch-failed'")
    assert plan_view._dispatched_trials(world.con, "run-A", routes) == {}  # a failed trial is no longer the route
    assert plan_view._dispatched_trials(world.con, "run-A", {}) == {}


# ------------------------------------------------------------------ status

def test_status_notes_name_a_live_trial_with_its_fallback(world):
    from office import guide, state
    seed = Seed(world.con)
    seed.task("run-A", "T1", status="running")
    seed.dispatch("run-A", "D1", "T1")
    seed.trial("A1", "run-A", "T1", "D1")
    world.con.execute("UPDATE tasks SET current_dispatch_id='D1' WHERE id='T1'")
    tasks = state.tasks(world.con, "run-A")
    assert guide.trial_notes(world.con, tasks) == [f"T1 runs a discovery trial of {TRIPLE}, fallback {FALLBACK}"]
    assert guide.effective_routes(world.con, world.run, tasks) == {"T1": f"{TRIPLE} (trial, fallback {FALLBACK})"}
    world.con.execute("UPDATE route_trials SET status='accepted'")  # the trial is over
    assert guide.trial_notes(world.con, tasks) == []
    assert "trial" not in guide.effective_routes(world.con, world.run, tasks).get("T1", "")


def test_the_next_line_and_routes_line_name_a_trial_through_the_cli(env):
    approved_run(env, executor=[{"submit": True}])
    con = env.con()
    run_id = con.execute("SELECT id FROM runs").fetchone()[0]
    con.execute("INSERT INTO dispatches(id, run_id, role, task_id, triple, harness, model, effort, started_at, status) "
                "VALUES('Dt','%s','executor','T1',?,'codex','gpt-6.1-sol','high',?,'running')" % run_id,
                (TRIPLE, at(1)))
    con.execute("UPDATE tasks SET status='running', current_dispatch_id='Dt' WHERE id='T1'")
    con.execute("INSERT INTO route_trials(id, run_id, task_id, dispatch_id, role, route, probe_key, fallback_route, "
                "policy_digest, reason, status, created_at, updated_at) VALUES('At',?,'T1','Dt','executor',?,?,?,?,?,"
                "'launched','t','t')", (run_id, TRIPLE, probe_key(), FALLBACK, DIGEST, "discovery: untried candidate"))
    data = env.ojson("status")[1]["data"]
    assert data["routes"]["T1"] == f"{TRIPLE} (trial, fallback {FALLBACK})"
    assert f"T1 runs a discovery trial of {TRIPLE}, fallback {FALLBACK}" in data["next"]
    assert data["next"].startswith("exceptions only; office status (")
    con.execute("UPDATE route_trials SET status='accepted'")
    data = env.ojson("status")[1]["data"]
    assert "trial" not in data["next"] and "trial" not in data["routes"]["T1"]


def test_a_later_retry_that_landed_is_not_credited_to_the_trial(world):
    """The trial's launch failed before work; a plain retry on the same route landed the task."""
    seed = Seed(world.con)
    seed.run("R7")
    seed.task("R7", "T1", accepted="V2")
    seed.dispatch("R7", "D1", "T1", term="nonzero", exit_code=2)
    seed.trial("A7", "R7", "T1", "D1", ending="launch-failed", reason_class="transient")
    seed.dispatch("R7", "D2", "T1", day=2)  # same route, not a trial
    seed.revision("R7", "V2", "D2", "T1")
    res = inspect_cmd._learner(world.con, world.run)
    assert res.data["trial_evidence"]["codex/gpt-6.1-sol@high"]["landed"] == 0
    assert res.data["trial_evidence"]["codex/gpt-6.1-sol@high"]["not_the_model"] == 1


# ------------------------------------------------------------------ one trial in each state

def _fallback_lands(seed):
    seed.dispatch("run-A", "D2", "T1", day=2, harness="claude", model="claude-sonnet-5-5")
    seed.revision("run-A", "V2", "D2", "T1")


def _launch_failed(ending):
    def build(seed):
        seed.task("run-A", "T1", accepted="V2")
        seed.dispatch("run-A", "D1", "T1", term="nonzero", exit_code=2)
        seed.trial("A1", "run-A", "T1", "D1", ending=ending, reason_class="transient")
        _fallback_lands(seed)
    return build


def _abandoned(seed):
    seed.task("run-A", "T1", accepted="V2")
    seed.dispatch("run-A", "D1", "T1", term="nonzero", exit_code=2)
    seed.event("probe-result", "A1", "run-A", "T1", None, outcome="pass", origin="preflight")
    seed.event("trial-reserved", "A1", "run-A", "T1", "D1", outcome="reserved")
    seed.event("trial-abandoned", "A1", "run-A", "T1", "D1", outcome="abandoned", origin="recovery")
    seed.con.execute("INSERT INTO route_trials(id, run_id, task_id, dispatch_id, role, route, probe_key, fallback_route, "
                     "policy_digest, reason, status, created_at, updated_at) VALUES('A1','run-A','T1','D1','executor',?,?,?,?,"
                     "'r','abandoned','t','t')", (TRIPLE, probe_key(), FALLBACK, DIGEST))
    _fallback_lands(seed)


def _rejected(seed):
    seed.task("run-A", "T1", accepted="V2")
    seed.dispatch("run-A", "D1", "T1")
    seed.revision("run-A", "V1", "D1", "T1")
    seed.finding("run-A", "D1", "T1", "V1", "code_review")
    seed.trial("A1", "run-A", "T1", "D1")
    _fallback_lands(seed)


def _accepted(seed):
    seed.task("run-A", "T1", accepted="V1")
    seed.dispatch("run-A", "D1", "T1")
    seed.revision("run-A", "V1", "D1", "T1")
    seed.trial("A1", "run-A", "T1", "D1")


def _in_flight(seed):
    seed.task("run-A", "T1", status="running")
    seed.dispatch("run-A", "D1", "T1")
    seed.trial("A1", "run-A", "T1", "D1")


@pytest.mark.parametrize("build,outcome,recovered", [
    (_launch_failed("launch-failed"), "none: ended before any work (transient)", True),
    (_launch_failed("fell-back"), "none: ended before any work (transient)", True),
    (_abandoned, "none: abandoned", False),
    (_rejected, "not accepted, not yet recorded", False),
    (_accepted, "accepted, not yet recorded", False),
    (_in_flight, "in flight", False)],
    ids=["launch-failed", "fell-back", "abandoned", "rejected", "accepted", "in-flight"])
def test_each_trial_state_reads_as_its_own_outcome(world, build, outcome, recovered):
    build(Seed(world.con))
    (row,) = inspect_cmd._trial_rows(world.con, world.run)
    assert (row["outcome"], row["recovered_launch"]) == (outcome, recovered)


def test_a_recorded_rejection_reads_as_rejected(world):
    from office import db
    _rejected(Seed(world.con))
    with db.transaction(world.con):
        route_learning.record_trial_outcomes(world.con)
    (row,) = inspect_cmd._trial_rows(world.con, world.run)
    assert row["outcome"] == "rejected (recorded)"


def test_a_trial_with_no_dispatch_and_no_fallback_reads_none(world):
    row = {"attempt_id": "A1", "task_id": "T1", "dispatch_id": None, "route": TRIPLE, "fallback": None,
           "status": "reserved", "outcome": "in flight", "recovered_launch": False}
    assert inspect_cmd._trial_lines([row]) == [
        "", "trials:", f"  A1 T1 dispatch none {TRIPLE} | fallback none | status reserved | outcome in flight"]


def test_trial_rows_are_scoped_to_this_run_role_and_task(world):
    seed = Seed(world.con)
    _accepted(seed)
    seed.event("trial-reserved", "Aworker", "run-A", "T9", None, outcome="reserved", role="worker")
    seed.run("run-B")
    seed.task("run-B", "T1", status="running")
    seed.dispatch("run-B", "D9", "T1")
    seed.trial("AB", "run-B", "T1", "D9")
    ids = lambda **kw: sorted(t["attempt_id"] for t in inspect_cmd._trial_rows(world.con, world.run, **kw))  # noqa: E731
    assert ids() == ["A1", "Aworker"]  # run-B's trial is not run-A's
    assert ids(role="executor") == ["A1"] and ids(role="worker") == ["Aworker"]
    assert ids(task_id="T1") == ["A1"]


# ------------------------------------------------------------------ preview, learner trust, caps, none-handling

@pytest.fixture
def preview_world(world, monkeypatch):
    from office import prs, state, visual
    monkeypatch.setattr(state, "current_requirements", lambda con, run_id: {"frozen": {"end_state": "ask"}})
    monkeypatch.setattr(prs, "settings", lambda con, run: {})
    monkeypatch.setattr(visual, "applicability", lambda con, run, task, files: {"status": "none"})
    monkeypatch.setattr(candidates, "probe_quota_snapshot", lambda *a, **kw: {})
    world.run = {**world.run, "gates": {}}
    return world


TASKS = [{"id": "T1", "title": "Build it", "depends": []}]


def test_a_real_preview_carries_the_probe_candidate_and_the_diagram_names_it(preview_world):
    world = preview_world
    entry = plan_view.preview(world.con, world.run, TASKS)["tasks"]["T1"]
    disc = world.decide()["discovery"]
    assert entry["discovery"]["intent"] == "probe" and entry["discovery"]["candidate"] == disc["candidate"]
    assert entry["discovery"]["fallback"] == disc["fallback"] and "trial" not in entry
    pv = {"tasks": {"T1": entry}, "end_state": "ask", "deploy": {}, "prs": {"enabled": False, "reason": "off"}}
    text = "\n".join(plan_view.render(world.run, 1, pv))
    assert f"probe candidate {disc['candidate']}: a trial only if a fresh exact probe passes, fallback {disc['fallback']}" in text


def test_a_real_preview_of_a_trial_decision_names_the_trial_and_its_fallback(preview_world, monkeypatch):
    world = preview_world
    _, _, _, second = world.preflight("high")
    monkeypatch.setattr(candidates, "route_role", lambda *a, **kw: second)
    entry = plan_view.preview(world.con, world.run, TASKS)["tasks"]["T1"]
    disc = second["discovery"]
    assert entry["discovery"] == {"intent": "trial", "candidate": disc["candidate"], "fallback": disc["fallback"]}
    pv = {"tasks": {"T1": entry}, "end_state": "ask", "deploy": {}, "prs": {"enabled": False, "reason": "off"}}
    assert f"trial route {disc['candidate']}, fallback {disc['fallback']}" in "\n".join(plan_view.render(world.run, 1, pv))


def test_a_real_preview_marks_a_task_whose_dispatch_is_a_live_trial(preview_world):
    world = preview_world
    seed = Seed(world.con)
    _in_flight(seed)
    world.con.execute("UPDATE tasks SET current_dispatch_id='D1' WHERE id='T1'")
    entry = plan_view.preview(world.con, world.run, TASKS)["tasks"]["T1"]
    assert entry["dispatched_route"] == TRIPLE
    assert entry["trial"] == {"route": TRIPLE, "fallback": FALLBACK, "status": "launched"}
    pv = {"tasks": {"T1": entry}, "end_state": "ask", "deploy": {}, "prs": {"enabled": False, "reason": "off"}}
    assert f"dispatched: {TRIPLE} (trial, fallback {FALLBACK})" in "\n".join(plan_view.render(world.run, 1, pv))


def test_the_learner_view_reads_trust_from_the_trust_state_not_a_default(world):
    seed = Seed(world.con)
    for i, kind in enumerate(("plan", "reviewer", "route"), start=1):
        rid, did = f"R{i}", f"D{i}"
        seed.run(rid)
        # a cancelled task reads as a plan change; an accepted one (through another dispatch) leaves the finding to decide
        seed.task(rid, "T1", status="cancelled" if kind != "route" else "accepted", accepted="VX" if kind == "route" else None)
        seed.dispatch(rid, did, "T1")
        seed.revision(rid, f"V{i}", did, "T1")
        seed.finding(rid, did, "T1", f"V{i}", {"plan": "brief", "reviewer": "plan", "route": "code_review"}[kind])
        seed.trial(f"A{i}", rid, "T1", did)
    db_path = world.con.execute("PRAGMA database_list").fetchone()[2]
    scoring.record_trust_act(db_path, TRIPLE, "proven", "rico", "verified by hand")
    res = inspect_cmd._learner(world.con, world.run)
    row = next(line for line in res.lines if "trial dispatches landed" in line)
    assert row.endswith("| trust proven") and res.data["trial_evidence"]["codex/gpt-6.1-sol@high"]["trust"] == "proven"
    ev = res.data["trial_evidence"]["codex/gpt-6.1-sol@high"]
    assert (ev["dispatches"], ev["landed"], ev["not_the_model"], ev["failed_on_route"]) == (3, 0, 2, 1)


@pytest.mark.parametrize("cap,text", [
    ("probes", {"used": 3, "max": 2}), ("trials", {"used": 0, "max": 0})])
def test_a_cap_that_is_over_or_zero_never_reads_negative(cap, text):
    assert "0 left" in inspect_cmd._cap_text(cap, text) and "-" not in inspect_cmd._cap_text(cap, text)


def test_a_rolling_cap_that_is_still_warming_up_says_so():
    line = inspect_cmd._cap_text("rolling", {"used": 0, "max": 0, "window": 3, "warmup": True})
    assert line == "rolling 0/0 of the last 3 decisions (warming up: too few recorded decisions to allow one)"
    assert "warming" not in inspect_cmd._cap_text("rolling", {"used": 0, "max": 1, "window": 7})


def test_a_trial_with_no_recorded_fallback_says_fallback_none(world):
    from office import guide, state
    seed = Seed(world.con)
    _in_flight(seed)
    world.con.execute("UPDATE route_trials SET fallback_route=NULL")
    world.con.execute("UPDATE tasks SET current_dispatch_id='D1' WHERE id='T1'")
    tasks = state.tasks(world.con, "run-A")
    assert guide.trial_notes(world.con, tasks) == [f"T1 runs a discovery trial of {TRIPLE}, fallback none"]
    assert guide.effective_routes(world.con, world.run, tasks) == {"T1": f"{TRIPLE} (trial, fallback none)"}
    assert "(trial, fallback none)" in "\n".join(render(dispatched_route=TRIPLE,
                                                         trial={"route": TRIPLE, "fallback": None, "status": "launched"}))


@pytest.mark.parametrize("status,live", [("reserved", True), ("launched", True), ("submitted", True), ("accepted", False),
                                         ("rejected", False), ("launch-failed", False), ("fell-back", False),
                                         ("abandoned", False)])
def test_only_an_in_flight_trial_is_live(world, status, live):
    seed = Seed(world.con)
    _in_flight(seed)
    world.con.execute("UPDATE route_trials SET status=?", (status,))
    assert (route_learning.live_trial(world.con, "D1") is not None) is live


PLAN_STACKED = """# Plan

## Requirements
done:
- add and mul exist
blast_radius: repo

## Tasks
### T1: Implement add
scope: calc.py
depends: none
checks: python3 -c "import calc; assert calc.add(2, 3) == 5"
accept:
- calc.add(2, 3) == 5
visual: none

### T2: Implement mul
scope: mul.py
depends: T1
checks: python3 -c "import mul; assert mul.mul(2, 3) == 6"
accept:
- mul.mul(2, 3) == 6
visual: none
"""


def test_the_next_line_keeps_a_dependency_hold_beside_the_trial_note(env):
    approved_run(env, plan=PLAN_STACKED)
    con = env.con()
    run_id = con.execute("SELECT id FROM runs").fetchone()[0]
    con.execute("INSERT INTO dispatches(id, run_id, role, task_id, triple, harness, model, effort, started_at, status) "
                "VALUES('Dt',?,'executor','T1',?,'codex','gpt-6.1-sol','high',?,'running')", (run_id, TRIPLE, at(1)))
    con.execute("UPDATE tasks SET status='running', current_dispatch_id='Dt' WHERE id='T1'")
    con.execute("INSERT INTO route_trials(id, run_id, task_id, dispatch_id, role, route, probe_key, fallback_route, "
                "policy_digest, reason, status, created_at, updated_at) VALUES('At',?,'T1','Dt','executor',?,?,?,?,'r',"
                "'launched','t','t')", (run_id, TRIPLE, probe_key(), FALLBACK, DIGEST))
    nxt = env.ojson("status")[1]["data"]["next"]
    assert nxt == (f"exceptions only; office status (T1 runs a discovery trial of {TRIPLE}, fallback {FALLBACK}; "
                   "T2 waits for T1 to be accepted (T1 running))")
