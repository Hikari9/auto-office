"""#541: accepted revisions with orphaned dispatch statuses cannot strand an upgrade."""
import sqlite3

from office import upgrade


def _con():
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript("""
      CREATE TABLE dispatches (id TEXT, run_id TEXT, kind TEXT, task_id TEXT,
                              status TEXT, ended_at TEXT, started_at TEXT);
      CREATE TABLE tasks (run_id TEXT, id TEXT, status TEXT, accepted_producer_dispatch_id TEXT);
      CREATE TABLE leases (run_id TEXT, task_id TEXT, released_at TEXT, revoked_at TEXT);
      CREATE TABLE outbox (run_id TEXT, status TEXT, kind TEXT, id TEXT);
      INSERT INTO tasks VALUES ('R','T1','accepted','D1');
      INSERT INTO dispatches VALUES ('D1','R','executor','T1','running',NULL,'2026-01-01');
    """)
    return con


def test_orphaned_running_dispatch_on_accepted_revision_no_longer_blocks():
    con = _con()
    assert upgrade.blockers(con, "R") == []
    con.execute("UPDATE tasks SET status='changes_required'")
    assert "dispatch D1" in upgrade.blockers(con, "R")[0]


def test_live_scope_lease_still_blocks_even_if_task_accepted():
    con = _con()
    con.execute("INSERT INTO leases VALUES ('R','T1',NULL,NULL)")
    assert "dispatch D1" in upgrade.blockers(con, "R")[0]
    con.execute("UPDATE leases SET released_at='2026-01-02'")
    assert upgrade.blockers(con, "R") == []
