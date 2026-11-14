"""Regression tests for #1156: Hydrogen manual_conformance on plain sqlite3 connections."""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from office import db, hydrogen_trials, route_probe  # noqa: E402
from office.util import now_iso  # noqa: E402


def _hydrogen_candidate() -> dict:
    return {
        **route_probe.candidate_from_spec("agy/gemini-4-hydrogen@medium"),
        "harness_version": "1.2.3",
    }


def test_manual_conformance_plain_connection_without_and_with_seeded_pass(tmp_path):
    db_path = tmp_path / "runs.db"
    with db.connect(db_path):
        pass

    cand = _hydrogen_candidate()
    adapter = route_probe._adapter(cand)
    fp = route_probe.fingerprint(cand, adapter)
    probe_key = route_probe.key(cand, adapter)

    plain = sqlite3.connect(db_path)
    try:
        assert plain.row_factory is None
        refusal = hydrogen_trials.manual_conformance(plain, cand, role="executor", size="S")
        assert refusal == "Hydrogen manual route needs a fresh exact harness/model/effort conformance receipt"

        plain.execute(
            "INSERT INTO route_probes(key, harness, harness_version, adapter_hash, profile, invocation_model_id, effort, "
            "result, reason_class, detail, probed_at, run_id, dispatch_id, attempt_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                probe_key,
                fp["harness"],
                fp["harness_version"],
                fp["adapter_hash"],
                fp["profile"],
                fp["invocation_model_id"],
                fp["effort"],
                "pass",
                None,
                "seeded pass",
                now_iso(),
                "run-1156",
                None,
                "att-1156",
            ),
        )
        plain.commit()

        assert hydrogen_trials.manual_conformance(plain, cand, role="executor", size="S") is None
    finally:
        plain.close()
