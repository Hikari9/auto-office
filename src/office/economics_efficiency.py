"""Read-only #502 efficiency view; missing telemetry is never billed at zero."""
from __future__ import annotations

import json

TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


def summarize(rows: list[dict], *, total_dispatches: int | None = None) -> dict:
    """Describe the observed Office-attributed sample, not an entire unknown run."""
    sample = [r for r in rows if r.get("attribution") == "office"]
    unknowns = {k: sum(r.get(k) is None for r in sample) for k in TOKEN_FIELDS}
    components = {k: sum(r[k] for r in sample if isinstance(r.get(k), int) and r[k] >= 0)
                  for k in TOKEN_FIELDS}
    root = [r for r in sample if r.get("role") == "root"]
    root_parts = sum(r[k] for r in root for k in TOKEN_FIELDS if isinstance(r.get(k), int) and r[k] >= 0)
    total_parts = sum(components.values())
    measured_d = len({r.get("dispatch_id") for r in sample if r.get("dispatch_id")})
    complete_fields = bool(sample) and not any(unknowns.values())
    dispatch_coverage = (total_dispatches is not None and measured_d == total_dispatches)
    # Even a completely populated sampled ledger cannot prove root coverage;
    # a root's zero turns can mean missing transcripts, not zero spending.
    root_measured_share = root_parts / total_parts if total_parts else None
    cache_coverage = {field: f"{len(sample) - unknowns[field]}/{len(sample)}"
                      for field in ("cache_read_tokens", "cache_write_tokens")}
    actual = [r for r in sample if r.get("cost_kind") == "actual" and isinstance(r.get("cost_usd"), (int, float))]
    actual_complete = bool(sample) and len(actual) == len(sample)
    total_actual = sum(r["cost_usd"] for r in actual)
    root_actual = sum(r["cost_usd"] for r in actual if r.get("role") == "root")
    kinds = {str(r.get("cost_kind") or "unknown") for r in sample}
    return {
        "rows": len(sample), "outside_rows": len(rows) - len(sample),
        "root_rows": len(root), "token_components": components, "missing_token_fields": unknowns,
        "root_share_of_observed_token_components": root_measured_share,
        "token_coverage": "complete-recorded-fields" if complete_fields else "partial-or-unmeasured",
        "dispatches_measured": measured_d, "dispatches_total": total_dispatches,
        "dispatch_coverage": "complete" if dispatch_coverage else "partial-or-unknown",
        "actual_usd_observed": total_actual if actual else None,
        "actual_usd_coverage": f"{len(actual)}/{len(sample)}" if sample else "0/0",
        "root_share_of_actual_usd": (root_actual / total_actual if actual_complete and total_actual > 0 else None),
        "cost_kinds": sorted(kinds), "cache_coverage": cache_coverage,
    }


def inspect(con, run: dict):
    """On-demand SQL read only; the default status and all hooks stay untouched."""
    from office import economics
    from office.result import Result

    enabled = economics.enabled(run)
    lines = [f"efficiency: cost collection {'enabled' if enabled else 'disabled'}; shadow advice only"]
    if economics.has_table(con):
        rows = [dict(r) for r in con.execute("SELECT * FROM usage_events WHERE run_id=?", (run["id"],))]
    else:
        rows = []
    dispatches = con.execute("SELECT COUNT(*) FROM dispatches WHERE run_id=?", (run["id"],)).fetchone()[0]
    s = summarize(rows, total_dispatches=dispatches)
    if not s["rows"]:
        lines.append("usage: no measured Office-attributed turns; root share, cache efficiency and billed cost unknown")
    else:
        share = s["root_share_of_observed_token_components"]
        lines.append("root share of observed token components: " +
                     (f"{share:.1%} (sample only; not whole-run cost)" if share is not None else "unknown"))
        lines.append(f"coverage: {s['dispatches_measured']}/{dispatches} dispatches; "
                     f"token fields {s['token_coverage']}; root turns {s['root_rows']}")
        cost = s["actual_usd_observed"]
        lines.append("reported actual USD: " + (f"${cost:.4f} observed only" if cost is not None else "unknown") +
                     f"; cost coverage {s['actual_usd_coverage']}; "
                     "nominal API estimates and subscription quota are not billed dollars")
        lines.append("cache read/write field coverage: " + ", ".join(
            f"{field} {cov}" for field, cov in s["cache_coverage"].items()) +
                     "; observed cache reads/writes: " +
                     f"{s['token_components']['cache_read_tokens']}/{s['token_components']['cache_write_tokens']} " +
                     "(missing fields excluded)")
        lines.append("review/rework/admin time: unknown unless independently observed; attempts are not evidence of rework cost")
    cold = economics._cold_resumes(con, run, [r for r in rows if r.get("attribution") == "office"]) if rows else []
    lines.append(f"verified cold resumes in observed records: {len(cold)}; unobserved resume states unknown")
    events = [dict(r) for r in con.execute("SELECT seq,payload_json FROM events WHERE run_id=? "
                                          "AND kind='delegation.shadow' ORDER BY seq DESC LIMIT 5", (run["id"],))]
    advice = []
    for r in events:
        try:
            payload = json.loads(r["payload_json"] or "{}")
        except (TypeError, ValueError):
            continue
        advice.append({"seq": r["seq"], **payload})
    if advice:
        latest = advice[0]
        lines.append(f"delegation shadow (event {latest['seq']}): proposed {latest.get('proposal', 'unknown')}; "
                     f"actual {latest.get('actual', 'unknown')}; no scheduling effect")
    else:
        lines.append("delegation shadow: no fan-out observation recorded")
    lines.append("matched baseline/treatment comparison: not measured here; use eval/v31/compare_economics.py")
    return Result(lines=lines, data={"enabled": enabled, "summary": s, "cold_resumes": cold,
                                     "delegation_shadow": advice})
