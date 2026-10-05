"""#300 adaptive executor/worker routing: slate, scoring, indeterminism, learning, eligibility.

Pure tests: routing.route is called with synthetic candidates and evidence
snapshots, and the learner with seeded runs.db rows. No harness, no network.
"""
import json
import sqlite3
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from office import adaptive, route_learning, routing

ROOT = Path(__file__).resolve().parents[2]
IDX = "Artificial Analysis Intelligence Index v4.3.2"


def cand(harness, model, effort="medium", *, out=2.0, inp=0.5, idx=45, tps=100.0, ttft=1000, quota=60,
         caps=("builder",)):
    return {
        "harness": harness, "harness_version": "2.0", "model_id": model, "invocation_model_id": model,
        "invocation_source": "documented:test", "effort": effort, "benchmark_indexes": {IDX: idx} if idx else {},
        "capabilities": list(caps), "price_fields": {"output_per_mtok": out, "input_per_mtok": inp},
        "speed_fields": {"output_tok_per_s": tps, "ttft_ms": ttft}, "cost": {"money_estimate": out},
        "quota": ({"status": "ok", "tightest_remaining_percent": quota} if quota is not None
                  else {"status": "unknown", "tightest_remaining_percent": None}),
    }


def ev(**routes):
    """routes: key -> (successes, failures[, attempts_mean, wall_seconds])."""
    out = {}
    for key, spec in routes.items():
        s, f = spec[0], spec[1]
        out[key.replace("__", "/").replace("_AT_", "@")] = {
            "n_effective": s + f, "n_raw": int(s + f), "runs": 4, "successes": s, "failures": f,
            "review_rounds_mean": 0.5, "attempts_mean": spec[2] if len(spec) > 2 else 1.2,
            "wall_seconds_median": spec[3] if len(spec) > 3 else None, "money_actual_median": None}
    return {"as_of": "2026-10-05T00:00:00+00:00", "routes": out, "pooling": {"prior_strength": 8},
            "context": {"role": "worker"}}


def req(cands, *, role="worker", evidence=None, seed="seed-1", cfg=None, **extra):
    return {"role": role, "playbook": "Change", "candidates": cands, "policy": {"cost_policy": "balanced"},
            "evidence": evidence or {"routes": {}, "pooling": {"prior_strength": 8}}, "routing_seed": seed,
            "adaptive_config": {"exploration": {"rate": 0.0}, **(cfg or {})}, **extra}


def key(c):
    return route_learning.candidate_key(c)


# ------------------------------------------------------------------ slate shape

def test_three_strong_candidates_form_the_slate():
    cs = [cand("claude", "opus", idx=55), cand("codex", "luna", idx=40, out=0.5), cand("agy", "flash", idx=42, out=1)]
    d = routing.route(req(cs))
    assert d["status"] == "selected" and len(d["slate"]) == 3
    assert [e["rank"] for e in d["slate"]] == ["PRIMARY", "FALLBACK 1", "FALLBACK 2"]
    assert all(e["reason"] and e["strength"] and e["weakness"] for e in d["slate"])


def test_fewer_qualifying_candidates_never_weaken_gates():
    cs = [cand("claude", "opus"), cand("codex", "luna"), cand("agy", "flash", caps=())]
    r = req(cs, role="worker", policy={"cost_policy": "balanced", "required_capabilities": ["builder"]})
    d = routing.route(r)
    assert len(d["slate"]) == 2
    assert any(x["stage"] == 3 and "agy" in x["candidate"] for x in d["rejected"])


def test_same_model_different_efforts_coexist_and_no_family_diversity_is_forced():
    cs = [cand("claude", "opus", e, idx=50 + i) for i, e in enumerate(("low", "medium", "high"))] \
        + [cand("codex", "weak", idx=20, out=1.0)]
    d = routing.route(req(cs))
    assert {e["label"] for e in d["slate"]} == {"claude/opus@low", "claude/opus@medium", "claude/opus@high"}


def test_no_qualifying_candidate_reports_status():
    d = routing.route(req([cand("claude", "opus", caps=())],
                          policy={"cost_policy": "balanced", "required_capabilities": ["builder"]}))
    assert d["status"] == "no_qualifying_candidate" and d["selected"] is None


# ------------------------------------------------------------------ cost-to-success, speed, preference

def test_raw_cheapest_does_not_win_against_better_history():
    cheap, strong = cand("codex", "luna", out=0.5, inp=0.1, idx=40), cand("claude", "sonnet", out=3, inp=0.5, idx=45)
    evidence = ev(**{key(cheap).replace("/", "__").replace("@", "_AT_"): (2, 18, 2.5),
                     key(strong).replace("/", "__").replace("@", "_AT_"): (18, 2, 1.1)})
    d = routing.route(req([cheap, strong], evidence=evidence))
    rows = {r["label"]: r for r in d["routing"]["candidates"]}
    assert d["slate"][0]["label"] == "claude/sonnet@medium"
    # Higher unit price, lower expected cost to a successful task.
    assert rows["claude/sonnet@medium"]["cost_to_success"] < rows["codex/luna@medium"]["cost_to_success"]


def test_speed_breaks_an_otherwise_competitive_comparison():
    fast, slow = cand("a", "m1", tps=300), cand("b", "m2", tps=40)
    d = routing.route(req([slow, fast], cfg={"competitive_band": 0.0}))
    assert d["slate"][0]["label"] == "a/m1@medium"
    rows = {r["label"]: r for r in d["routing"]["candidates"]}
    assert rows["a/m1@medium"]["components"]["speed"] > rows["b/m2@medium"]["components"]["speed"]


def test_preference_visibly_moves_a_close_ranking_but_cannot_rescue_an_inferior_route():
    a, b = cand("a", "m1", idx=46), cand("b", "m2", idx=45)
    base = routing.route(req([a, b], cfg={"competitive_band": 0.0}))
    assert base["slate"][0]["label"] == "a/m1@medium"
    pref = routing.route(req([a, b], cfg={"competitive_band": 0.0}, preferred_seed=[{"model_id": "m2"}]))
    assert pref["slate"][0]["label"] == "b/m2@medium"
    assert pref["routing"]["candidates"][0]["contributions"]["preference"] > 0
    weak = cand("c", "weak", idx=25, out=8)
    evidence = ev(**{key(weak).replace("/", "__").replace("@", "_AT_"): (1, 19, 3)})
    d = routing.route(req([a, weak], evidence=evidence, preferred_seed=[{"model_id": "weak"}]))
    assert d["slate"][0]["label"] == "a/m1@medium"


def test_old_money_band_cannot_eliminate_a_pricier_route_but_the_budget_ceiling_is_explicit():
    cheap, pricey = cand("codex", "luna", out=0.5, inp=0.1), cand("claude", "opus", out=20, inp=4, effort="xhigh")
    d = routing.route(req([cheap, pricey]))
    assert "claude@2/opus@xhigh" in d["qualifying"]
    assert not any("money band" in r["reason"] for r in d["rejected"])
    capped = routing.route(req([cheap, pricey], cfg={"budget_ceiling_usd": 1.0}))
    assert "claude@2/opus@xhigh" not in capped["qualifying"]
    assert any(r["stage"] == 8 and "budget ceiling" in r["reason"] for r in capped["rejected"])
    off = routing.route(req([cheap, pricey], cfg={"budget_ceiling_usd": None}))
    assert len(off["qualifying"]) == 2


def test_reviewer_routing_keeps_the_legacy_pipeline():
    cs = [cand("codex", "luna", out=0.5), cand("claude", "opus", out=20)]
    for c in cs:
        c["capabilities"] = ["review"]
    d = routing.route({"role": "plan_reviewer", "candidates": cs, "policy": {"cost_policy": "balanced"}})
    assert "slate" not in d and "routing" not in d
    assert any("money band" in r["reason"] for r in d["rejected"])


# ------------------------------------------------------------------ indeterminism

def test_close_call_draw_is_bounded_and_reproducible():
    a, b = cand("a", "m1"), cand("b", "m2")
    winners = {routing.route(req([a, b], seed=f"s{i}"))["slate"][0]["label"] for i in range(40)}
    assert winners == {"a/m1@medium", "b/m2@medium"}
    one = routing.route(req([a, b], seed="fixed"))
    two = routing.route(req([a, b], seed="fixed"))
    assert one["selected"] == two["selected"] and one["decision_hash"] == two["decision_hash"]
    assert one["routing"]["clincher"]["used"] and "draw" in one["routing"]["clincher"]


def test_a_clearly_weaker_route_never_wins_the_draw():
    strong, weak = cand("a", "m1", idx=55), cand("b", "m2", idx=25)
    for i in range(200):
        d = routing.route(req([strong, weak], seed=f"s{i}"))
        assert d["slate"][0]["label"] == "a/m1@medium"
        assert weak not in d["routing"]["clincher"]["band"]


def test_soft_spread_prefers_a_close_unused_route_and_records_it():
    a, b = cand("a", "m1", idx=46), cand("b", "m2", idx=45)
    load = {routing.candidate_id(a): 1}
    d = routing.route(req([a, b], cfg={"competitive_band": 0.0}, wave_load=load))
    assert d["slate"][0]["label"] == "b/m2@medium" and d["routing"]["spread"]["applied"]
    far = cand("c", "m3", idx=25)
    d = routing.route(req([a, far], cfg={"competitive_band": 0.0}, wave_load=load))
    assert d["slate"][0]["label"] == "a/m1@medium"  # spread never picks a clearly worse route


# ------------------------------------------------------------------ benchmark prior vs local evidence, exploration

def test_benchmarks_carry_cold_start_and_local_evidence_takes_over():
    smart, plain = cand("a", "smart", idx=58), cand("b", "plain", idx=36)
    cold = routing.route(req([smart, plain], cfg={"competitive_band": 0.0}))
    assert cold["slate"][0]["label"] == "a/smart@medium"
    assert all(r["benchmark"]["authority"] == 1.0 for r in cold["routing"]["candidates"])
    evidence = ev(**{key(smart).replace("/", "__").replace("@", "_AT_"): (4, 26, 2.0),
                     key(plain).replace("/", "__").replace("@", "_AT_"): (27, 3, 1.1)})
    warm = routing.route(req([smart, plain], evidence=evidence, cfg={"competitive_band": 0.0}))
    rows = {r["label"]: r for r in warm["routing"]["candidates"]}
    assert warm["slate"][0]["label"] == "b/plain@medium"
    assert rows["b/plain@medium"]["benchmark"]["authority"] < 0.25


def test_exploration_is_bounded_by_margin_cost_and_rolling_cap():
    proven, fresh = cand("a", "m1", idx=50), cand("b", "m2", idx=49)
    evidence = ev(**{key(proven).replace("/", "__").replace("@", "_AT_"): (20, 4, 1.1)})
    cfg = {"competitive_band": 0.0, "exploration": {"rate": 1.0}}
    d = routing.route(req([proven, fresh], evidence=evidence, cfg=cfg))
    assert d["routing"]["exploration"]["picked"] == routing.candidate_id(fresh)
    assert d["slate"][0]["reason"].startswith("exploration")
    capped = routing.route(req([proven, fresh], evidence=evidence, cfg=cfg, exploration_history=[True, True]))
    assert capped["routing"]["exploration"]["picked"] is None and "rolling cap" in capped["routing"]["exploration"]["blocked"]
    pricey = cand("b", "m2", idx=49, out=40, inp=10)
    costly = routing.route(req([proven, pricey], evidence=evidence, cfg=cfg))
    assert costly["routing"]["exploration"]["picked"] is None
    weak = cand("b", "m2", idx=20)
    far = routing.route(req([proven, weak], evidence=evidence, cfg=cfg))
    assert far["routing"]["exploration"]["picked"] is None


# ------------------------------------------------------------------ planner choice

def test_planner_choice_within_band_and_override_with_reason():
    cs = [cand("a", "m1", idx=50), cand("b", "m2", idx=50), cand("c", "weak", idx=30)]
    audit = routing.route(req(cs, cfg={"competitive_band": 0.05}))["routing"]
    second = audit["candidates"][1]["label"]
    plan = adaptive.apply_planner_choice(audit, {"routes": [second]})
    assert plan["chooser"] == "planner" and plan["primary"] == audit["candidates"][1]["route"]
    assert not plan["departs_from_ranking"]
    bad = adaptive.apply_planner_choice(audit, {"routes": ["c/weak@medium"]})
    assert bad["chooser"] == "router" and "route_why" in bad["planner_error"]
    ok = adaptive.apply_planner_choice(audit, {"routes": ["c/weak@medium"], "why": "only c can reach the staging VPN"})
    assert ok["departs_from_ranking"] and ok["why"].startswith("only c")
    assert len(ok["fallbacks"]) == 2
    entry = adaptive.slate_for(audit, ok)[0]
    assert entry["label"] == "c/weak@medium" and entry["reason"].startswith("planner choice:")
    unknown = adaptive.apply_planner_choice(audit, {"routes": ["z/none@low"]})
    assert "not a qualifying route" in unknown["planner_error"]


# ------------------------------------------------------------------ inline slate rendering

@pytest.mark.parametrize("n", [1, 2, 3])
def test_inline_slate_renders_one_two_three(n):
    cs = [cand("a", "m1"), cand("b", "m2"), cand("c", "m3")][:n]
    lines = adaptive.render_slate(routing.route(req(cs))["slate"])
    assert lines[0].strip() == "ROUTING" and len(lines) == 1 + 2 * n
    assert sum("PRIMARY" in ln for ln in lines) == 1 and sum(ln.strip().startswith("+ ") for ln in lines) == n


def test_inline_slate_for_no_route():
    assert "no qualifying route" in adaptive.render_slate([])[0]


def test_audit_validates_against_the_schema():
    d = routing.route(req([cand("a", "m1"), cand("b", "m2")]))
    audit = {**d["routing"], "phase": "plan", "role": "worker", "task_id": "T1",
             "planner": adaptive.apply_planner_choice(d["routing"], None)}
    schema = json.loads((ROOT / "schemas" / "routing-decision.schema.json").read_text())
    Draft202012Validator(schema).validate(json.loads(json.dumps(audit)))


def test_config_validation_bounds_preference_and_ceiling():
    assert adaptive.validate({}) == []
    bad = {"routing": {"adaptive": {"budget_ceiling_usd": 0, "weights": {"balanced": {"preference": 0.9}}}}}
    problems = adaptive.validate(bad)
    assert any("budget_ceiling_usd" in p for p in problems) and any("preference" in p for p in problems)


# ------------------------------------------------------------------ learner: attribution, decay, eligibility

def _db(tmp_path):
    from office import db
    con = db.connect(tmp_path / "runs.db")
    con.execute("INSERT INTO runs(id, playbook, phase, risk_json) VALUES('R1','Change','closed','{}')")
    return con


def _task(con, tid, status="accepted", accepted=None):
    con.execute("INSERT INTO tasks(run_id, id, title, role, scope_json, depends_json, accept_json, checks_json, status, "
                "introduced_plan_version, contract_version, acceptance_version, accepted_revision_id, created_at, updated_at) "
                "VALUES('R1',?,?,'executor','[]','[]','[]','[]',?,1,1,1,?,'t','t')", (tid, tid, status, accepted))


def _dispatch(con, did, tid, *, model="m1", started="2026-10-01T00:00:00+00:00", term="success", exit_code=0):
    con.execute("INSERT INTO dispatches(id, run_id, role, task_id, triple, harness, model, effort, started_at, ended_at, "
                "terminal_classification, exit_code) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (did, "R1", "executor", tid, f"a@2/{model}@medium", "a", model, "medium", started,
                 started.replace("00:00:00", "01:00:00"), term, exit_code))


def _revision(con, rid, did, tid):
    con.execute("INSERT INTO revisions(id, run_id, task_id, seq, dispatch_id, commit_sha, tree_sha, requirements_version, "
                "plan_version, applied_version, env_fingerprint, operation_id, status, created_at) "
                "VALUES(?,?,?,1,?,'c','t',1,1,1,'e',?,'submitted','t')", (rid, "R1", tid, did, rid))


def _finding(con, fid, rid, did, tid, category):
    con.execute("INSERT INTO findings(id, dispatch_id, run_id, task_id, revision_id, category, severity, state, summary, "
                "created_at) VALUES(?,?,?,?,?,?,'material','resolved','x','t')", (fid, did, "R1", tid, rid, category))


def test_environment_and_brief_failures_do_not_charge_the_route(tmp_path):
    con = _db(tmp_path)
    # T1: the first session died on a signal; a second landed it.
    _task(con, "T1", accepted="V2")
    _dispatch(con, "D1", "T1", term="signal", exit_code=None)
    _dispatch(con, "D2", "T1", model="m2", started="2026-10-02T00:00:00+00:00")
    _revision(con, "V2", "D2", "T1")
    # T2: rejected for a plan defect only.
    _task(con, "T2", status="cancelled")
    _dispatch(con, "D3", "T2")
    _revision(con, "V3", "D3", "T2")
    _finding(con, "F1", "V3", "D3", "T2", "requirement-contradiction")
    # T3: rejected for a code defect.
    _task(con, "T3", accepted="V5")
    _dispatch(con, "D4", "T3")
    _revision(con, "V4", "D4", "T3")
    _finding(con, "F2", "V4", "D4", "T3", "code_review")
    _dispatch(con, "D5", "T3", model="m2", started="2026-10-03T00:00:00+00:00")
    _revision(con, "V5", "D5", "T3")
    by_id = {o["dispatch_id"]: o for o in route_learning.derive_outcomes(con)}
    assert by_id["D1"]["attribution"] == "environment" and by_id["D1"]["learn_weight"] == 0
    assert by_id["D3"]["attribution"] == "plan" and by_id["D3"]["learn_weight"] == 0
    assert by_id["D4"]["attribution"] == "route" and by_id["D4"]["learn_weight"] > 0.8
    assert by_id["D2"]["success"] and by_id["D5"]["success"]
    ev_ = route_learning.evidence_for(list(by_id.values()), [cand("a", "m1"), cand("a", "m2")],
                                      {"playbook": "Change"}, as_of="2026-10-05T00:00:00+00:00")
    m1 = ev_["routes"]["a/m1@medium"]
    assert m1["failures"] > 0.8 and m1["failures"] < 1.0  # only the code defect counts, decayed a few days
    assert m1["failure_attribution"] == {"environment": 1, "plan": 1, "route": 1}


def test_mixed_and_unknown_teach_less_than_route_attribution():
    common = dict(label=None, task_status="accepted", run_phase="closed", term="success", outcome=None, attribution=None,
                  stall=None, exit_code=0, has_revision=True, rounds=0, checks_failed=0, gate_verdicts=[])
    route = route_learning.attribute_failure(**common, findings=[("code_review", "material", "open")])
    mixed = route_learning.attribute_failure(**common, findings=[("code_review", "material", "open"),
                                                                 ("plan", "material", "open")])
    unknown = route_learning.attribute_failure(**common, findings=[])
    w = {name: route_learning.ATTRIBUTION_WEIGHT[a] * (0.5 + 0.5 * c)
         for name, (a, c, _) in (("route", route), ("mixed", mixed), ("unknown", unknown))}
    assert w["route"] > w["mixed"] > w["unknown"] > 0


def test_fix_round_on_the_same_route_is_one_episode():
    rows = [{"dispatch_id": "D1", "run_id": "R", "task_id": "T1", "route": "a/m@high", "role": "executor",
             "success": False, "attribution": "route", "attribution_confidence": 0.8,
             "attribution_provenance": "x", "learn_weight": 0.9, "review_rounds": 1, "wall_seconds": 100,
             "money_actual": None, "ended_at": "1", "kind": "fresh"},
            {"dispatch_id": "D2", "run_id": "R", "task_id": "T1", "route": "a/m@high", "role": "executor",
             "success": True, "attribution": "route", "attribution_confidence": 1.0, "attribution_provenance": "x",
             "learn_weight": 1.0, "review_rounds": 0, "wall_seconds": 50, "money_actual": None, "ended_at": "2",
             "kind": "fix"}]
    (ep,) = route_learning.episodes(rows)
    assert ep["success"] and ep["attempts"] == 2 and ep["review_rounds"] == 1


def _episodes(route, outcomes, runs=4):
    return [{"run_id": f"R{i % runs}", "task_id": f"T{i}", "route": route, "role": "executor", "success": ok,
             "attribution": "route", "attribution_confidence": 0.8, "learn_weight": 1.0,
             "ended_at": f"2026-09-{10 + i:02d}"} for i, ok in enumerate(outcomes)]


def test_learned_eligibility_needs_maturity_and_replay():
    tiny = _episodes("a/m@low", [False] * 4)
    assert route_learning.eligibility_transitions(tiny, {"a/m@low": {"prior_p": 0.5}}, {}) == []
    bad = _episodes("a/m@low", [True] + [False] * 14)
    (demote,) = route_learning.eligibility_transitions(bad, {"a/m@low": {"prior_p": 0.5}}, {})
    assert demote["state"] == "learned-ineligible" and demote["replay"]["validated"]
    good = _episodes("a/m@high", [False] + [True] * 18)
    (promote,) = route_learning.eligibility_transitions(good, {"a/m@high": {"prior_p": 0.5}}, {})
    assert promote["state"] == "learned-eligible"
    one_run = _episodes("a/m@high", [True] * 18, runs=1)
    assert route_learning.eligibility_transitions(one_run, {"a/m@high": {"prior_p": 0.5}}, {}) == []
    # A recent turn for the worse fails the held-out replay: no promotion.
    turned = _episodes("a/m@high", [True] * 16 + [False] * 4)
    assert route_learning.eligibility_transitions(turned, {"a/m@high": {"prior_p": 0.5}}, {}) == []


def test_learned_eligibility_changes_routing_but_not_factual_gates():
    c = cand("a", "m1")
    k = key(c)
    promoted = routing.route(req([c], role="executor", learned_eligibility={k: {"state": "learned-eligible",
                                                                                "event_id": "LE1"}}))
    assert promoted["status"] == "selected"
    assert promoted["routing"]["candidates"][0]["eligibility"]["source"] == "learned"
    unpromoted = routing.route(req([c], role="executor"))
    assert unpromoted["status"] == "no_qualifying_candidate"
    missing = cand("a", "m1", caps=())
    blocked = routing.route(req([missing], role="executor",
                                policy={"cost_policy": "balanced", "required_capabilities": ["builder"]},
                                learned_eligibility={key(missing): {"state": "learned-eligible", "event_id": "LE1"}}))
    assert blocked["status"] == "no_qualifying_candidate"
    assert any(r["stage"] == 3 for r in blocked["rejected"])
    demoted = routing.route(req([cand("b", "m2"), cand("a", "m1")],
                                learned_eligibility={k: {"state": "learned-ineligible", "event_id": "LE2"}}))
    assert routing.candidate_id(c) not in demoted["qualifying"]
    assert any(r["stage"] == 7 and "learned ineligible" in r["reason"] for r in demoted["rejected"])


def test_harness_major_change_reduces_old_evidence():
    old = [{**o, "harness_major": "1"} for o in _episodes("a/m1@medium", [True] * 10)]
    c = cand("a", "m1")  # harness major 2
    stats = route_learning.evidence_for([], [c], {}, as_of="2026-09-30T00:00:00+00:00")  # shape only
    assert stats["routes"]["a/m1@medium"]["n_effective"] == 0
    for o in old:
        o.update(dispatch_id=o["task_id"], review_rounds=0, wall_seconds=None, money_actual=None, kind="fresh",
                 attribution_provenance="x", playbook=None, size_class=None)
    stale = route_learning.evidence_for(old, [c], {}, as_of="2026-09-30T00:00:00+00:00")["routes"]["a/m1@medium"]
    current = route_learning.evidence_for([{**o, "harness_major": "2"} for o in old], [c], {},
                                          as_of="2026-09-30T00:00:00+00:00")["routes"]["a/m1@medium"]
    assert stale["stale_outcomes"] == 10 and stale["n_effective"] < current["n_effective"] * 0.3


def test_refresh_persists_attributions_and_appends_reversible_events(tmp_path):
    con = _db(tmp_path)
    for i in range(14):
        tid = f"T{i}"
        _task(con, tid, status="accepted", accepted=f"W{i}")
        con.execute("UPDATE runs SET id=id")
        _dispatch(con, f"D{i}", tid, started=f"2026-09-{10 + i:02d}T00:00:00+00:00")
        _revision(con, f"V{i}", f"D{i}", tid)
        _finding(con, f"F{i}", f"V{i}", f"D{i}", tid, "code_review")
        _dispatch(con, f"E{i}", tid, model="m2", started=f"2026-09-{10 + i:02d}T05:00:00+00:00")
        _revision(con, f"W{i}", f"E{i}", tid)
    # Spread the failing route's tasks over runs so the maturity bar's run count is met.
    for i in range(14):
        con.execute("INSERT OR IGNORE INTO runs(id, playbook, phase, risk_json) VALUES(?, 'Change', 'closed', '{}')",
                    (f"R{i % 4 + 2}",))
        con.execute("UPDATE dispatches SET run_id=? WHERE id IN (?,?)", (f"R{i % 4 + 2}", f"D{i}", f"E{i}"))
        con.execute("UPDATE tasks SET run_id=? WHERE id=?", (f"R{i % 4 + 2}", f"T{i}"))
        con.execute("UPDATE revisions SET run_id=? WHERE id IN (?,?)", (f"R{i % 4 + 2}", f"V{i}", f"W{i}"))
    written = route_learning.refresh(con, {"executor": {"a/m1@medium": {"prior_p": 0.5}, "a/m2@medium": {"prior_p": 0.5}}})
    states = {(w["route"], w["state"]) for w in written}
    assert ("a/m1@medium", "learned-ineligible") in states and ("a/m2@medium", "learned-eligible") in states
    assert con.execute("SELECT COUNT(*) FROM route_attributions").fetchone()[0] == 28
    assert route_learning.current_eligibility(con, "executor")["a/m1@medium"]["state"] == "learned-ineligible"
    assert route_learning.refresh(con, None) == []  # idempotent: no new evidence, no new event


def test_invalid_adaptive_config_is_refused_at_resolve(tmp_path, monkeypatch):
    from office import config
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with pytest.raises(ValueError, match="budget_ceiling_usd"):
        config.resolve(None, ["routing.adaptive.budget_ceiling_usd=-1"])


def test_accepting_the_router_slate_is_not_an_override():
    cs = [cand("a", "m1", idx=50), cand("b", "m2", idx=45), cand("c", "m3", idx=40)]
    audit = routing.route(req(cs))["routing"]
    same = adaptive.apply_planner_choice(audit, {"routes": [e["label"] for e in audit["slate"]]})
    assert same["chooser"] == "planner" and same["override"] is False
