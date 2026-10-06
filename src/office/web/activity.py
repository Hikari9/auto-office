"""A bounded, newest-first, redacted window over a run's events."""
from __future__ import annotations

import json
import re
from pathlib import Path

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
SUMMARY_CAP = 500
STRING_CAP = 1000
PAYLOAD_CAP = 4000
MASK = "***"

_SECRET_KEY = re.compile(r"(?i)(token|secret|password|passwd|pwd|api[_-]?key|private[_-]?key|authorization|cookie|"
                         r"credential|access[_-]?key)")
_PATTERNS = [
    # Authorization headers and bearer/basic credentials.
    (re.compile(r"(?i)\b(authorization)(\s*[:=]\s*)(?:(bearer|basic|token)\s+)?[^\s,;\"']+"),
     lambda m: f"{m.group(1)}{m.group(2)}{(m.group(3) + ' ') if m.group(3) else ''}{MASK}"),
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}"), lambda m: f"{m.group(1)} {MASK}"),
    # Env-style assignments whose name says secret: GH_TOKEN=..., export API_KEY="...".
    (re.compile(r"\b([A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|KEY|CREDENTIALS?)[A-Za-z0-9_]*)"
                r"(\s*=\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)", re.IGNORECASE),
     lambda m: f"{m.group(1)}{m.group(2)}{MASK}"),
    # Well-known token shapes.
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}|sk-(?:ant-)?[A-Za-z0-9_-]{16,}|"
                r"xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,})"), lambda m: MASK),
    # Credentials embedded in URLs: https://user:pass@host.
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@"), lambda m: f"{m.group(1)}{MASK}@"),
]


def redact_text(text: str, *, home: str | Path | None = None, cap: int = STRING_CAP) -> str:
    out = str(text)
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    out = shorten_home(out, home)
    if len(out) > cap:
        out = out[:cap] + f"…[{len(out) - cap} more]"
    return out


def shorten_home(text: str, home: str | Path | None = None) -> str:
    base = str(home if home is not None else Path.home()).rstrip("/")
    if not base:
        return text
    return re.sub(re.escape(base) + r"(?![\w.-])", "~", text)


def redact_value(value, *, home=None):
    """Recursively redact a decoded payload: secret-named keys are masked whole."""
    if isinstance(value, dict):
        return {k: (MASK if _SECRET_KEY.search(str(k)) and isinstance(v, (str, dict, list)) and v else redact_value(v, home=home))
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v, home=home) for v in value]
    if isinstance(value, str):
        return redact_text(value, home=home)
    return value


def redact_payload(raw: str | None, *, home=None):
    if raw is None:
        return None
    try:
        value = redact_value(json.loads(raw), home=home)
    except (TypeError, ValueError):
        value = redact_text(raw, home=home)
    encoded = json.dumps(value, sort_keys=True)
    if len(encoded) > PAYLOAD_CAP:
        return {"truncated": True, "preview": encoded[:PAYLOAD_CAP], "bytes": len(encoded)}
    return value


def window(snap, run_id: str, *, limit: int | None = None, before_seq: int | None = None, home=None) -> dict:
    """Newest-first events for `run_id`, at most `limit` (default 50, max 200), older than `before_seq`."""
    n = DEFAULT_LIMIT if limit is None else max(1, min(int(limit), MAX_LIMIT))
    if not snap.has("events"):
        return {"available": False, "reason": "this runs.db has no events table", "items": [], "next_before_seq": None}
    where, params = "run_id=?", [run_id]
    if before_seq is not None:
        where += " AND seq<?"
        params.append(int(before_seq))
    rows = snap.rows(f"SELECT * FROM events WHERE {where} ORDER BY seq DESC LIMIT ?", (*params, n + 1))
    more = len(rows) > n
    rows = rows[:n]
    items = [{"seq": r["seq"], "kind": r.get("kind"), "audience": r.get("audience"), "task_id": r.get("task_id"),
              "dispatch_id": r.get("dispatch_id"), "created_at": r.get("created_at"),
              "office_version": r.get("office_version"),
              "summary": redact_text(r.get("summary") or "", home=home, cap=SUMMARY_CAP),
              "payload": redact_payload(r.get("payload_json"), home=home)} for r in rows]
    return {"available": True, "items": items, "limit": n,
            "next_before_seq": items[-1]["seq"] if more and items else None}
