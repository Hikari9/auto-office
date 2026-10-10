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
            "cost_usd": float(cost) if isinstance(cost, (int, float)) and kind != "unknown" else None,
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
    ", dispatch_id=CASE WHEN attribution='office' THEN dispatch_id ELSE excluded.dispatch_id END"
    ", task_id=CASE WHEN attribution='office' THEN task_id ELSE excluded.task_id END"
    ", role=CASE WHEN attribution='office' THEN role ELSE excluded.role END"
    ", phase=CASE WHEN attribution='office' THEN phase ELSE excluded.phase END"
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


def _fmt(v) -> str:
    return "unknown" if v is None else f"{v:,}"


def inspect(con, run: dict) -> Result:
    """Measured usage only. Disabled or empty says so; nothing is estimated."""
    on = enabled(run)
    if not has_table(con):
        msg = ("economics collection is disabled (economics.collect: false); no usage data" if not on
               else "no usage recorded for this run yet")
        return Result(lines=[msg], next=("office economics ingest" if on else
                                         "office config economics.collect true  # opt in"),
                      data={"enabled": on, "rows": 0})
    rows = [dict(r) for r in con.execute("SELECT * FROM usage_events WHERE run_id=?", (run["id"],))]
    office_rows = [r for r in rows if r["attribution"] == "office"]
    lines = [f"economics: collection {'enabled' if on else 'disabled (showing data recorded while enabled)'}"]
    if not office_rows:
        lines.append("no Office-attributed usage recorded for this run")
        return Result(lines=lines, next="office economics ingest" if on else None,
                      data={"enabled": on, "rows": 0, "outside_rows": len(rows)})
    groups: dict[tuple, list] = {}
    for r in office_rows:
        groups.setdefault((r["role"] or "unknown", r["phase"] or "unknown"), []).append(r)
    by_group = []
    lines.append("role/phase: turns | input | output | cache read | cache write | cost")
    for (role, phase), rs in sorted(groups.items()):
        totals = {f: _sum(rs, f) for f in TOKEN_FIELDS}
        costs = {}
        for r in rs:
            costs.setdefault(r["cost_kind"], []).append(r["cost_usd"])
        cost_txt = ", ".join(f"{k} ${sum(v for v in vs if v is not None):.4f}" if k != "unknown"
                             else f"unknown x{len(vs)}" for k, vs in sorted(costs.items()))
        lines.append(f"  {role}/{phase}: {len(rs)} | " + " | ".join(_fmt(totals[f]) for f in TOKEN_FIELDS)
                     + f" | {cost_txt}")
        by_group.append({"role": role, "phase": phase, "turns": len(rs), **totals,
                         "cost": {k: {"count": len(vs), "usd": (None if k == "unknown" else
                                                                sum(v for v in vs if v is not None))}
                                  for k, vs in costs.items()}})
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
                                     "groups": by_group, "missing": missing, "cold_resumes": cold,
                                     "coverage": {"dispatches": total_d, "measured_dispatches": measured_d}})


def _cold_resumes(con, run: dict, rows: list[dict]) -> list[str]:
    out = []
    for d in con.execute("SELECT id FROM dispatches WHERE run_id=? AND resumed_from IS NOT NULL", (run["id"],)):
        mine = sorted((r for r in rows if r["dispatch_id"] == d["id"] and r["occurred_at"]),
                      key=lambda r: r["occurred_at"])
        if mine and (mine[0]["cache_write_tokens"] or 0) > 0 and mine[0]["cache_read_tokens"] == 0:
            out.append(d["id"])
    return out
