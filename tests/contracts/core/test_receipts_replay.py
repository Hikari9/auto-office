"""Check artifacts attest the exact immutable revision and are replayable after repair."""
import hashlib
import json
import sqlite3

import pytest

from office import paths, verification_coverage


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    run_id = "synthetic-run"
    root = tmp_path / "run"
    digest = "a" * 40
    artifact = root / "evidence" / "integration" / ("I" + digest[:7])
    artifact.mkdir(parents=True)
    log = artifact / "check-1.log"
    log.write_bytes(b"contract check passed\n")
    monkeypatch.setattr(paths, "run_dir", lambda _: root)
    db_path = tmp_path / "runs.db"
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE gates (id TEXT, run_id TEXT, kind TEXT, status TEXT, verdict TEXT, input_key TEXT, revision_id TEXT)")
    con.execute("CREATE TABLE evidence (run_id TEXT, gate_id TEXT, kind TEXT, revision_id TEXT, path TEXT, bytes INTEGER, sha256 TEXT, meta_json TEXT)")
    con.execute("INSERT INTO gates VALUES (?,?,?,?,?,?,?)",
                 ("G1", run_id, "checks", "done", "PASS", "integration:" + digest, "I" + digest[:7]))
    con.execute("INSERT INTO evidence VALUES (?,?,?,?,?,?,?,?)",
                (run_id, "G1", "check_output", "I" + digest[:7], str(log), log.stat().st_size,
                 "sha256:" + hashlib.sha256(log.read_bytes()).hexdigest(),
                 json.dumps({"command": "pytest --all", "exit": 0})))
    con.commit()
    run = {"id": run_id, "repo_root": str(tmp_path), "landing": {"integration": {"full_suite": {"commit": digest,
            "gate": "G1", "command": "pytest --all"}}}}
    return con, run, digest, log, db_path


def test_valid_artifact_attests_only_its_bound_revision(evidence):
    con, run, digest, _, _ = evidence
    assert verification_coverage.attested_checks(con, run, digest) == ["pytest --all"]
    assert verification_coverage.attested_checks(con, run, "b" * 40) == []


@pytest.mark.parametrize("damage", ["missing", "content", "length", "digest", "revision", "verdict"])
def test_tampered_or_false_pass_evidence_is_rejected(evidence, damage):
    con, run, digest, log, _ = evidence
    if damage == "missing":
        log.unlink()
    elif damage == "content":
        log.write_bytes(b"contract check failed\n")
    elif damage == "length":
        con.execute("UPDATE evidence SET bytes=bytes+1")
    elif damage == "digest":
        con.execute("UPDATE evidence SET sha256=?", ("sha256:" + "f" * 64,))
    elif damage == "revision":
        con.execute("UPDATE evidence SET revision_id='other'")
    elif damage == "verdict":
        con.execute("UPDATE gates SET verdict='FAIL'")
    assert verification_coverage.attested_checks(con, run, digest) == []


def test_repaired_artifact_can_be_replayed_and_attested_again(evidence):
    con, run, digest, log, _ = evidence
    saved = log.read_bytes()
    log.write_bytes(b"forged\n")
    assert verification_coverage.attested_checks(con, run, digest) == []
    log.write_bytes(saved)
    assert verification_coverage.attested_checks(con, run, digest) == ["pytest --all"]


def test_gate_must_be_bound_to_the_recorded_revision(evidence):
    con, run, digest, _, _ = evidence
    con.execute("UPDATE gates SET revision_id='another-revision'")
    assert verification_coverage.attested_checks(con, run, digest) == []


def test_tamper_and_repair_replay_survives_database_reopen(evidence):
    con, run, digest, log, db_path = evidence
    original = log.read_bytes()
    log.write_bytes(b"forged receipt after restart\n")
    con.close()
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    assert verification_coverage.attested_checks(con, run, digest) == []
    con.close()
    log.write_bytes(original)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    assert verification_coverage.attested_checks(con, run, digest) == ["pytest --all"]
