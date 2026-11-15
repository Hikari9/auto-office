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
REVIEW_ROLES = frozenset(("code_reviewer", "plan_reviewer"))  # H3 (#1168) code; H5 plan
# Not visual_reviewer: agy's vision profile runs with --dangerously-skip-permissions and no worktree guard (H5).
REVIEW_EFFORT = "high"  # the effort that ran the 5-case planted-bug set (research/hydrogen-shadow)
CANARY_MAX = 10  # #1168: owner-approved S canary budget ceiling; a config value above it is refused
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
    budget = raw.get("canary_tasks")
    budget = budget if type(budget) is int and 0 <= budget <= CANARY_MAX else 0
    return {"enabled": raw.get("enabled") is True, "max_per_run": MAX_PER_RUN,
            "max_rolling_20": CAP, "window": WINDOW, "canary_tasks": budget}


def validate(config: dict) -> list[str]:
    raw = ((config.get("routing") or {}).get("hydrogen_trials") or {})
    if not isinstance(raw, dict):
        return ["routing.hydrogen_trials must be a mapping"]
    if set(raw) - {"enabled", "canary_tasks"}:
        return ["routing.hydrogen_trials only accepts enabled and canary_tasks; safety limits are immutable"]
    if "enabled" in raw and type(raw["enabled"]) is not bool:
        return ["routing.hydrogen_trials.enabled must be a boolean"]
    if "canary_tasks" in raw and (type(raw["canary_tasks"]) is not int
                                  or not 0 <= raw["canary_tasks"] <= CANARY_MAX):
        return [f"routing.hydrogen_trials.canary_tasks must be an integer from 0 to {CANARY_MAX}"]
    return []


def exact_effort(size: str) -> str | None:
    return {"XS": "low", "S": "medium", "M": "high", "L": "high"}.get(size)


def user_allowed(rule: dict | None) -> bool:
    """Whether an effective role rule is the user's own exact-route allow (#1163).

    `rule` is route_policy.effective_role_state for this candidate, role and size, so size scope
    is already applied. A wildcard route, a shipped/seed/override source or a non-allow never counts.
    """
    from office import route_policy
    rule = rule or {}
    return (rule.get("state") == "allow" and rule.get("source") == "user"
            and route_policy._is_exact_spec(rule.get("rule")))


def route_refusal(candidate: dict, *, role: str, size: str | None, manual: bool,
                  config: dict | None = None, risk: dict | None = None,
                  descriptor: dict | None = None, user_allowed: bool = False) -> str | None:
    """Hard veto even when a user allow, benchmark bypass or adaptive exploration exists.

    `user_allowed` (see `user_allowed()`) skips only the automatic-canary checks, like `manual`."""
    if not is_hydrogen(candidate):
        return None
    if role in REVIEW_ROLES:
        # H3 (#1168): owner-promoted voting code reviewer; H5 (Rico 2026-11-15): also a plan reviewer.
        # Only the exact @high route that ran the planted-bug set, only under the user's exact allow (a manual --review-as does not stand in
        # for it, H4 #1178), always behind a fresh exact probe (manual_conformance), never through
        # discovery or adaptive exploration.
        if candidate.get("harness") != "agy" or candidate.get("effort") != REVIEW_EFFORT:
            return f"Hydrogen reviews only as the exact agy/{MODEL}@{REVIEW_EFFORT} route"
        if not user_allowed:
            return f"Hydrogen review needs the user's exact allow of agy/{MODEL}@{REVIEW_EFFORT} for {role}"
        return None
    if role != "executor":
        return "Hydrogen is an executor or a code or plan reviewer only (never a worker, planner or other reviewer; not a visual reviewer until agy has a read-only vision launch)"
    if size not in ("XS", "S", "M", "L"):
        return "Hydrogen is excluded for XL and unclassified tasks"
    if candidate.get("harness") != "agy" or candidate.get("effort") != exact_effort(size):
        return "Hydrogen requires the exact approved agy model/size/effort"
    if manual or user_allowed:
        return None  # exact conformance checked separately; neither opens the automatic canary
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


def manual_conformance(con, candidate: dict, *, role: str, size: str | None,
                       user_allowed: bool = False) -> str | None:
    """Manual XS/L (and manual S/M) needs a fresh *exact* model+effort probe receipt.
    A code or plan review also needs the user's exact allow (`user_allowed`); manual selection alone is not one."""
    denial = route_refusal(candidate, role=role, size=size, manual=True, user_allowed=user_allowed)
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


def canary_spent(con) -> int:
    """Booked S canary dispatches (#1168). Append-only rows, so the count never goes down."""
    if con is None:
        return 0
    return int(con.execute("SELECT COUNT(*) FROM hydrogen_trial_opportunities WHERE canary=1").fetchone()[0])


def live_budget(run: dict | None = None) -> int:
    """The S canary budget from the live user/repo config, not the run's pinned snapshot (#1168).

    The budget is one global cap counted in runs.db, so a run started before it was set
    still sees it. It only reorders routes the run's pinned policy already admits
    (pinned enabled flag plus the exact user allow); it never widens access.
    An unreadable or invalid live config reads as no budget."""
    from pathlib import Path
    from office import config as cfg
    root = (run or {}).get("repo_root")
    try:
        live, _ = cfg.resolve(Path(root) if root else None)
    except Exception:  # noqa: BLE001 - fail closed: no canary preference
        return 0
    if validate(live):
        return 0
    return settings(live)["canary_tasks"]


def canary_progress(con, budget: int) -> dict:
    spent = canary_spent(con) if budget else 0
    return {"budget": budget, "spent": min(spent, budget) if budget else spent,
            "remaining": max(0, budget - spent), "complete": bool(budget) and spent >= budget}


def _run_canaries(con, run_id: str | None) -> int:
    return con.execute("SELECT COUNT(*) FROM hydrogen_trial_opportunities WHERE run_id=? AND canary=1",
                       (run_id,)).fetchone()[0]


def canary_active(con, config: dict | None, run: dict, task: dict | None, budget: int) -> bool:
    """Whether this S task should prefer the user-allowed Hydrogen route (#1168).

    `config` is the run's pinned config: its `enabled` flag must be true. `budget` is
    `live_budget(run)`. The canary only reorders an already admitted user allow; every
    hard veto and exact-probe check still runs. At the budget it reverts to ordinary
    scoring. One canary per run keeps the evidence spread over independent runs."""
    if con is None or not settings(config)["enabled"] or not budget:
        return False
    task = task or {}
    desc = task.get("descriptor") or {}
    if desc.get("task_size") != "S" or not run_safe(config or {}, run, task):
        return False
    return canary_spent(con) < budget and _run_canaries(con, run.get("id")) < MAX_PER_RUN


def book_canary(con, run_id: str, task_id: str, selected_route: str, budget: int) -> dict:
    """Book a canary dispatch under the caller's dispatch transaction.

    The live budget and per-run limit are re-checked under the writer lock. A lost race
    still launches (the user allow already permits Hydrogen) but books canary=0: it is
    not spent budget, and like every canary-path row the trial path never reads it."""
    if not con.in_transaction:
        raise RuntimeError("Hydrogen canary booking requires a BEGIN IMMEDIATE dispatch transaction")
    used_run = _run_canaries(con, run_id)
    spent = canary_spent(con)
    counted = bool(budget) and spent < budget and used_run < MAX_PER_RUN
    event_id = "HO" + uuid.uuid4().hex
    con.execute("INSERT INTO hydrogen_trial_opportunities(id, run_id, task_id, selected_route, experimental, "
                "created_at, canary) VALUES (?,?,?,?,?,?,?)",
                (event_id, run_id, task_id, selected_route, 1, now_iso(), 1 if counted else 0))
    return {"id": event_id, "canary": counted, "spent": spent + int(counted), "budget": budget,
            "blocked": None if counted else ("canary-complete" if spent >= budget else "one-canary-per-run")}


def snapshot(con) -> dict:
    """Last nineteen eligible opportunities, plus this pending opportunity."""
    # Canary-path rows (#1168, canary NOT NULL) are their own ledger; the trial window ignores them.
    rows = con.execute("SELECT experimental FROM hydrogen_trial_opportunities WHERE canary IS NULL "
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
                               "AND experimental=1 AND canary IS NULL", (run_id,)).fetchone()[0]
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
