"""#625 alias trust approval is a deprecated no-op under #837; the audit table and its atomic writer remain."""
from __future__ import annotations

import sqlite3
import pytest

from office import adapters, authority, db, scoring
from office.state import OfficeError


@pytest.fixture
def test_db(monkeypatch):
    # Trust assertions use a fixed harness major, independent of host installs.
    monkeypatch.setattr(adapters, "harness_version", lambda adapter: "0.0.0")
    con = sqlite3.connect(":memory:")
    db.migrate(con)
    scoring.ensure_trust_schema(con)
    try:
        yield con
    finally:
        con.close()


@pytest.mark.parametrize("spec", ["codex/luna@medium", "codex@0/gpt-6-luna@high", "claude/sonnet@high", "bogus"])
def test_approve_trust_is_a_deprecated_noop_and_writes_no_act(test_db, spec):
    """#837: the approval ceremony is gone but the command still runs, so old scripts keep working.
    It warns, points at `office route allow|deny`, and leaves the audit table untouched."""
    con = test_db
    res = authority.approve(con, {"id": "run-retired-001"}, "trust", "approve it, I said so", [spec])
    assert res.data["deprecated"] is True and res.data["recorded"] is False
    assert "deprecated" in res.lines[0] and "office route allow|deny" in res.lines[0]
    assert "office route policy" in res.next
    assert con.execute("SELECT COUNT(*) FROM adapter_trust_acts").fetchone()[0] == 0


def test_historical_trust_acts_stay_readable_as_audit_evidence(test_db):
    scoring.record_trust_acts(test_db, ["codex@0/gpt-6-luna@high"], "proven", "user", "old act recorded before the retirement")
    assert test_db.execute("SELECT COUNT(*) FROM adapter_trust_acts").fetchone()[0] == 1


_FAIL_SECOND_WRITE = (
    "CREATE TRIGGER fail_concrete BEFORE INSERT ON adapter_trust_acts "
    "WHEN NEW.triple = 'codex@0/gpt-6-luna@medium' "
    "BEGIN SELECT RAISE(ABORT, 'simulated second-write failure'); END"
)


def test_record_trust_acts_is_atomic_on_a_path_connection(tmp_path):
    """Same guarantee when the helper opens (and closes) its own connection."""
    path = tmp_path / "runs.db"
    setup = sqlite3.connect(str(path))
    scoring.ensure_trust_schema(setup)
    setup.execute(_FAIL_SECOND_WRITE)
    setup.commit()

    with pytest.raises(sqlite3.DatabaseError):
        scoring.record_trust_acts(path, ["codex@0/luna@medium", "codex@0/gpt-6-luna@medium"], "proven",
                                  "user", "user authorized: atomic batch")
    assert setup.execute("SELECT COUNT(*) FROM adapter_trust_acts").fetchone()[0] == 0

    acts = scoring.record_trust_acts(path, ["codex@0/luna@medium", "codex@0/luna@high"], "proven",
                                     "user", "user authorized: atomic batch")
    assert len(acts) == 2
    assert setup.execute("SELECT COUNT(*) FROM adapter_trust_acts").fetchone()[0] == 2
    setup.close()


def test_record_trust_acts_validates_before_writing(test_db):
    with pytest.raises(ValueError):
        scoring.record_trust_acts(test_db, ["codex@0/luna@medium", "  "], "proven", "user",
                                  "user authorized: validation")
    assert test_db.execute("SELECT COUNT(*) FROM adapter_trust_acts").fetchone()[0] == 0
