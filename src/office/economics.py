"""Orchestrator economics ledger (#502 PR 1): measured token/cache/cost usage per run.

Collection is opt-in. With `economics.collect` false (the default) nothing here
reads a session log or writes a row, and the `usage_events` table is not even
created. `office economics ingest` is the only writer; hooks never call it.

Semantics kept honest:
- input, output, cache-read and cache-write tokens are separate columns. A field
  the harness did not report is NULL, never 0.
- Claude Code reports `input_tokens` excluding cache; Codex reports
  `input_tokens` including `cached_input_tokens`, so the cached part is
  subtracted once and stored as cache-read. Codex reports no cache writes.
- cost_kind is one of actual, estimated-nominal, quota, unknown. Transcripts carry
  no prices, and there is no built-in price table: their cost is unknown.
- Rows are keyed by (harness, session, turn, event). Re-ingesting, duplicated
  or reordered events merge field-wise (the larger reported value wins), so the
  result does not depend on order or repetition.
- A long gap between turns is not evidence of a cache miss. A cold resume is
  only reported when the first measured turn after a resume wrote cache and read none.
"""
from __future__ import annotations

import json
from pathlib import Path

from office.result import Result
from office.state import Usage
from office.util import now_iso, parse_iso

LEDGER_VERSION = 1
COST_KINDS = ("actual", "estimated-nominal", "quota", "unknown")
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
# Dispatch roles that report as one role; the phase column keeps the plan/code distinction.
ROLE_GROUP = {"code_reviewer": "reviewer", "plan_reviewer": "reviewer"}

DDL = """
CREATE TABLE IF NOT EXISTS usage_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  harness TEXT NOT NULL, session_id TEXT NOT NULL, turn_id TEXT NOT NULL, event_id TEXT NOT NULL,
  run_id TEXT, dispatch_id TEXT, task_id TEXT, role TEXT, phase TEXT, model TEXT, attribution TEXT,
  occurred_at TEXT, source TEXT NOT NULL, source_path TEXT,
  input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER,
  cost_usd REAL, cost_kind TEXT NOT NULL, quota_units REAL,
  ledger_version INTEGER NOT NULL, recorded_at TEXT NOT NULL,
  UNIQUE(harness, session_id, turn_id, event_id));
CREATE INDEX IF NOT EXISTS usage_events_run ON usage_events(run_id, role, phase);
CREATE INDEX IF NOT EXISTS usage_events_session ON usage_events(harness, session_id, occurred_at)
"""


# ------------------------------------------------------------------ opt-in

def enabled(run: dict | None) -> bool:
    """True only when the live config explicitly sets economics.collect: true."""
    from office import config
    root = Path(run["repo_root"]) if run and run.get("repo_root") else None
    try:
        effective, _ = config.resolve(root)
    except Exception:
        return False
    block = effective.get("economics")
    return isinstance(block, dict) and block.get("collect") is True


def ensure_schema(con) -> None:
    for stmt in DDL.split(";"):
        if stmt.strip():
            con.execute(stmt)


def has_table(con) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='usage_events'").fetchone() is not None


# ------------------------------------------------------------------ parsers

def _int(value) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)) and value >= 0:
        return int(value)
    return None


def _lines(path: Path):
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


def parse_claude(path: Path, session_id: str | None = None) -> list[dict]:
    """Claude Code transcript: assistant entries carry message.usage. One API
    message is written once per content block with the same message.id, so the
    message id is the event identity and repeats merge."""
    out = []
    for entry in _lines(path):
        msg = entry.get("message")
        if entry.get("type") != "assistant" or not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
            continue
        usage = msg["usage"]
        event = msg.get("id") or entry.get("uuid")
        if not event:
            continue
        out.append({
            "harness": "claude", "session_id": entry.get("sessionId") or session_id or path.stem,
            "turn_id": str(entry.get("requestId") or ""), "event_id": str(event),
            "model": msg.get("model"), "occurred_at": entry.get("timestamp"),
            "input_tokens": _int(usage.get("input_tokens")), "output_tokens": _int(usage.get("output_tokens")),
            "cache_read_tokens": _int(usage.get("cache_read_input_tokens")),
            "cache_write_tokens": _int(usage.get("cache_creation_input_tokens")),
            "cost_usd": None, "cost_kind": "unknown", "source": "claude-transcript", "source_path": str(path),
        })
    return out


def parse_codex(path: Path, session_id: str | None = None) -> list[dict]:
    """Codex rollout: `token_count` events carry the last turn's usage and the
    cumulative total. The cumulative total identifies the event, so a repeated
    token_count merges. input_tokens includes the cached input: subtract it once."""
    out, model, sid = [], None, session_id
    for entry in _lines(path):
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        if entry.get("type") == "session_meta":
            sid = sid or payload.get("id")
        elif entry.get("type") == "turn_context":
            model = payload.get("model") or model
        elif entry.get("type") == "event_msg" and payload.get("type") == "token_count":
            info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
            last = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else None
            total = info.get("total_token_usage") if isinstance(info.get("total_token_usage"), dict) else {}
            ident = _int(total.get("total_tokens"))
            if last is None or ident is None:
                continue
            raw_in, cached = _int(last.get("input_tokens")), _int(last.get("cached_input_tokens"))
            # Without cached_input_tokens the raw input is all that is known: keep it, cache read stays unknown.
            fresh = raw_in if cached is None else (raw_in - cached if raw_in is not None and raw_in >= cached else None)
            out.append({
                "harness": "codex", "session_id": sid or path.stem, "turn_id": "", "event_id": f"total:{ident}",
                "model": model, "occurred_at": entry.get("timestamp"),
                "input_tokens": fresh, "output_tokens": _int(last.get("output_tokens")),
                "cache_read_tokens": cached, "cache_write_tokens": None,
                "cost_usd": None, "cost_kind": "unknown", "source": "codex-rollout", "source_path": str(path),
            })
    return out


def parse_normalized(path: Path) -> list[dict]:
    """One JSON object per line in the ledger's own field names, for harnesses
    or billing exports Office has no parser for."""
    out = []
    for n, entry in enumerate(_lines(path), 1):
        kind = entry.get("cost_kind") or "unknown"
        if kind not in COST_KINDS:
            raise Usage("bad-usage-file", f"{path}:{n}: cost_kind must be one of {', '.join(COST_KINDS)}")
        if not entry.get("harness") or not entry.get("session_id") or not entry.get("event_id"):
            raise Usage("bad-usage-file", f"{path}:{n}: harness, session_id and event_id are required")
        cost = entry.get("cost_usd")
        out.append({
            "harness": str(entry["harness"]), "session_id": str(entry["session_id"]),
            "turn_id": str(entry.get("turn_id") or ""), "event_id": str(entry["event_id"]),
            "model": entry.get("model"), "occurred_at": entry.get("occurred_at"),
            "role": entry.get("role"), "phase": entry.get("phase"),
            **{f: _int(entry.get(f)) for f in TOKEN_FIELDS},
            "cost_usd": (float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool)
                         and kind != "unknown" else None),
            "cost_kind": kind, "quota_units": entry.get("quota_units"),
            "source": "normalized-file", "source_path": str(path),
        })
    return out


PARSERS = {"claude": parse_claude, "codex": parse_codex}


def _transcripts(harness: str, session_id: str) -> list[Path]:
    from office import transcripts
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_.")
    if not safe:
        return []
    if harness == "claude":
        return sorted((transcripts.claude_home() / "projects").glob(f"*/{safe}.jsonl"))
    if harness == "codex":
        return sorted((transcripts.codex_home() / "sessions").glob(f"*/*/*/rollout-*{safe}.jsonl"))
    return []


# ------------------------------------------------------------------ writes

def _rank(col: str) -> str:
    return f"(CASE {col} WHEN 'actual' THEN 3 WHEN 'quota' THEN 2 WHEN 'estimated-nominal' THEN 1 ELSE 0 END)"


# Order-independent cost merge: actual > quota > estimated-nominal > unknown; a tie keeps the larger cost.
_NEW_COST_WINS = (f"({_rank('excluded.cost_kind')} > {_rank('cost_kind')} OR ({_rank('excluded.cost_kind')} = "
                  f"{_rank('cost_kind')} AND COALESCE(excluded.cost_usd, -1) > COALESCE(cost_usd, -1)))")

# A stored office row with no dispatch link adopts the dispatch's link (task, role, phase) from a later in-window
# transcript of the same run. An out-of-window or other-run writer never links a row, and a linked row keeps its link.
_ADOPT = ("attribution='office' AND excluded.attribution='office' AND excluded.run_id=run_id "
          "AND excluded.dispatch_id IS NOT NULL AND dispatch_id IS NULL")

_UPSERT = (
    "INSERT INTO usage_events(harness, session_id, turn_id, event_id, run_id, dispatch_id, task_id, role, phase, model, "
    "attribution, occurred_at, source, source_path, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
    "cost_usd, cost_kind, quota_units, ledger_version, recorded_at) "
    "VALUES(:harness,:session_id,:turn_id,:event_id,:run_id,:dispatch_id,:task_id,:role,:phase,:model,:attribution,"
    ":occurred_at,:source,:source_path,:input_tokens,:output_tokens,:cache_read_tokens,:cache_write_tokens,"
    ":cost_usd,:cost_kind,:quota_units,:ledger_version,:recorded_at) "
    "ON CONFLICT(harness, session_id, turn_id, event_id) DO UPDATE SET "
    + ", ".join(f"{f}=MAX(COALESCE({f}, excluded.{f}), COALESCE(excluded.{f}, {f}))" for f in TOKEN_FIELDS)
    + ", occurred_at=MIN(COALESCE(occurred_at, excluded.occurred_at), COALESCE(excluded.occurred_at, occurred_at))"
    ", model=COALESCE(model, excluded.model)"
    f", cost_usd=CASE WHEN {_NEW_COST_WINS} THEN excluded.cost_usd ELSE cost_usd END"
    f", cost_kind=CASE WHEN {_NEW_COST_WINS} THEN excluded.cost_kind ELSE cost_kind END"
    ", run_id=CASE WHEN attribution='office' THEN run_id ELSE excluded.run_id END"
    f", dispatch_id=CASE WHEN attribution='office' THEN CASE WHEN {_ADOPT} THEN excluded.dispatch_id ELSE dispatch_id END ELSE excluded.dispatch_id END"
    f", task_id=CASE WHEN attribution='office' THEN CASE WHEN {_ADOPT} THEN excluded.task_id ELSE task_id END ELSE excluded.task_id END"
    f", role=CASE WHEN attribution='office' THEN CASE WHEN {_ADOPT} THEN excluded.role ELSE role END ELSE excluded.role END"
    f", phase=CASE WHEN attribution='office' THEN CASE WHEN {_ADOPT} THEN excluded.phase ELSE phase END ELSE excluded.phase END"
    ", attribution=CASE WHEN attribution='office' THEN attribution ELSE excluded.attribution END"
)


def record(con, events: list[dict], **assoc) -> int:
    """Upsert events with their run association. Caller holds the transaction."""
    ensure_schema(con)
    now = now_iso()
    for ev in events:
        row = {"run_id": None, "dispatch_id": None, "task_id": None, "role": None, "phase": None,
               "attribution": None, "quota_units": None, **ev}
        for k, v in assoc.items():
            if v is not None and (k not in ev or ev.get(k) is None):
                row[k] = v
        if callable(row.get("attribution")):
            row["attribution"] = row["attribution"](row.get("occurred_at"))
        con.execute(_UPSERT, {**row, "ledger_version": LEDGER_VERSION, "recorded_at": now})
    return len(events)


def _window(start: str | None, end: str | None):
    """Attribute a root-session event to the run only inside its binding window."""
    def attribution(at: str | None) -> str:
        if not at or not start:
            return "outside"
        try:
            t = parse_iso(at)
            if t < parse_iso(start) or (end and t > parse_iso(end)):
                return "outside"
        except (ValueError, TypeError):
            return "outside"
        return "office"
    return attribution


def ingest(con, run: dict, *, file: str | None = None, harness: str | None = None, session: str | None = None,
           role: str | None = None) -> Result:
    from office import db
    if not enabled(run):
        return Result(lines=["economics collection is disabled (economics.collect: false); nothing was read or written"],
                      next="office config economics.collect true  # opt in, then office economics ingest",
                      data={"enabled": False})
    lines, counts = [], {"events": 0, "sessions": 0, "missing_transcripts": 0}
    if file:
        path = Path(file)
        if not path.is_file():
            raise Usage("missing-file", f"no usage file at {file}")
        events = PARSERS[harness](path, session) if harness in PARSERS else parse_normalized(path)
        with db.transaction(con):
            counts["events"] += record(con, events, run_id=run["id"], role=role, attribution="office")
        lines.append(f"ingested {len(events)} usage event(s) from {path}")
        return Result(lines=lines, next="office inspect economics", data={"enabled": True, **counts})
    sources = []
    rows = [dict(d) for d in con.execute(
        "SELECT id, task_id, role, kind, harness, session_id, started_at, ended_at FROM dispatches "
        "WHERE run_id=? AND session_id IS NOT NULL AND harness IS NOT NULL ORDER BY started_at, id", (run["id"],))]
    for i, d in enumerate(rows):
        # A resumed dispatch can share its predecessor's session: each dispatch owns only the turns inside
        # its own window, which ends when it ended or when the next dispatch on that session started.
        nxt = next((n["started_at"] for n in rows[i + 1:]
                    if (n["harness"], n["session_id"]) == (d["harness"], d["session_id"]) and n["started_at"]), None)
        ends = [e for e in (d["ended_at"], nxt) if e]
        end = min(ends, key=parse_iso) if ends else None
        attribution = _window(d["started_at"], end) if d["started_at"] else "office"
        sources.append((d["harness"], d["session_id"], {"run_id": run["id"], "dispatch_id": d["id"],
                        "task_id": d["task_id"], "role": d["role"], "phase": d["kind"], "attribution": attribution}))
    for b in con.execute("SELECT harness, session_id, bound_at, ended_at FROM session_bindings WHERE run_id=?",
                         (run["id"],)):
        end = b["ended_at"] or run.get("terminal_at")
        sources.append((b["harness"], b["session_id"], {"run_id": run["id"], "role": "root",
                        "attribution": _window(b["bound_at"], end)}))
    for h, sid, assoc in sources:
        files = _transcripts(h, sid) if h in PARSERS else []
        if not files:
            counts["missing_transcripts"] += 1
            continue
        counts["sessions"] += 1
        for path in files:
            events = PARSERS[h](path, sid)
            with db.transaction(con):
                counts["events"] += record(con, events, **assoc)
    lines.append(f"ingested {counts['events']} usage event(s) from {counts['sessions']} session transcript(s)"
                 + (f"; {counts['missing_transcripts']} session(s) had no readable transcript"
                    if counts["missing_transcripts"] else ""))
    return Result(lines=lines, next="office inspect economics", data={"enabled": True, **counts})


# ------------------------------------------------------------------ inspect

def _sum(rows, field) -> int | None:
    vals = [r[field] for r in rows if r[field] is not None]
    return sum(vals) if vals else None


def _fmt(rows, field) -> str:
    """Total over the turns that reported the field. `unknown` when none did, never 0; a partial total says how many did not report."""
    total = _sum(rows, field)
    if total is None:
        return "unknown"
    missing = sum(1 for r in rows if r[field] is None)
    return f"{total:,}" + (f" (+{missing} unknown)" if missing else "")


def _role(row: dict) -> str:
    return ROLE_GROUP.get(row["role"], row["role"]) or "unknown"


def _by_role(rows: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(_role(r), []).append(r)
    return groups


def _cost_kinds(rows: list[dict]) -> dict[str, list]:
    kinds: dict[str, list] = {}
    for r in rows:
        kinds.setdefault(r["cost_kind"], []).append(r["cost_usd"])
    return kinds


def _known(values: list) -> list:
    return [v for v in values if v is not None]


def _cost_data(rows: list[dict]) -> dict:
    out = {}
    for kind, vs in _cost_kinds(rows).items():
        known = _known(vs)
        out[kind] = {"count": len(vs), "usd": sum(known) if known else None}
        if known and len(known) < len(vs):  # a priced kind with some unpriced turns: the usd total is partial
            out[kind]["unknown_count"] = len(vs) - len(known)
    return out


def _missing(rows: list[dict]) -> dict:
    return {f: sum(1 for r in rows if r[f] is None) for f in TOKEN_FIELDS}


def _task_of(row: dict) -> str | None:
    """A row belongs to a task only through its dispatch. A usage file can name a task; it cannot pick one."""
    return row["task_id"] if row["dispatch_id"] else None


def _task_ids(rows: list[dict], task_ids: list[str]) -> list[str]:
    """The listed tasks, then any task a dispatch-linked row names: a cancelled task keeps its spend visible."""
    return [*task_ids, *sorted({_task_of(r) for r in rows if _task_of(r)} - set(task_ids))]


def _cost(rows: list[dict]) -> str:
    parts = []
    for kind, vs in sorted(_cost_kinds(rows).items()):
        known = _known(vs)
        if not known:
            parts.append(f"unknown x{len(vs)}" if kind == "unknown" else f"{kind} unknown x{len(vs)}")
        else:
            parts.append(f"{kind} ${sum(known):.4f}" + (f" (+{len(vs) - len(known)} unknown)" if len(known) < len(vs) else ""))
    return ", ".join(parts)


def _table_cells(rows: list[dict]) -> str:
    """turns | input | output | cache read | cache write | cost, matching the header `inspect economics` prints."""
    return f"{len(rows)} | " + " | ".join(_fmt(rows, f) for f in TOKEN_FIELDS) + f" | {_cost(rows)}"


def _labelled(rows: list[dict]) -> str:
    if not rows:
        return "unknown (no usage recorded)"
    return (f"{len(rows)} turn(s), input {_fmt(rows, 'input_tokens')}, output {_fmt(rows, 'output_tokens')}, "
            f"cache read {_fmt(rows, 'cache_read_tokens')}, cache write {_fmt(rows, 'cache_write_tokens')}, "
            f"cost {_cost(rows)}")


def _listed_tasks(con, run: dict) -> list[str]:
    from office import state
    return [t["id"] for t in state.tasks(con, run["id"]) if t["status"] != "cancelled"]


def _office_rows(con, run_id: str) -> list[dict]:
    if not has_table(con):
        return []
    return [dict(r) for r in con.execute(
        "SELECT * FROM usage_events WHERE run_id=? AND attribution='office'", (run_id,))]


def status_lines(con, run: dict, task_ids: list[str]) -> tuple[list[str], list[str]]:
    """`office status` usage as (default, verbose). Read-only. Nothing shows by default until collection is on or a
    row exists; the off notice is verbose only."""
    on = enabled(run)
    rows = _office_rows(con, run["id"])
    if not on and not rows:
        return [], ["usage: unknown (cost collection is off; office config economics.collect true)"]
    lines = [f"usage: {_labelled(rows)}"]
    lines += [f"usage run/{role}: {_labelled(rs)}" for role, rs in sorted(_by_role(rows).items())]
    for tid in _task_ids(rows, task_ids):
        mine = [r for r in rows if _task_of(r) == tid]
        if not mine:
            lines.append(f"usage {tid}: unknown (no usage attributed to this task)")
        lines += [f"usage {tid}/{role}: {_labelled(rs)}" for role, rs in sorted(_by_role(mine).items())]
    if not on:
        lines.append("usage: collection is off; figures are from when it was on")
    return lines, []


def inspect(con, run: dict) -> Result:
    """Measured usage only. Disabled or empty says so; nothing is estimated."""
    on = enabled(run)
    task_ids = _listed_tasks(con, run)
    if not has_table(con):
        msg = ("economics collection is disabled (economics.collect: false); no usage data" if not on
               else "no usage recorded for this run yet")
        unknown = [f"task {tid}: unknown (no usage attributed to this task)" for tid in task_ids] if on else []
        return Result(lines=[msg, *unknown], next=("office economics ingest" if on else
                                                   "office config economics.collect true  # opt in"),
                      data={"enabled": on, "rows": 0})
    rows = [dict(r) for r in con.execute("SELECT * FROM usage_events WHERE run_id=?", (run["id"],))]
    office_rows = [r for r in rows if r["attribution"] == "office"]
    lines = [f"economics: collection {'enabled' if on else 'disabled (showing data recorded while enabled)'}"]
    if not office_rows:
        lines.append("no Office-attributed usage recorded for this run")
        lines += [f"task {tid}: unknown (no usage attributed to this task)" for tid in task_ids] if on else []
        return Result(lines=lines, next="office economics ingest" if on else None,
                      data={"enabled": on, "rows": 0, "outside_rows": len(rows)})
    by_group = []
    lines.append("role/phase: turns | input | output | cache read | cache write | cost")
    groups: dict[tuple, list] = {}
    for r in office_rows:
        groups.setdefault((_role(r), r["phase"] or "unknown"), []).append(r)
    for (role, phase), rs in sorted(groups.items()):
        lines.append(f"  {role}/{phase}: " + _table_cells(rs))
        by_group.append({"role": role, "phase": phase, "turns": len(rs), **{f: _sum(rs, f) for f in TOKEN_FIELDS},
                         "missing": _missing(rs), "cost": _cost_data(rs)})
    by_task, unmeasured = [], []
    lines.append("task/role: turns | input | output | cache read | cache write | cost")
    for tid in _task_ids(office_rows, task_ids):
        mine = [r for r in office_rows if _task_of(r) == tid]
        if not mine:
            unmeasured.append(tid)
            lines.append(f"  task {tid}: unknown (no usage attributed to this task)")
        for role, rs in sorted(_by_role(mine).items()):
            lines.append(f"  task {tid}/{role}: " + _table_cells(rs))
            by_task.append({"task": tid, "role": role, "turns": len(rs), **{f: _sum(rs, f) for f in TOKEN_FIELDS},
                            "missing": _missing(rs), "cost": _cost_data(rs)})
    missing = {f: sum(1 for r in office_rows if r[f] is None) for f in TOKEN_FIELDS}
    read, write, fresh = (_sum(office_rows, "cache_read_tokens"), _sum(office_rows, "cache_write_tokens"),
                          _sum(office_rows, "input_tokens"))
    if read is not None and fresh is not None and read + fresh + (write or 0) > 0:
        lines.append(f"cache: {read / (read + fresh + (write or 0)):.0%} of prompt tokens read from cache"
                     + ("" if write is not None else " (cache writes not reported)"))
    else:
        lines.append("cache: unknown (cache reads or input not reported)")
    cold = _cold_resumes(con, run, office_rows)
    lines.append(f"cold resumes (first turn after a resume wrote cache, read none): {len(cold)}"
                 + (f" ({', '.join(cold[:5])})" if cold else ""))
    total_d = con.execute("SELECT COUNT(*) FROM dispatches WHERE run_id=?", (run["id"],)).fetchone()[0]
    measured_d = len({r["dispatch_id"] for r in office_rows if r["dispatch_id"]})
    roots = sum(1 for r in office_rows if r["role"] == "root")
    lines.append(f"coverage: {measured_d}/{total_d} dispatch(es) measured; root turns {roots}; "
                 "missing fields " + ", ".join(f"{f.replace('_tokens', '')} {n}" for f, n in missing.items()))
    outside = len(rows) - len(office_rows)
    if outside:
        lines.append(f"{outside} turn(s) outside their run or dispatch window are excluded")
    return Result(lines=lines, data={"enabled": on, "rows": len(office_rows), "outside_rows": outside,
                                     "groups": by_group, "tasks": by_task, "unmeasured_tasks": unmeasured,
                                     "missing": missing, "cold_resumes": cold,
                                     "coverage": {"dispatches": total_d, "measured_dispatches": measured_d}})


def _cold_resumes(con, run: dict, rows: list[dict]) -> list[str]:
    out = []
    for d in con.execute("SELECT id FROM dispatches WHERE run_id=? AND resumed_from IS NOT NULL", (run["id"],)):
        mine = sorted((r for r in rows if r["dispatch_id"] == d["id"] and r["occurred_at"]),
                      key=lambda r: r["occurred_at"])
        if mine and (mine[0]["cache_write_tokens"] or 0) > 0 and mine[0]["cache_read_tokens"] == 0:
            out.append(d["id"])
    return out
