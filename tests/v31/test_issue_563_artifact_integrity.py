"""#563: a PASS gate and evidence row do not certify missing/tampered logs."""
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from office import paths, verification_coverage as vc

CMD = "pytest tests/ --all -q"


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    rid = "run-test"
    root = tmp_path / "run"
    anchor = root / "evidence" / "integration" / ("I" + "a" * 7)
    anchor.mkdir(parents=True)
    log = anchor / "check-1.log"
    log.write_bytes(b"42 selected tests passed\n")
    monkeypatch.setattr(paths, "run_dir", lambda run_id: root)
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE gates (id TEXT, run_id TEXT, kind TEXT, status TEXT, verdict TEXT, input_key TEXT, revision_id TEXT)")
    con.execute("CREATE TABLE evidence (run_id TEXT, gate_id TEXT, kind TEXT, revision_id TEXT, path TEXT, bytes INTEGER, sha256 TEXT, meta_json TEXT)")
    sha = "a" * 40
    con.execute("INSERT INTO gates VALUES (?,?,?,?,?,?,?)", ("G1", rid, "checks", "done", "PASS", "integration:" + sha, "I" + "a" * 7))
    con.execute("INSERT INTO evidence VALUES (?,?,?,?,?,?,?,?)",
                (rid, "G1", "check_output", "I" + "a" * 7, str(log), log.stat().st_size,
                 "sha256:" + hashlib.sha256(log.read_bytes()).hexdigest(),
                 json.dumps({"command": CMD, "exit": 0})))
    con.commit()
    run = {"id": rid, "landing": {"integration": {"full_suite": {"commit": sha, "gate": "G1", "command": CMD}}}}
    return con, run, sha, log, root


@pytest.mark.parametrize("tamper", [
    "delete", "rewrite", "truncate", "sha", "length", "path", "symlink",
    "revision", "gate", "verdict", "command", "run", "wrong_revision_folder",
    "gate_revision",
])
def test_missing_or_inconsistent_artifact_cannot_attest(evidence, tmp_path, tamper):
    con, run, sha, log, root = evidence
    assert vc.attested_checks(con, run, sha) == [CMD]
    if tamper == "delete":
        log.unlink()
    elif tamper == "rewrite":
        log.write_bytes(b"42 selected tests failed\n")
    elif tamper == "truncate":
        log.write_bytes(b"")
    elif tamper == "sha":
        con.execute("UPDATE evidence SET sha256=?", ("sha256:" + "f" * 64,))
    elif tamper == "length":
        con.execute("UPDATE evidence SET bytes=bytes+1")
    elif tamper == "path":
        outside = tmp_path / "outside-check.log"
        outside.write_bytes(log.read_bytes())
        con.execute("UPDATE evidence SET path=?", (str(outside),))
    elif tamper == "symlink":
        target = log.with_name("content.log")
        log.rename(target)
        log.symlink_to(target)
    elif tamper == "wrong_revision_folder":
        another = root / "evidence" / "integration" / "other-revision"
        another.mkdir()
        copied = another / "check-1.log"
        copied.write_bytes(log.read_bytes())
        con.execute("UPDATE evidence SET path=?", (str(copied),))
    elif tamper == "revision":
        con.execute("UPDATE evidence SET revision_id='other'")
    elif tamper == "gate_revision":
        con.execute("UPDATE gates SET revision_id='Iwrong'")
        con.execute("UPDATE evidence SET revision_id='Iwrong'")
    elif tamper == "gate":
        con.execute("UPDATE evidence SET gate_id='unrelated'")
    elif tamper == "verdict":
        con.execute("UPDATE gates SET verdict='FAILED'")
    elif tamper == "command":
        con.execute("UPDATE evidence SET meta_json=?", ('{"command":"pytest -q","exit":0}',))
    elif tamper == "run":
        con.execute("UPDATE evidence SET run_id='other'")
    con.commit()
    assert vc.attested_checks(con, run, sha) == []


def test_artifact_can_be_restored_and_verified_again(evidence):
    con, run, sha, log, root = evidence
    original = log.read_bytes()
    log.write_bytes(b"corrupted")
    assert vc.attested_checks(con, run, sha) == []
    log.write_bytes(original)
    assert vc.attested_checks(con, run, sha) == [CMD]


def test_wrong_composed_sha_and_proof_gate_never_attest(evidence):
    con, run, sha, log, root = evidence
    assert vc.attested_checks(con, run, "b" * 40) == []
    run["landing"]["integration"]["full_suite"]["gate"] = "other"
    assert vc.attested_checks(con, run, sha) == []
