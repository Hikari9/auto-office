"""Properties of route(): which candidates may be chosen, what a rejected candidate cannot change, and how
preference and cost order the ones that qualify.

Candidates are generated, so these hold for any slate of harnesses, models, efforts, capabilities and prices
instead of the handful an example names. `route` is pure here: no runs.db, no harness, no network.
"""
from __future__ import annotations

import random

from hypothesis import assume, given, strategies as st

from office import routing

IDX = "Artificial Analysis Intelligence Index v4.3.2"
FLOOR = {"min_effort": "medium"}


@st.composite
def _candidate(draw):
    return {
        "harness": draw(st.sampled_from(["claude", "codex", "agy"])), "harness_version": "2.0",
        "model_id": draw(st.sampled_from(["m1", "m2", "m3", "m4"])), "invocation_source": "documented:test",
        "effort": draw(st.sampled_from(["low", "medium", "high"])),
        "capabilities": draw(st.sets(st.sampled_from(["builder", "review", "vision"]), min_size=0, max_size=3).map(sorted)),
        "benchmark_indexes": {IDX: draw(st.integers(min_value=20, max_value=60))},
        "price_fields": {"output_per_mtok": draw(st.integers(min_value=1, max_value=20)), "input_per_mtok": 0.5},
        "speed_fields": {"output_tok_per_s": draw(st.integers(min_value=20, max_value=300)), "ttft_ms": 1000},
        "cost": {"money_estimate": draw(st.integers(min_value=1, max_value=20)),
                 "quota_burn": draw(st.integers(min_value=0, max_value=9))},
        "quota": draw(st.sampled_from([{"status": "unknown", "tightest_remaining_percent": None},
                                       {"status": "ok", "tightest_remaining_percent": 60}])),
        "hard_excluded": draw(st.booleans()),
        "supported_playbooks": draw(st.sampled_from([None, ["Change"], ["Other"]])),
    }


_slates = st.lists(_candidate(), min_size=1, max_size=6, unique_by=routing.candidate_id)


def _qualifies(c, capability):
    """The documented gates: not excluded, has the capability, meets the effort floor, supports the task shape."""
    return (not c["hard_excluded"] and capability in c["capabilities"] and c["effort"] in ("medium", "high")
            and (not c["supported_playbooks"] or "Change" in c["supported_playbooks"]))


def _worker(cands, **extra):
    return {"role": "worker", "playbook": "Change", "candidates": cands,
            "policy": {"cost_policy": "balanced", "required_capabilities": ["builder"], "floor": FLOOR},
            "evidence": {"routes": {}, "pooling": {"prior_strength": 8}}, "routing_seed": "seed",
            "adaptive_config": {"exploration": {"rate": 0.0}}, **extra}


def _reviewer(cands, policy=None, **extra):
    return {"role": "plan_reviewer", "playbook": "Change", "candidates": cands,
            "policy": {"required_capabilities": ["review"], "floor": FLOOR, **(policy or {})}, **extra}


# ------------------------------------------------------------------ qualification (adaptive worker route)

@given(cands=_slates)
def test_a_worker_slate_holds_only_candidates_that_pass_every_gate(cands):
    decision = routing.route(_worker(cands))
    qualifying = {routing.candidate_id(c) for c in cands if _qualifies(c, "builder")}
    if not qualifying:
        assert decision["status"] == "no_qualifying_candidate" and decision["selected"] is None
    else:
        assert decision["status"] == "selected"
        assert {e["route"] for e in decision["slate"]} <= qualifying
        assert decision["selected"] in qualifying
    rejected = {r["candidate"] for r in decision["rejected"]}
    assert {routing.candidate_id(c) for c in cands} - qualifying <= rejected, "a candidate that fails a gate is named, never dropped"


@given(cands=_slates, extra=_candidate())
def test_a_candidate_that_fails_a_gate_cannot_change_who_is_chosen(cands, extra):
    assume(routing.candidate_id(extra) not in {routing.candidate_id(c) for c in cands})
    assume(not _qualifies(extra, "builder"))
    before = routing.route(_worker(cands))
    after = routing.route(_worker(cands + [extra]))
    assert (after["status"], after.get("selected")) == (before["status"], before.get("selected"))
    assert [e["route"] for e in after.get("slate", [])] == [e["route"] for e in before.get("slate", [])]


@given(cands=_slates, seed=st.integers())
def test_routing_depends_on_the_candidates_not_the_order_they_are_listed_in(cands, seed):
    shuffled = list(cands)
    random.Random(seed).shuffle(shuffled)
    first, second = routing.route(_worker(cands)), routing.route(_worker(shuffled))
    assert first.get("selected") == second.get("selected")
    assert [e["route"] for e in first.get("slate", [])] == [e["route"] for e in second.get("slate", [])]


# ------------------------------------------------------------------ preference and cost (reviewer route)

def _eligible(cands):
    return [c for c in cands if _qualifies(c, "review")]


@given(cands=_slates, policy=st.sampled_from(["balanced", "money_saver", "quota_saver"]))
def test_a_reviewer_is_chosen_from_the_candidates_that_pass_every_gate(cands, policy):
    decision = routing.route(_reviewer(cands, {"cost_policy": policy}))
    eligible = {routing.candidate_id(c) for c in _eligible(cands)}
    if eligible:
        assert decision["status"] == "selected" and decision["selected"] in eligible
    else:
        assert decision["status"] == "no_qualifying_candidate"


@given(cands=_slates, seed=st.lists(st.fixed_dictionaries({"model_id": st.sampled_from(["m1", "m2", "m3", "m4"])},
                                                          optional={"effort": st.sampled_from(["medium", "high"])}),
                                    min_size=1, max_size=4))
def test_a_preferred_reviewer_is_never_passed_over_for_one_further_down_the_seed(cands, seed):
    eligible = _eligible(cands)
    assume(eligible)
    rank = {routing.candidate_id(c): routing.preferred_rank(c, seed) for c in eligible}
    decision = routing.route(_reviewer(cands, preferred_seed=seed))
    worst = len(seed)
    chosen = rank[decision["selected"]] if rank[decision["selected"]] is not None else worst
    assert chosen == min(r if r is not None else worst for r in rank.values())


@given(cands=_slates)
def test_money_saver_picks_a_cheapest_route_and_quota_saver_a_lightest_one(cands):
    eligible = _eligible(cands)
    assume(eligible)
    by_id = {routing.candidate_id(c): c for c in eligible}
    cheapest = routing.route(_reviewer(cands, {"cost_policy": "money_saver"}))["selected"]
    assert by_id[cheapest]["cost"]["money_estimate"] == min(c["cost"]["money_estimate"] for c in eligible)
    lightest = routing.route(_reviewer(cands, {"cost_policy": "quota_saver"}))["selected"]
    assert by_id[lightest]["cost"]["quota_burn"] == min(c["cost"]["quota_burn"] for c in eligible)
