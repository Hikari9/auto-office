"""`db.connect` under concurrent first opens of one runs.db (#494 C3).

The first connection to a new file switches it to WAL. That switch can fail with
`database is locked` without waiting for the busy timeout while another connection
is creating the file, so callers racing a first open used to crash instead of
sharing the database.
"""
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from office import db  # noqa: E402


_OPENER = """
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from office import db
while time.time() < float(sys.argv[3]):
    pass
try:
    con = db.connect(Path(sys.argv[2]))
except BaseException as exc:
    print("ERR", repr(exc))
    sys.exit(1)
print(con.execute("PRAGMA journal_mode").fetchone()[0])
"""


def _race(path: Path, n: int) -> list[str]:
    """`n` separate processes open the same not-yet-existing database at the same instant.
    Processes, not threads: the lock is taken across processes, and threads of one
    process did not reproduce it."""
    go = time.time() + 1.5
    procs = [subprocess.Popen([sys.executable, "-c", _OPENER, str(ROOT / "src"), str(path), str(go)],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) for _ in range(n)]
    return [p.communicate(timeout=120)[0].strip() for p in procs]


def test_concurrent_first_opens_of_one_database_all_succeed_in_wal_mode(tmp_path):
    for round_ in range(3):  # a fresh file each round: only a first open races the switch
        path = tmp_path / f"round{round_}" / "runs.db"
        outcomes = _race(path, 8)
        assert outcomes == ["wal"] * 8, outcomes
        con = db.connect(path)  # migration ran to one correct schema, triggers included
        assert db._schema_version(con) == db.SCHEMA_VERSION and not db._drifted(con)
        assert con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
                           "AND tbl_name='route_discovery_events'").fetchone()[0] >= 1
        con.close()


class _FlakyConnection:
    """A real connection that refuses the first WAL switches the way a racing opener sees."""

    def __init__(self, con: sqlite3.Connection, refusals: int, message: str = "database is locked"):
        self._con, self.refusals, self.message, self.switches = con, refusals, message, 0

    def execute(self, sql, *args):
        if sql.strip().upper() == "PRAGMA JOURNAL_MODE=WAL":
            self.switches += 1
            if self.switches <= self.refusals:
                raise sqlite3.OperationalError(self.message)
        return self._con.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._con, name)

    @property
    def row_factory(self):
        return self._con.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._con.row_factory = value


def _flaky(monkeypatch, refusals, message="database is locked"):
    made = []
    real = sqlite3.connect

    def connect(*a, **kw):
        flaky = _FlakyConnection(real(*a, **kw), refusals, message)
        made.append(flaky)
        return flaky
    monkeypatch.setattr(db.sqlite3, "connect", connect)
    monkeypatch.setattr(db.time, "sleep", lambda s: None)
    return made


def test_a_locked_wal_switch_is_retried_until_it_succeeds(tmp_path, monkeypatch):
    made = _flaky(monkeypatch, refusals=3)
    con = db.connect(tmp_path / "runs.db")
    assert made[0].switches == 4
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_a_database_that_stays_locked_raises_after_a_bounded_number_of_tries(tmp_path, monkeypatch):
    made = _flaky(monkeypatch, refusals=10 ** 6)
    with pytest.raises(RuntimeError, match="could not enter WAL mode") as raised:
        db.connect(tmp_path / "runs.db")
    assert isinstance(raised.value.__cause__, sqlite3.OperationalError)
    assert made[0].switches == 40


def test_a_database_that_will_not_switch_to_wal_raises_runtime_error(tmp_path, monkeypatch):
    real = sqlite3.connect

    class Stuck(_FlakyConnection):
        def execute(self, sql, *args):
            if sql.strip().upper() == "PRAGMA JOURNAL_MODE=WAL":
                return self._con.execute("PRAGMA journal_mode=DELETE")
            return self._con.execute(sql, *args)

    monkeypatch.setattr(db.sqlite3, "connect", lambda *a, **kw: Stuck(real(*a, **kw), 0))
    with pytest.raises(RuntimeError, match="could not enter WAL mode"):
        db.connect(tmp_path / "runs.db")


def test_an_unrelated_error_is_not_retried(tmp_path, monkeypatch):
    made = _flaky(monkeypatch, refusals=10 ** 6, message="disk I/O error")
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        db.connect(tmp_path / "runs.db")
    assert made[0].switches == 1


def test_a_database_already_in_wal_mode_is_not_switched_again(tmp_path, monkeypatch):
    db.connect(tmp_path / "runs.db").close()
    made = _flaky(monkeypatch, refusals=10 ** 6)  # any switch attempt would now fail the open
    db.connect(tmp_path / "runs.db").close()
    assert made[0].switches == 0
