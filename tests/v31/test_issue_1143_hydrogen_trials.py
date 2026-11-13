"""#1143: peer preferences, default-off Hydrogen safety and atomic canary ledger."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import pytest
import yaml

from office import adaptive, candidates, db, executor_slates, routing
from office import hydrogen_trials as h


@pytest.fixture
def database(tmp_path):
    return tmp_path / "runs.db"


def _book(path: Path, run: str, *, experiment: bool = True):
    with closing(db.connect(path)) as con:
        with db.transaction(con):
            return h.book(con, run, "T1", "claude/sonnet@medium", experiment=experiment,
                          fallback_route="claude/sonnet@medium")


def _warm(path: Path, n: int = 19):
    for i in range(n):
        assert not _book(path, f"prior-{i}", experiment=False)["experimental"]


def test_cold_start_is_conservative_and_does_not_invent_opportunities(database):
    for i in range(19):
        r = _book(database, f"cold-{i}")
        assert r["blocked"] == "cold-start"
        assert not r["experimental"]
    r = _book(database, "canary-first")
    assert r["experimental"] and r["prior"]["previous"] == 19


def test_rolling_twenty_is_never_above_three_and_counts_failed_reservations(database):
    _warm(database)
    attempts = [_book(database, f"unique-{i}") for i in range(9)]
    assert sum(x["experimental"] for x in attempts) == 3
    with closing(db.connect(database)) as con:
        rows = [x[0] for x in con.execute("SELECT experimental FROM hydrogen_trial_opportunities ORDER BY seq")]
        assert all(sum(rows[i:i + 20]) <= 3 for i in range(len(rows) - 19))
        # A launched/reserved attempt's permanent record counts even if later failed.
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            con.execute("DELETE FROM hydrogen_trial_opportunities WHERE experimental=1")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            con.execute("UPDATE hydrogen_trial_opportunities SET experimental=0")


def test_one_hydrogen_trial_per_run_despite_retry(database):
    _warm(database)
    assert _book(database, "same-run")["experimental"]
    r = _book(database, "same-run")
    assert r["blocked"] == "one-trial-per-run"
    assert not r["experimental"]
    assert _book(database, "other-run")["experimental"]


def test_multiple_connections_reserve_atomically_under_contention(database):
    _warm(database)
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda x: _book(database, f"parallel-{x}"), range(12)))
    assert sum(x["experimental"] for x in results) == 3
    with closing(db.connect(database)) as con:
        rows = [r[0] for r in con.execute("SELECT experimental FROM hydrogen_trial_opportunities ORDER BY seq")]
        assert all(sum(rows[i:i + 20]) <= 3 for i in range(len(rows) - 19))


def test_atomic_booking_requires_writer_transaction(database):
    with closing(db.connect(database)) as con:
        with pytest.raises(RuntimeError, match="transaction"):
            h.book(con, "r", "T1", "route", experiment=True)


def test_seed_peers_are_equal_weight_and_arrows_remain_distinct():
    expected = {"XS": [1, 2, 2], "S": [1, 1, 2], "M": [1, 1, 2],
                "L": [1, 2], "XL": [1, 2, 2]}
    for size, rungs in expected.items():
        seed = executor_slates.ideal_seed(size)
        assert [entry["rung"] for entry in seed] == rungs
        for entry in seed:
            c = {"model_id": entry["model_id"], "effort": entry["effort"],
                 "harness": entry.get("harness")}
            assert routing.preferred_rank(c, seed) == entry["rung"] - 1
            assert adaptive._preference_component(c, seed)[0] == 1 / entry["rung"]
    assert [x["model_id"] for x in executor_slates.ideal_seed("L")] == [
        "claude-sonnet-5-5", "gemini-4-hydrogen"]
    assert not any("flash" in x["model_id"] for s in expected for x in executor_slates.ideal_seed(s))
    assert executor_slates.POLICY["benchmark_index"] == "Artificial Analysis Intelligence Index v4.3.2"


def test_unknown_size_is_not_l_implicitly():
    out = routing.route({"role": "executor", "context": {"gear": "L"}, "candidates": []})
    assert out["status"] == "size_unclassified"
    assert "classify" in out["action"]


def test_new_executor_without_gear_or_size_must_explicitly_classify():
    decision = routing.route({"role": "executor", "context": {}, "candidates": [],
                              "require_classified_executor_size": True})
    assert decision["status"] == "size_unclassified"
    legacy = routing.route({"role": "executor", "context": {}, "candidates": [],
                            "require_classified_executor_size": False})
    assert legacy["status"] != "size_unclassified"


def test_hydrogen_never_qualifies_from_generic_allow_or_unverified_effort():
    c = {"model_id": h.MODEL, "harness": "agy", "effort": "medium"}
    assert h.route_refusal(c, role="executor", size="S", manual=False)
    assert h.route_refusal(c, role="executor", size="XL", manual=True)
    assert h.route_refusal(c, role="visual_reviewer", size="S", manual=True)
    assert h.route_refusal(c, role="executor", size="XS", manual=False)
    assert h.route_refusal(c, role="executor", size="M", manual=True)
    assert h.route_refusal(c, role="executor", size="S", manual=True) is None
    assert h.manual_conformance(None, c, role="executor", size="XL")


def test_risk_gate_only_bounded_reversible_local_s_m():
    risk = {"size_class": "S", "blast_radius": "repo", "irreversible": False}
    desc = {"task_size": "S", "bounds": "bounded", "domain": "ui"}
    assert h.safe_task("S", risk, desc)
    assert not h.safe_task("S", {**risk, "blast_radius": "production"}, desc)
    assert not h.safe_task("S", risk, {**desc, "domain": "security"})
    assert not h.safe_task("S", risk, {**desc, "bounds": "unknown"})
    assert not h.safe_task("S", risk, {**desc, "task_size": "M"})
    assert not h.safe_task("S", risk, {**desc, "domain": "finance-system"})
    assert not h.safe_task("S", risk, {**desc, "domain": ""})
    assert not h.safe_task("S", risk, {**desc, "risk_category": "credentials"})


def test_run_needs_independent_review_and_owner_switch():
    config = {"routing": {"hydrogen_trials": {"enabled": True}}}
    task = {"descriptor": {"task_size": "M", "bounds": "bounded", "domain": "feature"}}
    run = {"risk": {"size_class": "M", "blast_radius": "local", "irreversible": False},
           "gates": {"code_review": True}}
    assert h.run_safe(config, run, task)
    assert not h.run_safe({"routing": {"hydrogen_trials": {"enabled": False}}}, run, task)
    assert not h.run_safe(config, {**run, "gates": {"code_review": False}}, task)


def test_verified_quota_pool_requires_exact_provider_and_account():
    base = {"model_id": "codex-one", "harness": "codex", "effort": "medium"}
    q = {"status": "ok", "tightest_remaining_percent": 15, "provider": "openai",
         "account_id": "work", "pool_id": "plan", "shared_pool_verified": True}
    a = {**base, "quota": q}
    b = {**base, "model_id": "codex-two", "quota": q}
    assert routing.verified_shared_quota_pool_key(a) == routing.verified_shared_quota_pool_key(b)
    assert routing.verified_shared_quota_pool_key(a) in routing._demotion_keys(a)
    assert routing.verified_shared_quota_pool_key(a) != routing.verified_shared_quota_pool_key(
        {**b, "quota": {**q, "account_id": "personal"}})
    assert routing.verified_shared_quota_pool_key({**a, "quota": {**q, "shared_pool_verified": False}}) is None
    assert routing.verified_shared_quota_pool_key({**a, "quota": {**q, "pool_id": ""}}) is None


def test_hard_quota_and_canary_schema_limits_cannot_be_relaxed():
    assert h.validate({"routing": {"hydrogen_trials": {"enabled": True}}}) == []
    assert h.validate({"routing": {"hydrogen_trials": {"max_rolling_20": 100}}})
    assert h.validate({"routing": {"hydrogen_trials": {"enabled": "true"}}})
