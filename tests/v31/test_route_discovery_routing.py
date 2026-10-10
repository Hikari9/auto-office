"""#494 T3: candidate qualification, trial eligibility and discovery allocation.

Pure tests call `routing.route` with synthetic requests. The wiring tests drive
`candidates.build_candidates` and `candidates.route_role`. No harness, no network.
"""
import copy
import json

import pytest

from office import adaptive, candidates, config as cfg, route_policy, route_probe, routing
from office.state import Refused

IDX = "Artificial Analysis Intelligence Index v4.3.2"
NEW = "codex@2/newmodel@high"


def cand(harness, model, effort="medium", *, idx=45, out=2.0, quota=60, caps=("builder",), **extra):
    return {
        "harness": harness, "harness_version": "2.0", "model_id": model, "invocation_model_id": model,
        "invocation_source": "documented:test", "effort": effort, "benchmark_indexes": {IDX: idx} if idx else {},
        "capabilities": list(caps), "price_fields": {"output_per_mtok": out, "input_per_mtok": 0.5},
        "speed_fields": {"output_tok_per_s": 100.0, "ttft_ms": 1000}, "cost": {"money_estimate": out},
        "quota": ({"status": "ok", "tightest_remaining_percent": quota} if quota is not None
                  else {"status": "unknown", "tightest_remaining_percent": None}),
        "route_status": "available", "status_reason": "catalog invocation available", "discovery": False,
        "probe_key": None, **extra,
    }


def untried(model="newmodel", effort="high", *, probe=None, status="discovered-unconfirmed", **kw):
    key = f"codex|2.0|{model}|{effort}|hash|worker"
    return cand("codex", model, effort, route_status=status, status_reason="documented; probe required",
                discovery=True, probe_key=key, probe=probe, **kw)


def passed(at="2026-10-10T00:00:00+00:00"):
    return {"result": "pass", "reason_class": None, "probed_at": at, "fresh": True}


def failed(reason_class="unsupported-model-effort"):
    return {"result": "fail", "reason_class": reason_class, "probed_at": "2026-10-10T00:00:00+00:00", "fresh": True}


def discovery(*, known=None, quarantined=(), size="S", blast="repo", irreversible=False, used=(0, 0, 0),
              max_=(2, 1, 3), percent=100, enabled=True, **settings):
    s = {**route_policy.DISCOVERY_DEFAULTS, "enabled": enabled, "max_trial_percent_rolling_20": percent, **settings}
    return {"settings": s,
            "allocation": {"probes": {"used": used[0], "max": max_[0]}, "trials": {"used": used[1], "max": max_[1]},
                           "rolling": {"used": used[2], "max": max_[2], "window": 20, "warmup": max_[2] == 0}},
            "known_working": known if known is not None else ["claude@2/opus@medium"],
            "quarantined": list(quarantined),
            "risk": {"size_class": size, "blast_radius": blast, "irreversible": irreversible}}


def req(cands, *, role="executor", disc=None, seed="seed-1", **extra):
    r = {"role": role, "playbook": "Change", "candidates": cands, "policy": {"cost_policy": "balanced",
         "required_capabilities": ["builder"]}, "evidence": {"routes": {}, "pooling": {"prior_strength": 8}},
         "routing_seed": seed, "adaptive_config": {"exploration": {"rate": 0.0, "margin": 1.0,
                                                                    "max_cost_vs_primary_percent": 100000}}, **extra}
    if disc is not None:
        r["discovery"] = disc
    return r


def base():
    return [cand("claude", "opus", idx=55), cand("codex", "luna", idx=40, out=0.5)]


def trusted(rq):
    """routing derives executor trust from runs.db; these pure tests use the learned path instead."""
    keys = {routing.candidate_id(c): route_keys(c) for c in rq["candidates"] if c["route_status"] == "available"}
    rq["learned_eligibility"] = {k: {"state": "learned-eligible", "event_id": "e"} for k in keys.values()}
    return rq


def route_keys(c):
    from office import route_learning
    return route_learning.candidate_key(c)


def go(cands, *, disc=None, **kw):
    discovery_input = kw.pop("discovery_input", None)
    return routing.route(trusted(req(cands, disc=disc, **kw)), discovery_input)


def cats(d):
    return {e["candidate"]: e.get("category") for e in d["rejected"] if e.get("category")}


# ------------------------------------------------------------------ discovery off

def test_discovery_off_ignores_pool_candidates_and_hashes_exactly_as_before():
    plain = go(base())
    with_pool = go(base() + [untried()])
    assert with_pool["selected"] == plain["selected"] and with_pool["decision_hash"] == plain["decision_hash"]
    assert with_pool["slate"] == plain["slate"] and "discovery" not in with_pool
    assert cats(with_pool) == {"codex@2/newmodel@high": "untried"}
    # discovery configured but disabled for this run behaves the same
    off = go(base() + [untried()], disc=discovery(enabled=False))
    assert off["selected"] == plain["selected"] and "discovery" not in off
    assert off["decision_hash"] == plain["decision_hash"]


# decision_hash and a digest of the whole result, computed by routing.route at 341b48b (before #494)
# for requests that carry no discovery input.
GOLDEN_BEFORE_494 = {
    "two": ("sha256:ff3c41ae5690c965d11f4599ccca6a22714035e6751374c4a90a4ee9398acf2a", "049814e09be95432"),
    "three": ("sha256:811e29d6fbaf68a6337c594860b7db95093e57d126214cdb8dcb03ad9bdca5a9", "275109a3750be881"),
    "worker": ("sha256:40ab10639a2b4e71fa048b2d5a8ff496a24e9d1c78fc8c8573484c51e31ad894", "47afc75e129d1149"),
    "explore": ("sha256:ea98364617d5102b827b2a025a28bd56120372c536c6202dd9f82367bdf7b700", "8ae898fdd5006f56"),
}


def golden_requests():
    yield "two", trusted(req(base()))
    yield "three", trusted(req(base() + [cand("codex", "terra", idx=48, out=1.0)], seed="seed-7"))
    yield "worker", trusted(req(base(), role="worker", seed="seed-3"))
    explore = trusted(req(base() + [cand("agy", "flash", idx=30, out=0.2)], seed="seed-9"))
    explore["adaptive_config"] = {"exploration": {"rate": 0.5, "margin": 1.0, "max_cost_vs_primary_percent": 100000}}
    yield "explore", explore


def test_routing_output_is_byte_identical_to_the_pre_change_commit_when_discovery_is_not_in_play():
    import hashlib
    for name, request in golden_requests():
        result = routing.route(json.loads(json.dumps(request)))
        digest = hashlib.sha256(json.dumps(result, sort_keys=True, default=str).encode()).hexdigest()[:16]
        assert (result["decision_hash"], digest) == GOLDEN_BEFORE_494[name], name


def test_routing_output_is_unchanged_for_a_request_with_no_discovery_inputs():
    """The decision hash only gains a discovery block when discovery is active."""
    d = go(base())
    assert "discovery" not in d and "discovery" not in d["routing"]
    assert "discovery" not in json.dumps(d["routing"]["slate"])


# ------------------------------------------------------------------ cold start and the probe intent

def test_cold_start_yields_a_probe_intent_and_keeps_the_known_working_primary():
    d = go(base() + [untried()], disc=discovery())
    disc = d["discovery"]
    assert disc["active"] and disc["intent"] == "probe" and disc["candidate"] == "codex@2/newmodel@high"
    assert disc["probe_key"].startswith("codex|2.0|newmodel|high") and disc["probe"] is None
    assert disc["fallback"] == d["selected"] == d["slate"][0]["route"]
    assert "codex@2/newmodel@high" not in d["qualifying"] and all(e["route"] != "codex@2/newmodel@high" for e in d["slate"])
    assert cats(d)["codex@2/newmodel@high"] == "probe-candidate"
    assert disc["caps"]["probes"] == {"used": 0, "max": 2}


def test_a_probe_intent_is_never_a_trial_source():
    d = go(base() + [untried()], disc=discovery())
    assert all((r["eligibility"] or {}).get("source") != "trial" for r in d["routing"]["candidates"])
    assert d["selection_disclosure"].get("trial") is None


def test_a_stale_or_other_fingerprint_pass_is_not_on_the_candidate_so_it_yields_probe_not_trial():
    # build_candidates attaches a record only when it is fresh and exact, so a stale pass arrives as None.
    d = go(base() + [untried(probe=None)], disc=discovery())
    assert d["discovery"]["intent"] == "probe"
    # a fresh pass is still only a probe intent until the preflight recompute names it
    d = go(base() + [untried(probe=passed())], disc=discovery())
    assert d["discovery"]["intent"] == "probe" and d["discovery"]["probe"]["result"] == "pass"
    assert d["selected"] == d["discovery"]["fallback"]


def test_the_draw_is_seeded_and_bounded_by_the_percentage():
    hits = [go(base() + [untried()], disc=discovery(percent=15), seed=f"s{i}")["discovery"]["intent"] == "probe"
            for i in range(200)]
    assert 15 <= sum(hits) <= 45  # about 15 percent of 200, reproducible per seed
    assert go(base() + [untried()], disc=discovery(percent=15), seed="s7")["discovery"] == \
        go(base() + [untried()], disc=discovery(percent=15), seed="s7")["discovery"]
    never = [go(base() + [untried()], disc=discovery(percent=0), seed=f"s{i}")["discovery"]["intent"] for i in range(30)]
    assert set(never) == {"none"}


# ------------------------------------------------------------------ the recompute

def handle(c=None, **extra):
    c = c or untried(probe=passed())
    return {"candidate": routing.candidate_id(c), "probe_key": c["probe_key"], "reservation_id": "A1",
            "attempt_id": "A1", **extra}


def test_recompute_with_a_fresh_pass_yields_a_trial_for_that_candidate_only():
    c = untried(probe=passed())
    d = go(base() + [c], disc=discovery(), discovery_input=handle(c))
    disc = d["discovery"]
    assert disc["intent"] == "trial" and disc["attempt_id"] == disc["reservation_id"] == "A1"
    assert d["selected"] == disc["candidate"] == "codex@2/newmodel@high"
    assert d["slate"][0]["route"] == d["selected"] and d["slate"][1]["route"] == disc["fallback"]
    assert d["routing"]["candidates"][-1]["eligibility"]["source"] == "trial"
    assert "trial" in d["selection_disclosure"] and "codex@2/newmodel@high" in d["qualifying"]
    assert d["slate"][0]["rank"] == "PRIMARY" and d["slate"][1]["rank"] == "FALLBACK 1"
    assert disc["fallback"] in ("claude@2/opus@medium", "codex@2/luna@medium")


def test_recompute_with_a_failed_probe_drops_the_candidate_and_keeps_the_known_working_route():
    c = untried(probe=failed("unsupported-model-effort"))
    d = go(base() + [c], disc=discovery(), discovery_input=handle(c))
    assert d["discovery"]["intent"] == "none" and d["discovery"]["blocked"] == "probe-failed:unsupported-model-effort"
    assert d["selected"] == d["slate"][0]["route"] != "codex@2/newmodel@high"
    assert cats(d)["codex@2/newmodel@high"] == "probe-failed"


@pytest.mark.parametrize("reason_class", ["transient", "auth-quota-blocked", "isolation-missing", "conformance-failed"])
def test_every_probe_failure_class_blocks_a_trial_with_its_own_reason(reason_class):
    c = untried(probe=failed(reason_class))
    d = go(base() + [c], disc=discovery(), discovery_input=handle(c))
    assert d["discovery"]["blocked"] == f"probe-failed:{reason_class}"


@pytest.mark.parametrize("reason_class", ["conformance-failed", "isolation-missing", "unsupported-model-effort"])
def test_a_failed_probe_keeps_its_exact_class_in_the_rejection_never_a_model_verdict(reason_class):
    c = untried(probe=failed(reason_class))
    d = go(base() + [c], disc=discovery())
    row = next(r for r in d["rejected"] if r["candidate"] == "codex@2/newmodel@high")
    assert row["category"] == "probe-failed" and reason_class in row["reason"]
    assert "unsupported by the model" not in row["reason"] and "intelligence" not in row["reason"]


def test_an_unconfirmed_row_without_discovery_metadata_is_excluded_and_never_called_unsupported():
    state = route_policy.row_status({"dispatchable": False, "model_id": "x"})
    assert state["status"] == "discovered-unconfirmed" and state["discovery_eligible"] is False
    built, skipped = candidates.build_candidates(None, "executor", probe=False, discovery=discovery(), user_policies=[])
    plain = [s for s in skipped if s["category"] == "not-eligible"]
    assert plain and all("unsupported" not in s["category"] for s in plain)
    assert not [c for c in built if c.get("discovery") and c["model_id"] in ("gpt-6-luna",)]


def test_recompute_without_a_probe_record_is_dropped():
    c = untried(probe=None)
    d = go(base() + [c], disc=discovery(), discovery_input=handle(c))
    assert d["discovery"]["intent"] == "none" and d["discovery"]["blocked"] == "probe-missing"


def test_recompute_drops_a_candidate_whose_fingerprint_changed():
    c = untried(probe=passed())
    h = handle(c)
    c["probe_key"] = "codex|9.9|newmodel|high|hash|worker"
    d = go(base() + [c], disc=discovery(), discovery_input=h)
    assert d["discovery"]["blocked"] == "fingerprint-changed" and d["discovery"]["intent"] == "none"


@pytest.mark.parametrize("change,blocked", [
    (dict(disc=dict(size="L")), "risk"),
    (dict(disc=dict(size="XL")), "risk"),
    (dict(disc=dict(blast="production")), "risk"),
    (dict(disc=dict(blast="production-data")), "risk"),
    (dict(disc=dict(irreversible=True)), "risk"),
    (dict(disc=dict(size=None)), "risk"),
    (dict(disc=dict(used=(0, 1, 0))), "trial-cap"),
    (dict(disc=dict(max_=(2, 1, 0))), "rolling-cap"),
    (dict(disc=dict(used=(0, 0, 3))), "rolling-cap"),
    (dict(disc=dict(known=[])), "no-fallback"),
    (dict(cand=dict(caps=())), "permission"),
    (dict(cand=dict(quota=3)), "quota"),
    (dict(disc=dict(quarantined=[NEW])), "quarantine"),
])
def test_recompute_re_evaluates_every_gate_and_drops_the_candidate_with_its_reason(change, blocked):
    c = untried(probe=passed(), **change.get("cand", {}))
    d = go(base() + [c], disc=discovery(**change.get("disc", {})), discovery_input=handle(c))
    assert d["discovery"]["intent"] == "none" and d["discovery"]["blocked"] == blocked, d["discovery"]
    assert d["selected"] != "codex@2/newmodel@high"
    assert d["selected"] == d["slate"][0]["route"]


def test_recompute_never_selects_a_different_untried_route():
    a, b = untried("alpha", probe=failed()), untried("beta", probe=passed())
    d = go(base() + [a, b], disc=discovery(), discovery_input=handle(a))
    assert d["discovery"]["intent"] == "none" and d["discovery"]["candidate"] == "codex@2/alpha@high"
    assert d["selected"] != "codex@2/beta@high"
    gone = go(base() + [b], disc=discovery(), discovery_input=handle(a))
    assert gone["discovery"]["blocked"] == "candidate-gone" and gone["selected"] != "codex@2/beta@high"


def test_the_first_call_picks_one_candidate_and_marks_the_rest_untried():
    d = go(base() + [untried("alpha"), untried("beta"), untried("gamma")], disc=discovery())
    assert sum(1 for v in cats(d).values() if v == "probe-candidate") == 1
    assert sum(1 for v in cats(d).values() if v == "untried") == 2


# ------------------------------------------------------------------ allocation bounds on the first call

@pytest.mark.parametrize("kw,blocked", [
    (dict(used=(2, 0, 0)), "probe-cap"),
    (dict(used=(0, 1, 0)), "trial-cap"),
    (dict(max_=(2, 1, 0)), "rolling-cap"),
    (dict(known=[]), "no-fallback"),
    (dict(size="M", blast="production"), "risk"),
])
def test_the_first_call_does_not_ask_for_a_probe_when_a_cap_or_gate_blocks(kw, blocked):
    d = go(base() + [untried()], disc=discovery(**kw))
    assert d["discovery"]["intent"] == "none" and d["discovery"]["blocked"] == blocked


def test_a_cached_pass_needs_no_probe_slot():
    d = go(base() + [untried(probe=passed())], disc=discovery(used=(2, 0, 0)))
    assert d["discovery"]["intent"] == "probe"


def test_cold_start_rolling_warmup_blocks_and_is_disclosed():
    d = go(base() + [untried()], disc=discovery(max_=(2, 1, 0)))
    assert d["discovery"]["blocked"] == "rolling-cap"
    assert d["discovery"]["caps"]["rolling"]["warmup"] is True and d["discovery"]["caps"]["rolling"]["max"] == 0


def test_size_s_and_m_local_and_repo_tasks_are_inside_the_trial_bounds():
    for size, blast in (("S", "local"), ("S", "repo"), ("M", "local"), ("M", "repo")):
        d = go(base() + [untried()], disc=discovery(size=size, blast=blast))
        assert d["discovery"]["intent"] == "probe", (size, blast)


def test_a_candidate_far_behind_the_primary_fails_the_margin_bound():
    weak = untried(idx=5)
    rq = trusted(req(base() + [weak], disc=discovery()))
    rq["adaptive_config"] = {"exploration": {"rate": 0.0, "margin": 0.001, "max_cost_vs_primary_percent": 100000}}
    d = routing.route(rq)
    assert d["discovery"]["intent"] == "none" and d["discovery"]["blocked"] == "margin"


def test_the_exploration_cost_bound_applies_to_a_trial():
    pricey = untried(out=500.0)
    rq = trusted(req(base() + [pricey], disc=discovery()))
    rq["adaptive_config"] = {"exploration": {"rate": 0.0, "margin": 1.0, "max_cost_vs_primary_percent": 100}}
    d = routing.route(rq)
    assert d["discovery"]["blocked"] == "cost"


def test_a_user_budget_ceiling_still_removes_a_trial_candidate():
    pricey = untried(out=500.0)
    rq = trusted(req(base() + [pricey], disc=discovery()))
    rq["adaptive_config"] = {"exploration": {"rate": 0.0, "margin": 1.0, "max_cost_vs_primary_percent": 100000},
                             "budget_ceiling_usd": 0.5}
    d = routing.route(rq)
    assert d["discovery"]["intent"] == "none"


def test_exploration_taking_the_slot_blocks_discovery():
    known = ["claude@2/opus@medium", "codex@2/luna@medium"]
    rq = trusted(req(base() + [untried()], disc=discovery(known=known)))
    rq["adaptive_config"] = {"exploration": {"rate": 1.0, "margin": 1.0, "min_samples_mature": 100,
                                              "max_cost_vs_primary_percent": 100000, "max_per_run": 1}}
    d = routing.route(rq)
    assert d["routing"]["exploration"]["active"]
    assert d["discovery"]["blocked"] == "exploration-active" and d["discovery"]["intent"] == "none"


# ------------------------------------------------------------------ floors and quarantine

def test_an_unbenchmarked_trial_candidate_neither_fails_closed_nor_scores_high():
    floor = {"min_benchmark_index": {"index_name": IDX, "min_score": 30}}
    unbench = untried(idx=None)
    rq = trusted(req(base() + [unbench], disc=discovery()))
    rq["policy"]["floor"] = floor
    d = routing.route(rq)
    assert d["discovery"]["intent"] == "probe", d["rejected"]
    row = adaptive.trial_rows([unbench], {"rows": d["routing"]["candidates"], "slate": d["slate"]}, rq)[NEW]["row"]
    assert row["benchmark"]["source"] == "no pinned benchmark score; neutral prior"
    assert row["p_success"] <= 0.5  # the neutral, uncertain prior: not scored as high quality


def test_a_known_low_score_or_low_effort_still_rejects_a_trial_candidate():
    floor = {"min_benchmark_index": {"index_name": IDX, "min_score": 30}}
    rq = trusted(req(base() + [untried(idx=10)], disc=discovery()))
    rq["policy"]["floor"] = floor
    d = routing.route(rq)
    assert d["discovery"]["intent"] == "none" and d["discovery"]["blocked"] == "floor"
    assert cats(d)[NEW] == "floor"
    strong = [cand("claude", "opus", "high"), cand("codex", "luna", "high")]
    rq = trusted(req(strong + [untried(effort="low")],
                     disc=discovery(known=["claude@2/opus@high", "codex@2/luna@high"])))
    rq["policy"]["floor"] = {"min_effort": "high"}
    d = routing.route(rq)
    assert d["discovery"]["blocked"] == "floor" and "effort below role floor" in \
        next(e["reason"] for e in d["rejected"] if e["candidate"] == "codex@2/newmodel@low")


def test_floors_still_fail_closed_for_ordinary_candidates_on_a_missing_score():
    floor = {"min_benchmark_index": {"index_name": IDX, "min_score": 30}}
    rq = trusted(req([cand("claude", "opus", idx=None), cand("codex", "luna", idx=40)]))
    rq["policy"]["floor"] = floor
    d = routing.route(rq)
    assert any(x["stage"] == 4 and "benchmark_indexes" in x["reason"] for x in d["rejected"])


def test_a_quarantined_candidate_never_takes_a_trial():
    c = untried(probe=passed())
    d = go(base() + [c], disc=discovery(quarantined=[NEW]), discovery_input=handle(c))
    assert d["discovery"]["blocked"] == "quarantine" and d["selected"] != NEW


# ------------------------------------------------------------------ authority roles

@pytest.mark.parametrize("role", ["planner", "plan_reviewer", "code_reviewer", "integration_reviewer",
                                  "visual_reviewer", "browser_verifier", "closeout_verifier"])
def test_authority_roles_never_see_a_trial_or_probe_intent(role):
    c = untried(probe=passed())
    cs = base() + [c]
    rq = trusted(req(cs, role=role, disc=discovery()))
    d = routing.route(rq, handle(c))
    assert "discovery" not in d or d["discovery"]["intent"] == "none"
    assert NEW not in (d.get("qualifying") or []) and d.get("selected") != NEW
    assert any(e["candidate"] == NEW and e.get("category") for e in d["rejected"])


def test_an_unavailable_status_is_never_eligible_without_discovery_for_any_role():
    for role in ("executor", "worker", "planner", "code_reviewer"):
        rq = trusted(req(base() + [untried(probe=passed())], role=role))
        d = routing.route(rq)
        assert d.get("selected") != NEW
        assert any(e["candidate"] == NEW and e.get("category") == "untried" for e in d["rejected"])


def test_a_worker_role_pool_candidate_is_not_admitted_by_the_absence_of_a_trust_gate():
    d = go(base() + [untried(probe=passed())], disc=discovery(), role="worker")
    assert d["selected"] != NEW and d["discovery"]["intent"] == "probe"


# ------------------------------------------------------------------ user policy

def policy(denied=(), overkill=(), source="user"):
    return {"denied": list(denied), "overkill": list(overkill),
            "sources": {"denied_models": source, "overkill_rules": source}}


@pytest.mark.parametrize("role", ["executor", "worker", "planner", "code_reviewer", "visual_reviewer"])
def test_a_denied_route_is_rejected_at_stage_one_for_every_role_with_its_source(role):
    d = routing.route(trusted(req(base(), role=role, user_policies=[policy(denied=["claude/opus@medium"])])))
    rejected = [x for x in d["rejected"] if "opus" in x["candidate"]]
    assert rejected and rejected[0]["stage"] == 1 and rejected[0]["category"] == "denied"
    assert "denied by user routing.user_policy.denied_models: claude/opus@medium" in rejected[0]["reason"]
    assert d.get("selected") != "claude@2/opus@medium"


def test_denial_applies_even_to_a_manual_route_request():
    d = routing.route(trusted(req(base(), user_policies=[policy(denied=["claude/opus@medium"])], manual_route=True)))
    assert any(x["stage"] == 1 and x["category"] == "denied" for x in d["rejected"])


def test_denying_a_harness_or_a_bare_model_blocks_all_its_efforts():
    for spec in ("harness:claude", "opus"):
        d = routing.route(trusted(req([cand("claude", "opus", e) for e in ("low", "high")] + [cand("codex", "luna")],
                                       user_policies=[policy(denied=[spec])])))
        assert {x["candidate"] for x in d["rejected"] if x.get("category") == "denied"} == \
            {"claude@2/opus@low", "claude@2/opus@high"}


def test_overkill_skips_a_route_only_in_automatic_selection_inside_its_scope():
    rule = {"route": "claude/opus@medium", "roles": ["executor"], "size_classes": ["S"]}
    auto = routing.route(trusted(req(base(), size_class="S", user_policies=[policy(overkill=[rule])])))
    assert any(x["candidate"] == "claude@2/opus@medium" and x["category"] == "overkill" for x in auto["rejected"])
    assert auto["selected"] != "claude@2/opus@medium"
    # the same route stays selectable by an explicit manual route
    manual = routing.route(trusted(req(base(), size_class="S", manual_route=True, user_policies=[policy(overkill=[rule])])))
    assert not any(x.get("category") == "overkill" for x in manual["rejected"])
    # and stays automatic outside the rule's scope
    for kw in (dict(size_class="M"), dict(role="worker", size_class="S")):
        out = routing.route(trusted(req(base(), user_policies=[policy(overkill=[rule])], **kw)))
        assert not any(x.get("category") == "overkill" for x in out["rejected"]), kw


def test_overkill_rule_with_no_scope_covers_every_role_and_size_but_not_manual_routes():
    rule = {"route": "claude/opus@medium"}
    for role in ("executor", "planner", "code_reviewer"):
        d = routing.route(trusted(req(base(), role=role, size_class="XL", user_policies=[policy(overkill=[rule])])))
        assert any(x.get("category") == "overkill" for x in d["rejected"])


# ------------------------------------------------------------------ the alias contract

def test_an_alias_over_a_disabled_target_is_never_available_and_names_the_target():
    rows = [
        {"model_id": "gpt-9-x", "invocation_model_id": "gpt-9-x", "invocation_harness": "codex", "effort": "high",
         "dispatchable": False, "invocation_source": "unverified: not callable"},
        {"model_id": "niner", "invocation_harness": "codex", "effort": "high", "alias_family": r"^gpt-(?P<version>\d+)-x$"},
    ]
    out = candidates.resolve_aliases(rows)
    alias = next(r for r in out if r["model_id"] == "niner")
    assert alias["dispatchable"] is False and "target gpt-9-x" in alias["status_reason"]
    state = route_policy.row_status(alias)
    assert state["status"] == "discovered-unconfirmed" and state["discovery_eligible"] is False


def test_an_alias_over_a_discovery_eligible_target_inherits_eligibility_not_availability():
    rows = [
        {"model_id": "gpt-9-x", "invocation_model_id": "gpt-9-x", "invocation_harness": "codex", "effort": "high",
         "dispatchable": False, "discovery": "eligible", "discovery_reason": "documented",
         "invocation_source": "documented: x"},
        {"model_id": "niner", "invocation_harness": "codex", "effort": "high", "alias_family": r"^gpt-(?P<version>\d+)-x$"},
    ]
    alias = next(r for r in candidates.resolve_aliases(rows) if r["model_id"] == "niner")
    assert alias["dispatchable"] is False and alias["discovery"] == "eligible"
    assert route_policy.row_status(alias)["status"] == "discovered-unconfirmed"
    assert route_policy.row_status(alias)["discovery_eligible"] is True


# ------------------------------------------------------------------ build_candidates

@pytest.fixture
def installed(monkeypatch):
    from office import adapters
    monkeypatch.setattr(adapters, "installed", lambda a: True)
    monkeypatch.setattr(adapters, "harness_version", lambda a: "2.0.0")


def label(c):
    return f"{c.get('harness') or c.get('invocation_harness')}/{c.get('model_id')}@{c.get('effort')}"


def test_no_active_catalog_row_is_dropped_silently(installed):
    for settings in (None, {**route_policy.DISCOVERY_DEFAULTS, "enabled": True}):
        built, skipped = candidates.build_candidates(None, "executor", probe=False, discovery=settings, user_policies=[])
        seen = [label(c) for c in built] + [s["candidate"] for s in skipped]
        assert sorted(seen) == sorted(label(r) for r in candidates.catalog_rows()), settings
        assert all(s.get("category") and s.get("reason") for s in skipped)


def test_discovery_off_keeps_every_discovery_row_out_of_the_candidates_and_names_it_untried(installed):
    built, skipped = candidates.build_candidates(None, "executor", probe=False, user_policies=[])
    assert all(c["route_status"] == "available" and c["discovery"] is False for c in built)
    sol = [s for s in skipped if s["candidate"].startswith("codex/gpt-6.1-sol@")]
    assert len(sol) == 5 and {s["category"] for s in sol} == {"untried"}
    assert all("discovery is off" in s["reason"] for s in sol)
    flash = [s for s in skipped if s["candidate"] == "agy/gemini-3.8-flash@high"]
    assert flash and flash[0]["category"] == "untried"


def test_discovery_on_returns_eligible_rows_as_candidates_with_status_and_a_probe_key(installed):
    settings = {**route_policy.DISCOVERY_DEFAULTS, "enabled": True}
    built, skipped = candidates.build_candidates(None, "executor", probe=False, discovery=settings, user_policies=[])
    pool = [c for c in built if c["route_status"] != "available"]
    assert {label(c) for c in pool} >= {f"codex/gpt-6.1-sol@{e}" for e in ("low", "medium", "high", "xhigh", "max")}
    assert {label(c) for c in pool} >= {f"claude/claude-haiku-5-5@{e}" for e in ("low", "medium", "high", "xhigh", "max")}
    for c in pool:
        assert c["discovery"] is True and c["route_status"] == "discovered-unconfirmed" and c["probe_key"]
        assert c["probe_key"].split("|")[:4] == [c["harness"], "2.0.0", c["invocation_model_id"], c["effort"]]
    # benchmark-only and ineligible rows are never offered for discovery
    cat = {s["candidate"]: s["category"] for s in skipped}
    assert cat["codex/gpt-6-luna@none"] == "not-eligible"
    assert not [c for c in pool if c["model_id"] in ("claude-sonnet-5-5", "gpt-6-luna")]


def test_a_fresh_exact_pass_attaches_to_exactly_that_candidate(installed, tmp_path):
    from office import adapters, db
    con = db.connect(tmp_path / "runs.db")
    settings = {**route_policy.DISCOVERY_DEFAULTS, "enabled": True}
    built, _ = candidates.build_candidates(con, "executor", probe=False, discovery=settings, user_policies=[])
    target = next(c for c in built if label(c) == "codex/gpt-6.1-sol@high")
    fp = route_probe.fingerprint(target, adapters.load_all()["codex"])
    from office.util import now_iso
    con.execute("INSERT INTO route_probes(key, harness, harness_version, adapter_hash, profile, invocation_model_id, "
                "effort, result, probed_at, attempt_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (target["probe_key"], *(fp[k] for k in ("harness", "harness_version", "adapter_hash", "profile",
                                                         "invocation_model_id", "effort")), "pass", now_iso(), "A0"))
    built, _ = candidates.build_candidates(con, "executor", probe=False, discovery=settings, user_policies=[])
    by = {label(c): c for c in built}
    assert by["codex/gpt-6.1-sol@high"]["probe"]["result"] == "pass"
    assert by["codex/gpt-6.1-sol@high"]["route_status"] == "probe-passed"
    for sibling in ("codex/gpt-6.1-sol@low", "codex/gpt-6.1-sol@xhigh", "claude/claude-haiku-5-5@high"):
        assert by[sibling]["probe"] is None and by[sibling]["route_status"] == "discovered-unconfirmed"


def test_a_stale_or_other_fingerprint_pass_never_attaches_to_a_candidate(installed, tmp_path):
    from datetime import datetime, timedelta, timezone
    from office import adapters, db
    con = db.connect(tmp_path / "runs.db")
    settings = {**route_policy.DISCOVERY_DEFAULTS, "enabled": True}
    built, _ = candidates.build_candidates(con, "executor", probe=False, discovery=settings, user_policies=[])
    target = next(c for c in built if label(c) == "codex/gpt-6.1-sol@high")
    fp = route_probe.fingerprint(target, adapters.load_all()["codex"])
    insert = ("INSERT INTO route_probes(key, harness, harness_version, adapter_hash, profile, invocation_model_id, "
              "effort, result, probed_at, attempt_id) VALUES(?,?,?,?,?,?,?,?,?,?)")
    columns = ("harness", "harness_version", "adapter_hash", "profile", "invocation_model_id", "effort")
    long_ago = (datetime.now(timezone.utc) - timedelta(days=settings["probe_ttl_days"] + 5)).isoformat()
    con.execute(insert, (target["probe_key"], *(fp[k] for k in columns), "pass", long_ago, "A-old"))
    other = target["probe_key"].replace("|2.0.0|", "|9.9.9|")  # a pass for another harness version
    assert other != target["probe_key"]
    con.execute(insert, (other, fp["harness"], "9.9.9", *(fp[k] for k in columns[2:]), "pass",
                         datetime.now(timezone.utc).isoformat(), "A-other"))
    built, _ = candidates.build_candidates(con, "executor", probe=False, discovery=settings, user_policies=[])
    chosen = next(c for c in built if label(c) == "codex/gpt-6.1-sol@high")
    assert chosen["probe"] is None and chosen["route_status"] == "discovered-unconfirmed"


def test_denied_routes_leave_every_other_caller_with_a_category(installed):
    built, skipped = candidates.build_candidates(None, "executor", probe=False,
                                                 user_policies=[policy(denied=["codex/gpt-5.6-sol@high", "harness:agy"])])
    assert not [c for c in built if c["harness"] == "agy" or label(c) == "codex/gpt-5.6-sol@high"]
    denied = [s for s in skipped if s["category"] == "denied"]
    assert any(s["candidate"] == "codex/gpt-5.6-sol@high" for s in denied)
    assert all("denied by user routing.user_policy.denied_models" in s["reason"] for s in denied)


def test_the_users_current_config_denies_routes_for_callers_that_pass_no_policy(installed, monkeypatch, tmp_path):
    user = tmp_path / "user.yaml"
    user.write_text("routing:\n  user_policy:\n    denied_models: [codex/gpt-5.6-sol@high]\n")
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(user))
    built, skipped = candidates.build_candidates(None, "executor", probe=False)
    assert not [c for c in built if label(c) == "codex/gpt-5.6-sol@high"]
    assert any(s["category"] == "denied" and "by user routing.user_policy" in s["reason"] for s in skipped)
    user.write_text("routing: {}\n")  # an explicit policy change re-enables it
    built, _ = candidates.build_candidates(None, "executor", probe=False)
    assert [c for c in built if label(c) == "codex/gpt-5.6-sol@high"]


# ------------------------------------------------------------------ declared routes

def deny(monkeypatch, tmp_path, *specs, overkill=()):
    user = tmp_path / "user.yaml"
    lines = ["routing:", "  user_policy:", f"    denied_models: [{', '.join(specs)}]"]
    if overkill:
        lines.append("    overkill_rules:")
        lines += [f"      - {{route: {r}}}" for r in overkill]
    user.write_text("\n".join(lines) + "\n")
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(user))


def test_a_declared_route_refuses_a_denied_route_naming_the_source_and_the_way_back(monkeypatch, tmp_path):
    deny(monkeypatch, tmp_path, "codex/gpt-5.6-sol@high")
    with pytest.raises(Refused) as err:
        candidates.declared_candidate("codex", "gpt-5.6-sol", "high")
    assert err.value.category == "route-denied"
    assert "denied by user routing.user_policy.denied_models: codex/gpt-5.6-sol@high" in err.value.message
    assert "office config routing.user_policy.denied_models" in err.value.next_step
    with pytest.raises(Refused):
        candidates.declared_decision("codex/gpt-5.6-sol@high", flag="--review-as")
    # only that exact route: its sibling effort is still declarable
    assert candidates.declared_candidate("codex", "gpt-5.6-sol", "low")["override"] is True


@pytest.mark.parametrize("spec", ["harness:codex", "gpt-5.6-sol", "codex/gpt-5.6-sol"])
def test_denial_forms_all_block_a_declared_route(monkeypatch, tmp_path, spec):
    deny(monkeypatch, tmp_path, spec)
    with pytest.raises(Refused):
        candidates.declared_candidate("codex", "gpt-5.6-sol", "high")


def test_a_declared_alias_route_is_denied_through_its_target(monkeypatch, tmp_path):
    deny(monkeypatch, tmp_path, "claude-opus-5-5")
    with pytest.raises(Refused):
        candidates.declared_candidate("claude", "opus", "high")


def test_overkill_never_blocks_a_declared_route(monkeypatch, tmp_path):
    deny(monkeypatch, tmp_path, overkill=["codex/gpt-5.6-sol@high"])
    assert candidates.declared_candidate("codex", "gpt-5.6-sol", "high")["override"] is True


def test_with_no_user_policy_declared_routes_are_unchanged(monkeypatch, tmp_path):
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp_path / "none.yaml"))
    assert candidates.declared_candidate("codex", "gpt-5.6-sol", "high")["invocation_source"] == "user-override"


# ------------------------------------------------------------------ cost is a ranking factor, a user ceiling is hard

def inputs_for(config, tmp_path):
    from office import db
    con = db.connect(tmp_path / "ceiling.db")
    run = {"id": "r1", "playbook": "Change", "risk": {"size_class": "S"}}
    return candidates.adaptive_inputs(con, config, run, "executor", [], task_id=None, dispatch_kind="fresh",
                                      plan_version=1)


def test_the_shipped_economic_scale_is_not_a_ceiling_for_a_new_run(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp_path / "none.yaml"))
    config = cfg.resolve(None)[0]
    got = inputs_for(config, tmp_path)
    assert got["budget_ceiling"] == {"usd": None, "source": "shipped"}
    assert got["adaptive_config"]["budget_ceiling_usd"] is None
    assert adaptive.settings({"routing": {"adaptive": got["adaptive_config"]}})["budget_ceiling_usd"] is None
    assert config["routing"]["adaptive"]["cost_scale_usd"] == 25  # still the ranking scale


@pytest.mark.parametrize("tier", ["user", "repo"])
def test_a_user_set_ceiling_is_hard_and_its_source_is_disclosed(tmp_path, monkeypatch, tier):
    user, repo = tmp_path / "user.yaml", tmp_path / "repo"
    (repo / ".auto-office").mkdir(parents=True)
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(user))
    (user if tier == "user" else repo / ".auto-office" / "config.yaml").write_text(
        "routing:\n  adaptive:\n    budget_ceiling_usd: 3\n")
    config = cfg.resolve(repo)[0]
    got = inputs_for(config, tmp_path)
    assert got["budget_ceiling"] == {"usd": 3.0, "source": tier} and got["adaptive_config"]["budget_ceiling_usd"] == 3.0
    rq = req([cand("claude", "opus", out=900.0, idx=60), cand("codex", "luna", out=0.1, idx=50)], role="worker")
    rq.update(got)
    rq["adaptive_config"] = {**got["adaptive_config"], "exploration": {"rate": 0.0}}
    d = routing.route(rq)
    assert d["routing"]["budget_ceiling_usd"] == 3.0 and d["routing"]["budget_ceiling_source"] == tier
    assert any(x["stage"] == 8 and "budget ceiling" in x["reason"] and "claude" in x["candidate"] for x in d["rejected"])


def test_no_cost_removes_a_route_without_a_user_ceiling(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp_path / "none.yaml"))
    got = inputs_for(cfg.resolve(None)[0], tmp_path)
    rq = req([cand("claude", "opus", out=9000.0, idx=60), cand("codex", "luna", out=0.1, idx=50)], role="worker")
    rq.update(got)
    rq["adaptive_config"] = {**got["adaptive_config"], "exploration": {"rate": 0.0}}
    d = routing.route(rq)
    assert not any(x["stage"] == 8 for x in d["rejected"]) and len(d["slate"]) == 2
    assert d["routing"]["budget_ceiling_source"] == "shipped"


def test_a_run_pinned_before_provenance_keeps_its_old_ceiling_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp_path / "none.yaml"))
    old = copy.deepcopy(cfg.resolve(None)[0])
    for key in (route_policy.PROVENANCE_KEY, route_policy.DIGEST_KEY):
        old.pop(key)
    old["routing"].pop("discovery"), old["routing"].pop("user_policy")
    old["routing"]["adaptive"]["budget_ceiling_usd"] = 25
    got = inputs_for(old, tmp_path)
    assert "budget_ceiling" not in got and got["adaptive_config"]["budget_ceiling_usd"] == 25
    assert adaptive.settings({"routing": {"adaptive": got["adaptive_config"]}})["budget_ceiling_usd"] == 25
    gone = copy.deepcopy(old)
    gone["routing"]["adaptive"].pop("budget_ceiling_usd")
    assert adaptive.settings({"routing": {"adaptive": inputs_for(gone, tmp_path)["adaptive_config"]}})[
        "budget_ceiling_usd"] == 25.0  # the old built-in default is still hard for such a run


# ------------------------------------------------------------------ route_role end to end

def discovering_config(run, **discovery):
    config = copy.deepcopy(run["policy"])
    config["routing"]["discovery"].update({"enabled": True, "max_trial_percent_rolling_20": 100, **discovery})
    config["routing"]["adaptive"]["exploration"] = {"rate": 0.0, "margin": 1.0, "max_cost_vs_primary_percent": 100000}
    return config


def test_route_role_drives_cold_start_probe_then_trial_without_minting_trust(env):
    from conftest import start_inline
    from office import state
    env.trust()
    start_inline(env)
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()[0])
    run["risk"] = {**(run.get("risk") or {}), "size_class": "S", "blast_radius": "repo", "irreversible": False}
    config = discovering_config(run)
    acts = lambda: [con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("adapter_trust_acts", "recorded_overrides")]
    before = acts()

    first = candidates.route_role(con, config, run, "executor", task_id="T1", probe=False)
    disc = first["discovery"]
    assert first["status"] == "selected" and disc["intent"] == "probe" and disc["probe"] is None
    pooled = disc["candidate"]
    assert pooled not in first["qualifying"] and first["selected"] == disc["fallback"]
    assert first["request"]["discovery"]["settings"]["enabled"] is True
    assert all(c.get("discovery") is False for c in first["request"]["candidates"] if c["route_status"] == "available")
    assert before == acts()

    # the preflight's probe passed: record it the way route_probe would, then recompute for that candidate only
    pool = next(c for c in first["request"]["candidates"] if routing.candidate_id(c) == pooled)
    fp = {k: pool[k] for k in ("harness", "harness_version", "invocation_model_id", "effort")}
    from office.util import now_iso
    con.execute("INSERT INTO route_probes(key, harness, harness_version, adapter_hash, profile, invocation_model_id, "
                "effort, result, probed_at, attempt_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (pool["probe_key"], fp["harness"], fp["harness_version"], pool["adapter_hash"], "worker",
                 fp["invocation_model_id"], fp["effort"], "pass", now_iso(), "A1"))
    handle = {"candidate": pooled, "probe_key": pool["probe_key"], "reservation_id": "A1", "attempt_id": "A1"}
    second = candidates.route_role(con, config, run, "executor", task_id="T1", probe=False, discovery_input=handle)
    d2 = second["discovery"]
    assert d2["intent"] == "trial" and second["selected"] == pooled and d2["fallback"] == first["selected"]
    assert second["slate"][0]["rank"] == "PRIMARY" and second["request"]["discovery_input"] == handle
    # a recorded request replays to the same decision
    replay = routing.route(json.loads(json.dumps(second["request"])))
    assert replay["decision_hash"] == second["decision_hash"] and replay["selected"] == pooled
    # no trust, override or quarantine change is made anywhere on this path
    assert acts() == before
    from office import scoring
    assert scoring.evaluate_trust_state(con, pooled)[1] == "valid-unverified"


def test_a_mistyped_value_outside_the_denial_subtree_does_not_hide_or_refuse_a_policy(monkeypatch, tmp_path):
    user = tmp_path / "user.yaml"
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(user))
    user.write_text("routing:\n  adaptive:\n    competitive_band: high\n")
    assert candidates.live_user_policies() == []
    user.write_text("routing:\n  adaptive:\n    competitive_band: high\n  user_policy:\n"
                    "    denied_models: [harness:codex]\n")
    assert candidates.live_user_policies()[0]["denied"] == ["harness:codex"]
    user.write_text("routing:\n  user_policy:\n    overkill_rules: bad\n")
    with pytest.raises(ValueError, match="routing.user_policy.overkill_rules"):
        candidates.live_user_policies()


def test_route_role_honors_a_live_denial_and_fails_closed_on_an_unreadable_policy(env, monkeypatch, tmp_path):
    from conftest import start_inline
    from office import state
    env.trust()
    start_inline(env)
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()[0])
    config = discovering_config(run)
    user = tmp_path / "live-user.yaml"
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(user))
    first = candidates.route_role(con, config, run, "executor", task_id="T1", probe=False)
    harness = first["selected"].split("@")[0]
    # a denial written after the run was pinned takes effect on the next decision
    user.write_text(f"routing:\n  user_policy:\n    denied_models: [harness:{harness}]\n")
    after = candidates.route_role(con, config, run, "executor", task_id="T1", probe=False)
    assert not str(after.get("selected") or "").startswith(harness + "@")
    assert any(e.get("category") == "denied" for e in after["rejected"])
    # an unreadable or malformed live policy offers nothing: no route, probe or trial, rather than no denials
    for text in ("routing: [unclosed\n  - : :\n", "routing:\n  user_policy:\n    denied_models: \"codex/x@high\"\n",
                 "routing: 5\n"):
        user.write_text(text)
        with pytest.raises(Refused) as err:
            candidates.route_role(con, config, run, "executor", task_id="T1", probe=False)
        assert err.value.category == "policy-unreadable"
        with pytest.raises(Refused) as declared:
            candidates.declared_candidate("codex", "gpt-5.6-sol", "high")
        assert declared.value.category == "policy-unreadable"


def test_route_role_with_discovery_off_a_pinned_run_a_manual_route_or_a_reviewer_never_discovers(env):
    from conftest import start_inline
    from office import state
    env.trust()
    start_inline(env)
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()[0])
    run["risk"] = {**(run.get("risk") or {}), "size_class": "S", "blast_radius": "repo", "irreversible": False}
    on = discovering_config(run)

    def no_pool(result):
        assert "discovery" not in result
        assert all(c["route_status"] == "available" for c in result["request"]["candidates"])

    no_pool(candidates.route_role(con, run["policy"], run, "executor", task_id="T1", probe=False))      # shipped: off
    pinned = copy.deepcopy(on)
    for key in (route_policy.PROVENANCE_KEY, route_policy.DIGEST_KEY):
        pinned.pop(key)
    no_pool(candidates.route_role(con, pinned, run, "executor", task_id="T1", probe=False))             # pre-change pin
    declared = candidates.route_role(con, on, run, "executor", task_id="T1", probe=False,
                                     override="codex/gpt-6.1-sol@high")
    no_pool(declared)
    for role in ("planner", "code_reviewer", "plan_reviewer"):
        no_pool(candidates.route_role(con, on, run, role, task_id="T1", probe=False))


def test_route_role_rejects_a_denied_route_with_its_source_and_a_plan_route_choice_is_refused(env, monkeypatch):
    from conftest import start_inline
    from office import state
    env.trust()
    start_inline(env)
    con = env.con()
    run = state.get_run(con, con.execute("SELECT id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()[0])
    config = copy.deepcopy(run["policy"])
    config["routing"]["user_policy"]["denied_models"] = ["claude/haiku@high", "claude/claude-opus-5-5@medium"]
    config[route_policy.PROVENANCE_KEY]["routing.user_policy.denied_models"] = "user"
    result = candidates.route_role(con, config, run, "executor", task_id="T1", probe=False)
    denied = [x for x in result["rejected"] if x.get("category") == "denied"]
    assert denied and all(x["stage"] == 1 and "denied by user routing.user_policy.denied_models" in x["reason"]
                          for x in denied)
    audit = result["routing"]
    picked = adaptive.apply_planner_choice(audit, {"routes": [denied[0]["candidate"]], "why": "the user asked for it"})
    assert "rejected at stage 1" in picked["planner_error"] and "denied" in picked["planner_error"]
    assert picked["primary"] == audit["slate"][0]["route"]
