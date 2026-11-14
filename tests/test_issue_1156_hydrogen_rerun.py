"""Regression tests for #1156: Hydrogen manual_conformance on plain sqlite3 connections."""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from office import db, hydrogen_trials, route_probe, routing  # noqa: E402
from office.util import now_iso  # noqa: E402


def _hydrogen_candidate() -> dict:
    return {
        **route_probe.candidate_from_spec("agy/gemini-4-hydrogen@medium"),
        "harness_version": "1.2.3",
        "capabilities": ["code"],
        "dispatchable": True,
        "quota": {"status": "ok"},
    }


def _seed_probe_row(con, cand: dict, *, result: str) -> None:
    adapter = route_probe._adapter(cand)
    fp = route_probe.fingerprint(cand, adapter)
    probe_key = route_probe.key(cand, adapter)
    con.execute(
        "INSERT OR REPLACE INTO route_probes(key, harness, harness_version, adapter_hash, profile, "
        "invocation_model_id, effort, result, reason_class, detail, probed_at, run_id, dispatch_id, attempt_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            probe_key,
            fp["harness"],
            fp["harness_version"],
            fp["adapter_hash"],
            fp["profile"],
            fp["invocation_model_id"],
            fp["effort"],
            result,
            None if result == "pass" else "conformance-failed",
            f"seeded {result}",
            now_iso(),
            "run-1156",
            None,
            "att-1156",
        ),
    )
    con.commit()


def test_manual_conformance_plain_connection_without_and_with_seeded_pass(tmp_path):
    db_path = tmp_path / "runs.db"
    with db.connect(db_path):
        pass

    cand = _hydrogen_candidate()
    plain = sqlite3.connect(db_path)
    try:
        assert plain.row_factory is None
        refusal = hydrogen_trials.manual_conformance(plain, cand, role="executor", size="S")
        assert refusal == "Hydrogen manual route needs a fresh exact harness/model/effort conformance receipt"

        _seed_probe_row(plain, cand, result="fail")
        refusal = hydrogen_trials.manual_conformance(plain, cand, role="executor", size="S")
        assert refusal == "Hydrogen manual route needs a fresh exact harness/model/effort conformance receipt"

        _seed_probe_row(plain, cand, result="pass")
        assert hydrogen_trials.manual_conformance(plain, cand, role="executor", size="S") is None
    finally:
        plain.close()


def test_routing_route_manual_hydrogen_uses_plain_connect_without_crashing(tmp_path):
    db_path = tmp_path / "runs.db"
    with db.connect(db_path) as con:
        cand = _hydrogen_candidate()
        cand["route_status"] = "probe-passed"
        _seed_probe_row(con, cand, result="fail")

    req = {
        "role": "executor",
        "run_gear": "S",
        "size_class": "S",
        "manual_route": True,
        "declared_route": routing.candidate_id(cand),
        "runs_db": str(db_path),
        "candidates": [cand],
    }

    decision_fail = routing.route(req)
    assert decision_fail.get("selected") is None
    assert any(r.get("category") == "hydrogen-safety" for r in decision_fail.get("rejected", []))

    with db.connect(db_path) as con:
        _seed_probe_row(con, cand, result="pass")

    decision_pass = routing.route(req)
    assert decision_pass["selected"] == routing.candidate_id(cand)
    assert {key: decision_pass["candidate"][key] for key in ("harness", "model_id", "effort")} == {
        key: cand[key] for key in ("harness", "model_id", "effort")
    }
    assert not any(r.get("category") == "hydrogen-safety" for r in decision_pass.get("rejected", []))
