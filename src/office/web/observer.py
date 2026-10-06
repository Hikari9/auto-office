"""Read-only access to runs.db.

The observer never goes through `db.connect`: that path creates directories,
switches the journal mode and runs `db.migrate`, all of which write. It opens
the file as a read-only URI, turns on `query_only`, and reads each projection
inside one deferred read transaction. Under WAL that transaction pins one
snapshot, so a writer committing in between cannot produce a torn view.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import quote


def open_readonly(path: str | Path) -> sqlite3.Connection:
    """Open runs.db read-only. Refuses a missing file instead of creating it."""
    db_path = Path(path).expanduser().resolve()
    if not db_path.is_file():
        raise FileNotFoundError(f"runs.db not found: {db_path}")
    con = sqlite3.connect(f"file:{quote(str(db_path))}?mode=ro", uri=True, timeout=30,
                          isolation_level=None, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA query_only=ON")
    return con


class Snapshot:
    """One read transaction plus the schema it was read under."""

    def __init__(self, con: sqlite3.Connection):
        self.con = con
        self.tables: dict[str, set[str]] = {}
        for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            self.tables[name] = {r[1] for r in con.execute(f"PRAGMA table_info({_ident(name)})")}

    def has(self, table: str, column: str | None = None) -> bool:
        cols = self.tables.get(table)
        return cols is not None and (column is None or column in cols)

    def rows(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(r) for r in self.con.execute(sql, params).fetchall()]

    def table(self, table: str, where: str = "", params: tuple = (), order: str = "rowid") -> list[dict]:
        """Every row of `table` (with its rowid as `_rowid`), or [] when the table is absent."""
        if not self.has(table):
            return []
        sql = f"SELECT rowid AS _rowid, * FROM {_ident(table)}"
        if where:
            sql += f" WHERE {where}"
        return self.rows(f"{sql} ORDER BY {order}", params)


@contextmanager
def read_snapshot(con: sqlite3.Connection) -> Iterator[Snapshot]:
    """A consistent snapshot: BEGIN (deferred) and a first read pin it."""
    con.execute("BEGIN")
    try:
        yield Snapshot(con)
    finally:
        con.execute("ROLLBACK")


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class Observer:
    """Builds projections from one runs.db, each inside its own snapshot.

    `host_id`, `fs`, `probes` and `repo_slugs` are injected so reads never
    create state and tests never shell out (see `projection.Context`).
    """

    def __init__(self, path: str | Path, **context):
        from office.web.projection import Context
        self.path = Path(path)
        self.ctx = Context(**context)
        self._con: sqlite3.Connection | None = None

    @property
    def con(self) -> sqlite3.Connection:
        if self._con is None:
            self._con = open_readonly(self.path)
        return self._con

    def read(self, fn: Callable[[Snapshot], object]):
        with read_snapshot(self.con) as snap:
            return fn(snap)

    def workspace(self) -> dict:
        from office.web import projection
        return self.read(lambda s: projection.workspace(s, self.ctx))

    def run(self, run_id: str) -> dict | None:
        from office.web import projection
        return self.read(lambda s: projection.run(s, self.ctx, run_id))

    def activity(self, run_id: str, *, limit: int | None = None, before_seq: int | None = None) -> dict:
        from office.web import activity
        return self.read(lambda s: activity.window(s, run_id, limit=limit, before_seq=before_seq, home=self.ctx.home))

    def close(self) -> None:
        if self._con is not None:
            self._con.close()
            self._con = None
