"""#811: a qualifying GPT-6.1 Sol/Terra route supersedes GPT-5.6 of the same lineage and effort.

Pure routing tests over the shipped catalog's Sol/Terra rows. A 6.1 row is
made "available" here to stand in for a passed conformance probe; every other
gate (denial, overkill, trust, capability, quota, discovery, learned
eligibility, user seeds and declared routes) runs as shipped. No harness, no network.
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from office import adaptive, candidates, model_lineages, paths, route_learning, routing, scoring

IDX = "Artificial Analysis Intelligence Index v4.3.2"
LINEAGES = {"sol": ("gpt-5.6-sol", "gpt-6.1-sol"), "terra": ("gpt-5.6-terra", "gpt-6.1-terra")}
ROWS = {(r["model_id"], r["effort"]): r for r in candidates.catalog_rows()
        if r["model_id"] in {m for pair in LINEAGES.values() for m in pair}}
# Efforts both generations of a lineage carry; 6.1 Sol has no `none` row.
SHARED = [(name, effort) for name, (old, new) in LINEAGES.items()
          for effort in ("none", "low", "medium", "high", "xhigh", "max")
          if (old, effort) in ROWS and (new, effort) in ROWS]


def cand(model, effort, *, quota=60, caps=("builder",), status="available"):
    """A routing candidate built from the shipped catalog row, as candidates.py builds one."""
    row = ROWS[(model, effort)]
    return {
        "harness": "codex", "harness_version": "0.150.0", "model_id": model, "invocation_model_id": model,
        "invocation_source": row.get("invocation_source"), "effort": effort,
        "lineage_key": row.get("lineage_key"), "generation_version": row.get("generation_version"),
        "benchmark_indexes": row["benchmark_indexes"], "capabilities": list(caps),
        "price_fields": row["price_fields"], "speed_fields": row["speed_fields"],
        "cost": {"money_estimate": row["price_fields"]["output_per_mtok"]},
        "route_status": status, "status_reason": "test",
        "quota": ({"status": "ok", "tightest_remaining_percent": quota} if quota is not None
                  else {"status": "unknown", "tightest_remaining_percent": None}),
    }


def favour(*cands, wins=40):
    """Mature local evidence that makes each of `cands` look far better than the rest."""
    routes = {route_learning.candidate_key(c): {
        "n_effective": wins, "n_raw": wins, "runs": wins, "successes": wins, "failures": 0,
        "review_rounds_mean": 0.0, "attempts_mean": 1.0, "wall_seconds_median": 30.0,
        "money_actual_median": 0.01} for c in cands}
    return {"as_of": "2026-10-27T00:00:00+00:00", "routes": routes, "pooling": {"prior_strength": 8},
            "context": {"role": "worker"}}


def req(cands, *, role="worker", seed="seed-811", evidence=None, cfg=None, **extra):
    return {"role": role, "playbook": "Change", "candidates": cands, "policy": {"cost_policy": "balanced"},
            "evidence": evidence or {"routes": {}, "pooling": {"prior_strength": 8}}, "routing_seed": seed,
            "adaptive_config": {"exploration": {"rate": 0.0}, **(cfg or {})}, **extra}


def labels(d):
    return [e["label"] for e in d["slate"]]


def lab(model, effort):
    return f"codex/{model}@{effort}"


# ------------------------------------------------------------------ catalog / lineage invariants

def test_terra_and_sol_generations_share_a_lineage_and_order_numerically():
    for old, new in LINEAGES.values():
        a, b = model_lineages.identify(old), model_lineages.identify(new)
        assert a["lineage_key"] == b["lineage_key"] and a["release_status"] == b["release_status"] == "released"
        assert model_lineages.version_parts(b["generation_version"]) > model_lineages.version_parts(a["generation_version"])
    active = {r["model_id"] for r in model_lineages.partition(candidates.catalog_rows())["active"]}
    assert {m for pair in LINEAGES.values() for m in pair} <= active


def test_terra_61_rows_match_the_documented_scores_and_stay_gated():
    expected = {"max": 48, "xhigh": 45, "high": 44, "medium": 41, "low": 36, "none": 33}
    got = {e: ROWS[("gpt-6.1-terra", e)]["benchmark_indexes"][IDX] for e in expected}
    assert got == expected
    probed = {"low", "medium", "high"}  # #909: dispatchable on recorded probe passes
    for e in expected:
        row = ROWS[("gpt-6.1-terra", e)]
        if e in probed:
            assert row.get("dispatchable") is not False, e
        else:
            # Benchmark presence is not execution proof: unprobed rows stay out of the eligible slate.
            assert row.get("dispatchable") is False and row["discovery"] == "eligible", e
        assert row["source_effort"] == ("non-reasoning" if e == "none" else e)


def test_shipped_executor_has_no_medium_only_generation_seed():
    shipped = yaml.safe_load((paths.resources_root() / "config" / "config.default.yaml").read_text())
    assert "preferred_seed" not in shipped["roles"]["executor"]


# ------------------------------------------------------------------ newer preferred, per effort

@pytest.mark.parametrize("lineage, effort", SHARED)
def test_newer_generation_takes_the_older_slot_even_when_the_older_scores_better(lineage, effort):
    old, new = LINEAGES[lineage]
    o, n = cand(old, effort), cand(new, effort)
    d = routing.route(req([o, n], evidence=favour(o)))
    assert d["routing"]["numeric_order"][0] == routing.candidate_id(o), "evidence alone ranks 5.6 first"
    assert labels(d) == [lab(new, effort), lab(old, effort)]
    assert d["selected"] == routing.candidate_id(n)
    assert d["slate"][0]["reason"] == f"newer generation of {lab(old, effort)} (same lineage and effort)"
    assert d["slate"][1]["reason"].startswith(f"superseded by {lab(new, effort)}")
    assert d["routing"]["lineage_supersession"] == [{
        "route": routing.candidate_id(n), "label": lab(new, effort), "supersedes": routing.candidate_id(o),
        "supersedes_label": lab(old, effort), "lineage_key": n["lineage_key"], "effort": effort}]


@pytest.mark.parametrize("lineage, effort", SHARED)
@pytest.mark.parametrize("seed", ["a", "b", "c", "d", "e"])
def test_close_calls_and_exploration_never_put_the_older_generation_first(lineage, effort, seed):
    old, new = LINEAGES[lineage]
    o = cand(old, effort)
    d = routing.route(req([o, cand(new, effort)], seed=seed, evidence=favour(o),
                          cfg={"competitive_band": 1.0, "exploration": {"rate": 1.0}}))
    assert labels(d)[:2] == [lab(new, effort), lab(old, effort)]


def test_supersession_is_per_effort_and_never_invents_a_missing_effort():
    # 6.1 Sol has no `none` row: Sol@none stays 5.6 while Sol@low moves to 6.1.
    assert ("gpt-6.1-sol", "none") not in ROWS
    cs = [cand("gpt-5.6-sol", "none"), cand("gpt-5.6-sol", "low"), cand("gpt-6.1-sol", "low")]
    d = routing.route(req(cs, evidence=favour(cs[0], cs[1])))
    moved = d["routing"]["lineage_supersession"]
    assert [(m["label"], m["supersedes_label"]) for m in moved] == [
        (lab("gpt-6.1-sol", "low"), lab("gpt-5.6-sol", "low"))]
    assert lab("gpt-5.6-sol", "none") in labels(d)
    # A different effort is not a successor: 6.1@high does not displace 5.6@medium.
    d = routing.route(req([cand("gpt-5.6-sol", "medium"), cand("gpt-6.1-sol", "high")],
                          evidence=favour(cand("gpt-5.6-sol", "medium"))))
    assert "lineage_supersession" not in d["routing"] and labels(d)[0] == lab("gpt-5.6-sol", "medium")


def test_older_generation_stays_in_the_slate_when_newer_routes_fill_the_top():
    # 6.1 already outranks 5.6 at every effort: without demotion the slate would be
    # three 6.1 Terra routes and no 5.6 fallback.
    efforts = ("high", "medium", "max")
    news = [cand("gpt-6.1-terra", e) for e in efforts]
    olds = [cand("gpt-5.6-terra", e) for e in efforts]
    d = routing.route(req([*olds, *news], evidence=favour(*news)))
    primary = labels(d)[0]
    assert primary.startswith("codex/gpt-6.1-terra@")
    assert labels(d)[1] == primary.replace("gpt-6.1-terra", "gpt-5.6-terra")
    assert d["slate"][1]["reason"].startswith(f"superseded by {primary}")


# ------------------------------------------------------------------ older fallback when 6.1 cannot qualify

def test_absent_newer_generation_leaves_the_older_route_selected():
    d = routing.route(req([cand("gpt-5.6-terra", "high")]))
    assert d["selected"] == routing.candidate_id(cand("gpt-5.6-terra", "high"))
    assert "lineage_supersession" not in d["routing"]


def test_denied_newer_generation_falls_back_to_older():
    pol = {"denied": ["gpt-6.1-sol"], "ceilings": {}, "overkill": [], "sources": {"denied_models": "user"}}
    d = routing.route(req([cand("gpt-5.6-sol", "high"), cand("gpt-6.1-sol", "high")], user_policies=[pol]))
    assert labels(d) == [lab("gpt-5.6-sol", "high")]
    assert any(r["stage"] == 1 and r.get("category") == "denied" for r in d["rejected"])


def test_harness_scoped_denial_of_the_newer_generation_falls_back_to_older():
    pol = {"denied": ["codex/gpt-6.1-terra@max"], "ceilings": {}, "overkill": [], "sources": {}}
    d = routing.route(req([cand("gpt-5.6-terra", "max"), cand("gpt-6.1-terra", "max")], user_policies=[pol]))
    assert labels(d) == [lab("gpt-5.6-terra", "max")]


def test_version_ceiling_denying_the_older_generation_is_respected():
    # A ceiling denies generations at or below it, so 5.6 is gone and 6.1 runs alone.
    pol = {"denied": [], "ceilings": {"openai/gpt-terra": "5.6"}, "overkill": [], "sources": {}}
    d = routing.route(req([cand("gpt-5.6-terra", "max"), cand("gpt-6.1-terra", "max")], user_policies=[pol]))
    assert labels(d) == [lab("gpt-6.1-terra", "max")] and "lineage_supersession" not in d["routing"]


def test_missing_capability_on_newer_generation_falls_back_to_older():
    d = routing.route(req([cand("gpt-5.6-sol", "medium"), cand("gpt-6.1-sol", "medium", caps=())],
                          policy={"cost_policy": "balanced", "required_capabilities": ["builder"]}))
    assert labels(d) == [lab("gpt-5.6-sol", "medium")]
    assert any(r["stage"] == 3 for r in d["rejected"])


def test_newer_generation_over_the_quota_reserve_falls_back_to_older():
    d = routing.route(req([cand("gpt-5.6-terra", "medium", quota=60), cand("gpt-6.1-terra", "medium", quota=2)]))
    assert labels(d) == [lab("gpt-5.6-terra", "medium")]
    assert any(r["stage"] == 6 for r in d["rejected"])


def test_rate_limited_newer_generation_falls_back_to_older(tmp_path):
    db = tmp_path / "runs.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE route_cooldowns(route TEXT PRIMARY KEY, expires_at TEXT)")
    later = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    con.execute("INSERT INTO route_cooldowns VALUES(?,?)", (routing.cooldown_key("codex", "gpt-6.1-sol"), later))
    con.commit(); con.close()
    d = routing.route(req([cand("gpt-5.6-sol", "xhigh"), cand("gpt-6.1-sol", "xhigh")], runs_db=str(db)))
    assert labels(d) == [lab("gpt-5.6-sol", "xhigh")]


def test_undiscovered_newer_generation_stays_out_of_the_slate():
    # The shipped state: 6.1 is discovery-eligible but not dispatchable until an exact probe passes.
    d = routing.route(req([cand("gpt-5.6-terra", "low"), cand("gpt-6.1-terra", "low", status="discovered-unconfirmed")]))
    assert labels(d) == [lab("gpt-5.6-terra", "low")]
    assert routing.candidate_id(cand("gpt-6.1-terra", "low")) not in d["qualifying"]


def test_learned_ineligible_newer_generation_falls_back_to_older():
    n = cand("gpt-6.1-sol", "high")
    d = routing.route(req([cand("gpt-5.6-sol", "high"), n], learned_eligibility={
        route_learning.candidate_key(n): {"state": "learned-ineligible", "event_id": "LE811"}}))
    assert labels(d) == [lab("gpt-5.6-sol", "high")]


def _trust_db(tmp_path, *proven):
    db = tmp_path / "runs.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE dispatches(id TEXT, attribution TEXT, triple TEXT, money_estimate REAL, money_actual REAL)")
    con.execute("CREATE TABLE outcome_labels(dispatch_id TEXT, label TEXT, primary_attribution TEXT, evidence_hash TEXT)")
    con.execute("CREATE TABLE findings(dispatch_id TEXT, status TEXT, severity TEXT, evidence_hash TEXT)")
    scoring.ensure_trust_schema(con)
    for i, c in enumerate(proven):
        con.execute("INSERT INTO adapter_trust_acts VALUES (?,?,'proven','rico','test','r1','2026-10-01T00:00:00+00:00')",
                    (f"a{i}", routing.candidate_id(c)))
    con.commit(); con.close()
    return str(db)


def test_newer_executor_route_without_trust_history_is_neutral_and_eligible(tmp_path):
    """#837: missing historical trust never vetoes a route; it is neutral and stays in the slate."""
    o, n = cand("gpt-5.6-terra", "high"), cand("gpt-6.1-terra", "high")
    d = routing.route(req([o, n], role="worker", runs_db=_trust_db(tmp_path, o)))
    assert lab("gpt-6.1-terra", "high") in labels(d) and lab("gpt-5.6-terra", "high") in labels(d)
    assert not any(r["stage"] == 2 for r in d["rejected"])


def test_proven_newer_executor_route_supersedes(tmp_path):
    o, n = cand("gpt-5.6-terra", "high"), cand("gpt-6.1-terra", "high")
    d = routing.route(req([o, n], role="worker", runs_db=_trust_db(tmp_path, o, n), evidence=favour(o)))
    assert labels(d) == [lab("gpt-6.1-terra", "high"), lab("gpt-5.6-terra", "high")]


# ------------------------------------------------------------------ user pins and gear constraints

def test_user_seeded_older_route_is_not_superseded():
    o, n = cand("gpt-5.6-sol", "high"), cand("gpt-6.1-sol", "high")
    d = routing.route(req([o, n], evidence=favour(o),
                          preferred_seed=[{"harness": "codex", "model_id": "gpt-5.6-sol", "effort": "high"}]))
    assert labels(d)[0] == lab("gpt-5.6-sol", "high") and "lineage_supersession" not in d["routing"]


def test_policy_seeded_older_route_is_not_superseded():
    o, n = cand("gpt-5.6-sol", "high"), cand("gpt-6.1-sol", "high")
    pol = {"cost_policy": "balanced", "preferred_seed": [{"model_id": "gpt-5.6-sol", "effort": "high"}]}
    d = routing.route(req([o, n], evidence=favour(o), policy=pol))
    assert labels(d)[0] == lab("gpt-5.6-sol", "high") and "lineage_supersession" not in d["routing"]


def test_seed_listing_newer_first_still_supersedes():
    o, n = cand("gpt-5.6-sol", "high"), cand("gpt-6.1-sol", "high")
    seed = [{"model_id": "gpt-6.1-sol", "effort": "high"}, {"model_id": "gpt-5.6-sol", "effort": "high"}]
    d = routing.route(req([o, n], evidence=favour(o), preferred_seed=seed))
    assert labels(d) == [lab("gpt-6.1-sol", "high"), lab("gpt-5.6-sol", "high")]


def test_declared_older_route_is_not_superseded(tmp_path):
    o, n = cand("gpt-5.6-terra", "medium"), cand("gpt-6.1-terra", "medium")
    d = routing.route(req([o, n], role="worker", runs_db=_trust_db(tmp_path, n), evidence=favour(o),
                          declared_route=routing.candidate_id(o)))
    assert labels(d)[0] == lab("gpt-5.6-terra", "medium")
    assert d["routing"]["candidates"][0]["eligibility"]["source"] == "declared"


def test_gear_that_holds_routes_to_an_older_seed_keeps_them_there():
    # allow_advisory_undercut=false retains only seeded routes; supersession never re-adds a removed one.
    o, n = cand("gpt-5.6-sol", "medium"), cand("gpt-6.1-sol", "medium")
    d = routing.route(req([o, n], allow_advisory_undercut=False,
                          preferred_seed=[{"model_id": "gpt-5.6-sol", "effort": "medium"}]))
    assert labels(d) == [lab("gpt-5.6-sol", "medium")]
    assert any(r["stage"] == 7 and "advisory anchor" in r["reason"] for r in d["rejected"])


@pytest.mark.parametrize("size, expected", [("XS", "gpt-5.6-sol"), ("S", "gpt-5.6-sol"), ("M", "gpt-6.1-sol"),
                                            ("L", "gpt-6.1-sol"), ("XL", "gpt-6.1-sol")])
def test_size_scoped_overkill_rule_decides_before_supersession(size, expected):
    pol = {"denied": [], "ceilings": {}, "sources": {},
           "overkill": [{"route": "codex/gpt-6.1-sol", "roles": ["worker"], "size_classes": ["XS", "S"]}]}
    d = routing.route(req([cand("gpt-5.6-sol", "medium"), cand("gpt-6.1-sol", "medium")],
                          user_policies=[pol], size_class=size))
    assert labels(d)[0] == lab(expected, "medium")


# ------------------------------------------------------------------ cross-lineage comparison is untouched

def test_sol_is_not_forced_above_terra():
    st, tt = cand("gpt-6.1-sol", "medium"), cand("gpt-6.1-terra", "medium")
    olds = [cand("gpt-5.6-sol", "medium"), cand("gpt-5.6-terra", "medium")]
    terra_wins = routing.route(req([st, tt, *olds], evidence=favour(tt)))
    sol_wins = routing.route(req([st, tt, *olds], evidence=favour(st)))
    assert labels(terra_wins)[0] == lab("gpt-6.1-terra", "medium")
    assert labels(sol_wins)[0] == lab("gpt-6.1-sol", "medium")
    for d in (terra_wins, sol_wins):
        order = [r["label"] for r in d["routing"]["candidates"]]
        for old, new in LINEAGES.values():
            assert order.index(lab(new, "medium")) < order.index(lab(old, "medium"))


def test_other_lineages_keep_the_utility_order():
    other = {**cand("gpt-5.6-terra", "medium"), "model_id": "unlisted-model", "invocation_model_id": "unlisted-model"}
    o, n = cand("gpt-5.6-sol", "high"), cand("gpt-6.1-sol", "high")
    d = routing.route(req([other, o, n], evidence=favour(other)))
    assert labels(d)[0] == "codex/unlisted-model@medium"
    assert labels(d)[1:] == [lab("gpt-6.1-sol", "high"), lab("gpt-5.6-sol", "high")]


def test_decision_hash_covers_the_supersession_record(monkeypatch):
    plain = routing.route(req([cand("gpt-5.6-sol", "high")]))
    assert "lineage_supersession" not in plain["routing"]
    o, n = cand("gpt-5.6-sol", "high"), cand("gpt-6.1-sol", "high")
    a = routing.route(req([o, n], evidence=favour(o)))
    assert a["decision_hash"] == routing.route(req([o, n], evidence=favour(o)))["decision_hash"]
    assert a["routing"]["lineage_supersession"]
    # Same reorder, record withheld: only the record differs, so the hash must too.
    real = adaptive._supersede
    monkeypatch.setattr(adaptive, "_supersede", lambda rows, by_id, request: (real(rows, by_id, request), [])[1])
    silent = routing.route(req([o, n], evidence=favour(o)))
    assert labels(silent) == labels(a) and "lineage_supersession" not in silent["routing"]
    assert silent["decision_hash"] != a["decision_hash"]
