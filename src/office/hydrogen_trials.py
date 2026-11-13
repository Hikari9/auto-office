"""Owner-approved Hydrogen executor canary: a separate fail-closed dispatch gate.

This is deliberately independent of ordinary model discovery and adaptive exploration.
An opportunity is an actual, classified, low-risk S/M executor launch. Entries are
append-only, and booking happens under runs.db's BEGIN IMMEDIATE transaction. A
reserved attempt is spent even if its launch subsequently fails or is abandoned.
"""
from __future__ import annotations

import uuid
from office.util import now_iso

MODEL = "gemini-4-hydrogen"
WINDOW = 20
CAP = 3
MAX_PER_RUN = 1
HIGH_RISK = frozenset((
    "security", "credential", "credentials", "secret", "secrets", "payment", "payments",
    "finance", "financial", "billing", "infrastructure", "production", "production-data",
    "database", "iam", "permissions", "authentication", "authorization", "deploy",
    "irreversible", "privileged", "compliance", "personal-data", "customer-data",
))


def is_hydrogen(c: dict | None) -> bool:
    c = c or {}
    return MODEL in (c.get("model_id"), c.get("alias_resolved_to")) or str(c.get("invocation_model_id") or "").startswith(MODEL + "-")


def settings(config: dict | None) -> dict:
    raw = ((config or {}).get("routing") or {}).get("hydrogen_trials") or {}
    return {"enabled": raw.get("enabled") is True, "max_per_run": MAX_PER_RUN,
            "max_rolling_20": CAP, "window": WINDOW}


def validate(config: dict) -> list[str]:
    raw = ((config.get("routing") or {}).get("hydrogen_trials") or {})
    if not isinstance(raw, dict):
        return ["routing.hydrogen_trials must be a mapping"]
    if set(raw) - {"enabled"}:
        return ["routing.hydrogen_trials only accepts enabled; safety limits are immutable"]
    if "enabled" in raw and type(raw["enabled"]) is not bool:
        return ["routing.hydrogen_trials.enabled must be a boolean"]
    return []


def exact_effort(size: str) -> str | None:
    return {"XS": "low", "S": "medium", "M": "high", "L": "high"}.get(size)


def route_refusal(candidate: dict, *, role: str, size: str | None, manual: bool,
                  config: dict | None = None, risk: dict | None = None,
                  descriptor: dict | None = None) -> str | None:
    """Hard veto even when a user allow, benchmark bypass or adaptive exploration exists."""
    if not is_hydrogen(candidate):
        return None
    if role != "executor":
        return "Hydrogen is executor-only (never a reviewer, worker or planner)"
    if size not in ("XS", "S", "M", "L"):
        return "Hydrogen is excluded for XL and unclassified tasks"
    if candidate.get("harness") != "agy" or candidate.get("effort") != exact_effort(size):
        return "Hydrogen requires the exact approved agy model/size/effort"
    if manual:
        return None  # exact conformance checked separately; manual never opens the automatic canary
    if size not in ("S", "M"):
        return "Hydrogen XS/L requires explicit manual selection after conformance"
    if not settings(config)["enabled"]:
        return "automatic Hydrogen canary is disabled pending exact qualification"
    if not safe_task(size, risk, descriptor):
        return "automatic Hydrogen requires a bounded, reversible, low-risk S/M task"
    return None


def safe_task(size: str | None, risk: dict | None, descriptor: dict | None) -> bool:
    risk, descriptor = risk or {}, descriptor or {}
    if size not in ("S", "M") or descriptor.get("task_size") != size:
        return False
    if risk.get("irreversible") or risk.get("blast_radius") not in ("local", "repo"):
        return False
    if risk.get("size_class") not in ("S", "M") or descriptor.get("bounds") != "bounded":
        return False
    # A positive bounded, reversible classification is required; unknown/high-risk
    # domains are not silently treated as safe.
    domain = str(descriptor.get("domain") or "").strip().lower()
    if not domain or any(word in domain for word in HIGH_RISK):
        return False
    for item in (risk.get("domain"), risk.get("category"), descriptor.get("risk"), descriptor.get("risk_category")):
        if item and any(word in str(item).lower() for word in HIGH_RISK):
            return False
    return True


def manual_conformance(con, candidate: dict, *, role: str, size: str | None) -> str | None:
    """Manual XS/L (and manual S/M) needs a fresh *exact* model+effort probe receipt."""
    denial = route_refusal(candidate, role=role, size=size, manual=True)
    if denial:
        return denial
    if con is None:
        return "Hydrogen conformance requires durable runs.db evidence"
    from office import route_probe
    receipt = route_probe.status(con, candidate)
    if not receipt or receipt.get("result") != "pass":
        return "Hydrogen manual route needs a fresh exact harness/model/effort conformance receipt"
    return None


def run_safe(config: dict, run: dict, task: dict) -> bool:
    if not settings(config)["enabled"]:
        return False
    gates = run.get("gates") or {}
    # No experiment when independent code review is skipped/unknown.
    if gates.get("code_review") is not True:
        return False
    desc = task.get("descriptor") or {}
    return safe_task(desc.get("task_size"), run.get("risk"), desc)


def snapshot(con) -> dict:
    """Last nineteen eligible opportunities, plus this pending opportunity."""
    rows = con.execute("SELECT experimental FROM hydrogen_trial_opportunities "
                       "ORDER BY seq DESC LIMIT ?", (WINDOW - 1,)).fetchall()
    count = sum(int(r[0]) for r in rows)
    return {"previous": len(rows), "trial_used": count, "trial_remaining": max(0, CAP - count),
            "window": WINDOW, "limit": CAP, "cold_start": len(rows) < WINDOW - 1}


def book(con, run_id: str, task_id: str, selected_route: str, *, experiment: bool,
         fallback_route: str | None = None) -> dict:
    """Caller MUST already hold the same SQLite writer tx that will create the dispatch.

    Records all eligible opportunities, including non-trials, so a failed trial
    cannot vanish from the 20-entry denominator. No updates/deletes of prior rows.
    """
    if not con.in_transaction:
        raise RuntimeError("Hydrogen reservation requires a BEGIN IMMEDIATE dispatch transaction")
    before = snapshot(con)
    if experiment:
        used_run = con.execute("SELECT COUNT(*) FROM hydrogen_trial_opportunities WHERE run_id=? "
                               "AND experimental=1", (run_id,)).fetchone()[0]
        if used_run >= MAX_PER_RUN:
            experiment = False
            blocked = "one-trial-per-run"
        elif before["cold_start"]:
            experiment = False
            blocked = "cold-start"
        elif before["trial_used"] >= CAP:
            experiment = False
            blocked = "rolling-cap"
        else:
            blocked = None
    else:
        blocked = None
    if blocked and fallback_route:
        selected_route = fallback_route
    event_id = "HO" + uuid.uuid4().hex
    con.execute("INSERT INTO hydrogen_trial_opportunities(id, run_id, task_id, selected_route, experimental, "
                "created_at) VALUES (?,?,?,?,?,?)", (event_id, run_id, task_id, selected_route,
                                                      int(experiment), now_iso()))
    return {"id": event_id, "experimental": experiment, "blocked": blocked, "route": selected_route, "prior": before,
            "after": snapshot(con)}


def trial_refusal(con, run: dict, task: dict, candidate: dict, fallback: dict, config: dict) -> str | None:
    """Additional Hydrogen gate, after generic probe+quota checks, inside the dispatch transaction."""
    desc = task.get("descriptor") or {}
    size = desc.get("task_size")
    denial = route_refusal(candidate, role="executor", size=size, manual=False,
                           config=config, risk=run.get("risk"), descriptor=desc)
    if denial:
        return denial
    if not run_safe(config, run, task):
        return "risk-or-independent-review"
    from office import executor_slates, route_probe
    if not fallback or fallback.get("status") != "selected" or not fallback.get("candidate"):
        return "no-fallback"
    known = fallback["candidate"]
    if is_hydrogen(known) or known.get("route_status", "available") != "available":
        return "fallback-must-be-known-working-non-hydrogen"
    if executor_slates.benchmark_gate(known, size):
        return "fallback-not-qualified-for-same-size"
    exact = route_probe.status(con, candidate)
    if not exact or exact.get("result") != "pass":
        return "conformance-missing"
    return None
