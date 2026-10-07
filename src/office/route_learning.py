"""Route learning (#300): outcomes, attribution, comparable evidence, learned eligibility.

The learner reads runs.db and nothing else. It derives one outcome per ended
executor/worker dispatch, attributes a failure before it counts against a route,
and pools the outcomes into per-route evidence for the adaptive scorer
(`office.adaptive`). It is a contextual Beta-Bernoulli recommender: each route
keeps a success posterior whose prior is the benchmark, and comparable local
outcomes take over as their effective sample grows.

What the learner may not change lives here as code constants, not config:
the success definition (`derive_outcomes`), the attribution classes and their
learning weights (`ATTRIBUTION_WEIGHT`), and the maturity and replay rules that
gate an automatic eligibility change (`eligibility_transitions`). A change to
any of them is a code change that goes through review.
"""
from __future__ import annotations

import json
import math
import sqlite3
import uuid
from datetime import datetime, timezone

from office import scoring

LEARNER_VERSION = "route-learner-1"

ADAPTIVE_ROLES = ("executor", "worker")

# Learning authority per attribution class. A plan, environment, or reviewer
# defect is not the route's failure; mixed and unknown teach at reduced weight.
ATTRIBUTION_WEIGHT = {"route": 1.0, "mixed": 0.5, "unknown": 0.25, "plan": 0.0, "environment": 0.0, "reviewer": 0.0}

_ROUTE_FINDING_CATEGORIES = {"code_review", "checks", "visual", "carried", "convergence"}
_PLAN_FINDING_CATEGORIES = {"plan", "requirement-contradiction", "false-contract-assumption",
                            "double-scope-ownership", "unsafe-or-unauthorized-action", "brief"}
_MATERIAL = {"material", "major", "high", "critical"}
# outcome_labels.primary_attribution (protocol/telemetry-learning.md classes) -> learner class.
_LABEL_ATTRIBUTION = {"model": "route", "harness": "route", "adapter": "route", "planner": "plan", "brief": "plan",
                      "repository": "plan", "environment": "environment", "environment/network": "environment",
                      "quota": "environment", "quota/account": "environment", "verification": "reviewer",
                      "unknown": "unknown"}

# Eligibility maturity bar. A transition needs all of: this much attributed
# evidence, outcomes from this many runs, a posterior bound past the threshold,
# and a held-out replay that agrees.
MIN_EFFECTIVE_SAMPLES = 10.0
MIN_RUNS = 3
PROMOTE_LOWER_BOUND = 0.60
DEMOTE_UPPER_BOUND = 0.30
# The benchmark prior counts for less here than in ranking: eligibility is a
# claim about this route's own record.
ELIGIBILITY_PRIOR_STRENGTH = 4.0
HOLDOUT_FRACTION = 0.2
MIN_HOLDOUT = 2

# Evidence pooling defaults (calibration values; config may override them).
DEFAULTS = {
    "prior_strength": 8.0,            # pseudo-observations the benchmark prior is worth
    "other_playbook_weight": 0.5,     # outcome from another task shape
    "size_mismatch_weight": 0.75,     # outcome from another size class (both known)
    "fix_kind_weight": 0.75,          # fresh vs fix dispatch mismatch
    "stale_harness_major_weight": 0.25,
    "half_life_days": 120.0,
}


def _ts(value) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _cols(con, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}


def route_key(harness, model, effort) -> str:
    """Evidence identity: harness x invocation model x effort. The harness major is
    kept beside it so a major change reads as stale evidence, not a new route."""
    return f"{harness}/{model}@{effort}"


def candidate_key(c: dict) -> str:
    return route_key(c.get("harness"), c.get("invocation_model_id") or c.get("model_id"), c.get("effort"))


def _split_triple(triple: str) -> tuple[str | None, str | None, str | None, str | None]:
    """harness@version/model@effort -> (harness, major, model, effort)."""
    try:
        head, rest = triple.split("/", 1)
        harness, _, version = head.partition("@")
        model, _, effort = rest.rpartition("@")
        return harness, scoring.harness_major(version), model, effort
    except ValueError:
        return None, None, None, None


# ------------------------------------------------------------------ outcomes

def derive_outcomes(con: sqlite3.Connection) -> list[dict]:
    """One outcome per ended executor/worker dispatch whose result is known.

    success: a revision this dispatch produced became the task's accepted revision.
    failure: it did not, and the task has since moved on (accepted through another
    dispatch, cancelled, relaunched) or the run ended. A dispatch whose task is
    still in flight is undetermined and yields nothing.
    """
    dcols = _cols(con, "dispatches")
    if "task_id" not in dcols:
        return []
    opt = lambda c: c if c in dcols else f"NULL AS {c}"  # noqa: E731 - older schemas
    rows = con.execute(
        "SELECT d.id, d.run_id, d.role, d.task_id, d.triple, d.harness, d.model, d.effort, d.started_at, d.ended_at, "
        f"{opt('terminal_classification')}, {opt('exit_code')}, d.outcome, d.attribution, {opt('stall_kind')}, "
        "d.money_actual FROM dispatches d WHERE d.role IN ('executor','worker') AND d.ended_at IS NOT NULL "
        "ORDER BY d.ended_at, d.id").fetchall()
    if not rows:
        return []
    have_runs = _cols(con, "runs")
    runs = {}
    if {"playbook", "phase"} <= have_runs:
        for r in con.execute("SELECT id, playbook, phase, risk_json FROM runs").fetchall():
            try:
                size = (json.loads(r[3] or "{}") or {}).get("size_class")
            except ValueError:
                size = None
            runs[r[0]] = {"playbook": r[1], "phase": r[2], "size_class": size}
    tasks = {}
    if "accepted_revision_id" in _cols(con, "tasks"):
        for t in con.execute("SELECT run_id, id, status, accepted_revision_id FROM tasks").fetchall():
            tasks[(t[0], t[1])] = {"status": t[2], "accepted": t[3]}
    revs: dict[str, list[str]] = {}
    if "dispatch_id" in _cols(con, "revisions"):
        for r in con.execute("SELECT id, dispatch_id FROM revisions WHERE dispatch_id IS NOT NULL").fetchall():
            revs.setdefault(r[1], []).append(r[0])
    gates: dict[str, list[tuple[str, str]]] = {}
    if "revision_id" in _cols(con, "gates"):
        # A convergence-contract gate that could not run has a runtime status and no verdict.
        status = "COALESCE(verdict, review_status)" if "review_status" in _cols(con, "gates") else "verdict"
        for g in con.execute(f"SELECT revision_id, kind, {status} FROM gates WHERE revision_id IS NOT NULL").fetchall():
            gates.setdefault(g[0], []).append((g[1], g[2]))
    fcols = _cols(con, "findings")
    findings: dict[str, list[tuple]] = {}
    if {"revision_id", "category", "state"} <= fcols:
        convergence = {"contract", "blocking", "root_cause"} <= fcols
        extra = ", contract, blocking, root_cause" if convergence else ""
        for f in con.execute(f"SELECT revision_id, dispatch_id, category, severity, state{extra} FROM findings").fetchall():
            if convergence and f[5] == "convergence-v1":
                # #337: blocking, not severity, says a finding held progression;
                # a lane finding belongs to its owner's producing dispatch; a
                # plan-class root cause is the plan's, not the route's.
                category = f[7] if f[7] in _PLAN_FINDING_CATEGORIES else f[2]
                severity = "material" if f[6] else "minor"
                key = f[1] or f[0]
                if key:
                    findings.setdefault(key, []).append((category, severity, f[4]))
                continue
            key = f[0] or f[1]
            if key:
                findings.setdefault(key, []).append((f[2], f[3], f[4]))
    labels = {r[0]: r[1] for r in con.execute(
        "SELECT dispatch_id, primary_attribution FROM outcome_labels WHERE primary_attribution IS NOT NULL").fetchall()}
    starts: dict[tuple, list[str]] = {}
    for r in rows:
        starts.setdefault((r[1], r[3]), []).append(r[8] or "")

    out = []
    for (did, run_id, role, task_id, triple, harness, model, effort, started, ended, term, exit_code, outcome,
         attribution, stall, money) in rows:
        t_h, t_major, t_model, t_effort = _split_triple(triple or "")
        harness, model, effort = harness or t_h, model or t_model, effort or t_effort
        if not harness or not model:
            continue
        run = runs.get(run_id, {})
        task = tasks.get((run_id, task_id), {})
        my_revs = revs.get(did, [])
        success = bool(task.get("accepted") and task["accepted"] in my_revs)
        later = any(s > (started or "") for s in starts.get((run_id, task_id), []))
        settled = success or later or task.get("status") in ("accepted", "cancelled") \
            or run.get("phase") in ("closed", "abandoned")
        if not settled:
            continue
        rounds = sum(1 for rid in my_revs for kind, verdict in gates.get(rid, [])
                     if kind == "code_review" and verdict == "CHANGES_REQUIRED")
        checks_failed = sum(1 for rid in my_revs for kind, verdict in gates.get(rid, [])
                            if kind == "checks" and verdict in ("CHANGES_REQUIRED", "RECHECK"))
        if success:
            attr, conf, prov = "route", 1.0, "accepted revision"
        else:
            attr, conf, prov = attribute_failure(
                label=labels.get(did), task_status=task.get("status"), run_phase=run.get("phase"),
                term=term, outcome=outcome, attribution=attribution, stall=stall, exit_code=exit_code,
                has_revision=bool(my_revs), rounds=rounds, checks_failed=checks_failed,
                gate_verdicts=[v for rid in my_revs for _, v in gates.get(rid, [])],
                findings=[f for key in [*my_revs, did] for f in findings.get(key, [])])
        s, e = _ts(started), _ts(ended)
        wall = (e - s).total_seconds() if s and e and e >= s else None
        weight = 1.0 if success else ATTRIBUTION_WEIGHT[attr] * (0.5 + 0.5 * conf)
        out.append({
            "dispatch_id": did, "run_id": run_id, "task_id": task_id, "role": role,
            "route": route_key(harness, model, effort), "harness_major": t_major,
            "playbook": run.get("playbook"), "size_class": run.get("size_class"),
            "kind": "fix" if len(starts.get((run_id, task_id), [])) > 1 and
                    min(starts[(run_id, task_id)]) < (started or "") else "fresh",
            "success": success, "attribution": attr, "attribution_confidence": round(conf, 2),
            "attribution_provenance": prov, "learn_weight": round(weight, 4),
            "review_rounds": rounds, "wall_seconds": wall,
            "money_actual": float(money) if isinstance(money, (int, float)) else None,
            "ended_at": ended,
        })
    return out


def attribute_failure(*, label, task_status, run_phase, term, outcome, attribution, stall, exit_code,
                      has_revision, rounds, checks_failed, gate_verdicts, findings) -> tuple[str, float, str]:
    """(class, confidence, provenance) for a dispatch that did not land.

    Only route-attributed evidence teaches at full weight. Recorded labels win
    over heuristics; the heuristics read findings, gate verdicts and the
    dispatch's own terminal record."""
    if label and label in _LABEL_ATTRIBUTION:
        return _LABEL_ATTRIBUTION[label], 0.9, f"outcome label: {label}"
    if task_status == "cancelled":
        return "plan", 0.8, "task cancelled by a plan change"
    if outcome == "environment_failure" or stall:
        return "environment", 0.8, f"dispatch recorded {stall or outcome}"
    if not has_revision:
        if term in ("signal", "lost"):
            return "environment", 0.6, f"session {term} before any submission"
        if run_phase == "abandoned":
            return "unknown", 0.5, "run abandoned before any submission"
        if exit_code not in (None, 0) or term == "nonzero":
            return "mixed", 0.5, "harness exited nonzero before any submission"
        return "mixed", 0.4, "ended without submitting"
    material = [(cat, sev) for cat, sev, st in findings if st != "retracted" and (sev or "") in _MATERIAL]
    route_hit = any(cat in _ROUTE_FINDING_CATEGORIES for cat, _ in material)
    plan_hit = any(cat in _PLAN_FINDING_CATEGORIES for cat, _ in material)
    if route_hit and plan_hit:
        return "mixed", 0.6, "material code and plan findings"
    if plan_hit:
        return "plan", 0.7, "material plan/brief findings only"
    if route_hit:
        return "route", 0.8, "material code-review findings"
    if rounds or checks_failed:
        return "route", 0.6, f"{rounds} review and {checks_failed} check rejection(s)"
    if gate_verdicts and all(v == "UNAVAILABLE" for v in gate_verdicts):
        return "reviewer", 0.6, "review unavailable"
    if run_phase == "abandoned":
        return "unknown", 0.5, "run abandoned"
    return "unknown", 0.3, "no attributable evidence"


def episodes(outcomes: list[dict]) -> list[dict]:
    """Group dispatch outcomes into task-route episodes: the learning unit.

    A task that needs a fix round on the same route is one episode with two
    attempts, not a failure plus a success. The episode succeeds when one of its
    dispatches landed. A failed episode takes the most authoritative failure
    attribution among its dispatches (route > mixed > unknown > others)."""
    order = {"route": 5, "mixed": 4, "unknown": 3, "reviewer": 2, "environment": 1, "plan": 0}
    groups: dict[tuple, list[dict]] = {}
    for o in outcomes:
        groups.setdefault((o["run_id"], o["task_id"], o["route"]), []).append(o)
    out = []
    for (run_id, task_id, route), rows in groups.items():
        rows.sort(key=lambda o: o.get("ended_at") or "")
        success = any(o["success"] for o in rows)
        if success:
            attr, conf, prov, weight = "route", 1.0, "task landed on this route", 1.0
        else:
            worst = max(rows, key=lambda o: (order[o["attribution"]], o["attribution_confidence"]))
            attr, conf, prov = worst["attribution"], worst["attribution_confidence"], worst["attribution_provenance"]
            weight = ATTRIBUTION_WEIGHT[attr] * (0.5 + 0.5 * conf)
        walls = [o["wall_seconds"] for o in rows if o.get("wall_seconds")]
        money = [o["money_actual"] for o in rows if o.get("money_actual") is not None]
        last = rows[-1]
        out.append({
            "run_id": run_id, "task_id": task_id, "route": route, "role": last["role"],
            "harness_major": last.get("harness_major"), "playbook": last.get("playbook"),
            "size_class": last.get("size_class"), "kind": rows[0].get("kind"),
            "success": success, "attribution": attr, "attribution_confidence": conf,
            "attribution_provenance": prov, "learn_weight": round(weight, 4),
            "attempts": len(rows), "review_rounds": sum(o["review_rounds"] for o in rows),
            "wall_seconds": sum(walls) if walls else None, "money_actual": sum(money) if money else None,
            "dispatch_ids": [o["dispatch_id"] for o in rows], "ended_at": last.get("ended_at"),
        })
    out.sort(key=lambda e: (e.get("ended_at") or "", e["route"]))
    return out


# ------------------------------------------------------------------ evidence

def comparability(outcome: dict, context: dict, cfg: dict) -> float:
    """How much an outcome from `outcome`'s context says about `context`."""
    w = 1.0
    if context.get("playbook") and outcome.get("playbook") and outcome["playbook"] != context["playbook"]:
        w *= cfg["other_playbook_weight"]
    if context.get("size_class") and outcome.get("size_class") and outcome["size_class"] != context["size_class"]:
        w *= cfg["size_mismatch_weight"]
    if context.get("dispatch_kind") and outcome.get("kind") and outcome["kind"] != context["dispatch_kind"]:
        w *= cfg["fix_kind_weight"]
    return w


def evidence_for(outcomes: list[dict], candidates: list[dict], context: dict, *, as_of: str,
                 config: dict | None = None) -> dict:
    """Per candidate key: weighted local evidence comparable to `context`, over
    task-route episodes (`episodes`).

    Weights multiply attribution authority, context comparability, recency
    (half-life), and staleness when the harness major differs from the
    candidate's. Returned numbers are JSON-ready and are what the scorer and the
    audit record read; the raw outcome rows never enter a routing decision."""
    cfg = {**DEFAULTS, **(config or {})}
    now = _ts(as_of) or datetime.now(timezone.utc)
    want = {}
    for c in candidates:
        want.setdefault(candidate_key(c), scoring.harness_major(c.get("harness_version")))
    stats = {}
    eps = episodes(outcomes)
    for key, major in want.items():
        s = f = n = rounds = attempts = 0.0
        walls, money, runs, raw = [], [], set(), 0
        stale = 0.0
        attributions: dict[str, int] = {}
        for o in eps:
            if o["route"] != key:
                continue
            raw += 1
            attributions[o["attribution"]] = attributions.get(o["attribution"], 0) + (0 if o["success"] else 1)
            w = o["learn_weight"] * comparability(o, context, cfg)
            ended = _ts(o.get("ended_at"))
            if ended:
                age_days = max(0.0, (now - ended).total_seconds() / 86400.0)
                w *= 0.5 ** (age_days / cfg["half_life_days"])
            if o.get("harness_major") and major and o["harness_major"] != major:
                w *= cfg["stale_harness_major_weight"]
                stale += 1
            if w <= 0:
                continue
            n += w
            if o["success"]:
                s += w
            else:
                f += w
            rounds += w * o["review_rounds"]
            attempts += w * o["attempts"]
            runs.add(o["run_id"])
            # Per attempt: the scorer multiplies by expected attempts itself.
            if o.get("wall_seconds"):
                walls.append(o["wall_seconds"] / o["attempts"])
            if o.get("money_actual") is not None:
                money.append(o["money_actual"] / o["attempts"])
        stats[key] = {
            "n_raw": raw, "n_effective": round(n, 3), "successes": round(s, 3), "failures": round(f, 3),
            "runs": len(runs), "review_rounds_mean": round(rounds / n, 3) if n else None,
            "attempts_mean": round(attempts / n, 3) if n else None,
            "wall_seconds_median": _median(walls), "money_actual_median": _median(money),
            "stale_outcomes": int(stale), "failure_attribution": attributions,
        }
    return {"as_of": now.isoformat(), "learner_version": LEARNER_VERSION, "context": dict(context),
            "pooling": cfg, "routes": stats}


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    v = sorted(values)
    mid = len(v) // 2
    return round(v[mid] if len(v) % 2 else (v[mid - 1] + v[mid]) / 2, 3)


def beta_bounds(successes: float, failures: float, prior_p: float, strength: float) -> tuple[float, float, float]:
    """Posterior mean and a ~90% normal-approximation interval for a Beta posterior."""
    a = strength * prior_p + successes
    b = strength * (1 - prior_p) + failures
    mean = a / (a + b)
    var = a * b / ((a + b) ** 2 * (a + b + 1))
    sd = math.sqrt(var)
    return mean, max(0.0, mean - 1.645 * sd), min(1.0, mean + 1.645 * sd)


# ------------------------------------------------------------------ learned eligibility

def ensure_schema(con: sqlite3.Connection) -> None:
    con.execute("CREATE TABLE IF NOT EXISTS learned_eligibility(id TEXT PRIMARY KEY, route TEXT NOT NULL, "
                "role TEXT NOT NULL, state TEXT NOT NULL, previous_state TEXT, evidence_json TEXT NOT NULL, "
                "replay_json TEXT NOT NULL, learner_version TEXT NOT NULL, created_at TEXT NOT NULL)")
    con.execute("CREATE TABLE IF NOT EXISTS route_audit(id TEXT PRIMARY KEY, run_id TEXT, task_id TEXT, role TEXT, "
                "phase TEXT NOT NULL, plan_version INTEGER, decision_hash TEXT, policy_version TEXT, "
                "learner_version TEXT, seed TEXT, primary_route TEXT, dispatched_route TEXT, explored INTEGER, "
                "disclosure_json TEXT NOT NULL, created_at TEXT NOT NULL)")
    con.execute("CREATE TABLE IF NOT EXISTS route_attributions(dispatch_id TEXT PRIMARY KEY, run_id TEXT, "
                "route TEXT NOT NULL, success INTEGER NOT NULL, attribution TEXT NOT NULL, confidence REAL NOT NULL, "
                "provenance TEXT, learn_weight REAL NOT NULL, learner_version TEXT NOT NULL, derived_at TEXT NOT NULL)")


def current_eligibility(con: sqlite3.Connection, role: str) -> dict[str, dict]:
    """route key -> latest learned eligibility event for `role` (absent = no learned change)."""
    try:
        rows = con.execute("SELECT id, route, state, created_at FROM learned_eligibility WHERE role=? "
                           "ORDER BY created_at, rowid", (role,)).fetchall()
    except sqlite3.OperationalError:
        return {}
    out = {}
    for r in rows:
        out[r[1]] = {"event_id": r[0], "state": r[2], "at": r[3]}
    return {k: v for k, v in out.items() if v["state"] != "neutral"}


def _route_samples(outcomes: list[dict], key: str) -> list[dict]:
    return [o for o in outcomes if o["route"] == key and (o["success"] or o["attribution"] == "route")]


def replay_check(samples: list[dict], direction: str, prior_p: float, strength: float) -> dict:
    """Fit on the oldest outcomes, test the change on the newest held-out slice.

    A promotion is validated when the held-out success rate stays at or above the
    promotion bound and the fitted posterior predicts the holdout no worse than the
    benchmark prior alone (log loss). A demotion mirrors it. The learner never
    tunes on the holdout."""
    ordered = sorted(samples, key=lambda o: o.get("ended_at") or "")
    k = max(MIN_HOLDOUT, int(round(len(ordered) * HOLDOUT_FRACTION)))
    train, hold = ordered[:-k], ordered[-k:]
    if len(train) < 3 or len(hold) < MIN_HOLDOUT:
        return {"validated": False, "reason": "not enough outcomes to hold any out", "train": len(train),
                "holdout": len(hold)}
    s = sum(1 for o in train if o["success"])
    fitted, _, _ = beta_bounds(s, len(train) - s, prior_p, strength)
    hold_rate = sum(1 for o in hold if o["success"]) / len(hold)

    def logloss(p):
        p = min(max(p, 1e-3), 1 - 1e-3)
        return -sum(math.log(p) if o["success"] else math.log(1 - p) for o in hold) / len(hold)
    learned, prior = logloss(fitted), logloss(prior_p)
    if direction == "promote":
        ok = hold_rate >= PROMOTE_LOWER_BOUND and learned <= prior
    else:
        ok = hold_rate <= DEMOTE_UPPER_BOUND + 0.15 and learned <= prior
    return {"validated": ok, "train": len(train), "holdout": len(hold), "holdout_success_rate": round(hold_rate, 3),
            "fitted_p": round(fitted, 3), "logloss_learned": round(learned, 4), "logloss_prior": round(prior, 4),
            "reason": "held-out outcomes agree" if ok else "held-out outcomes do not support the change"}


def eligibility_transitions(outcomes: list[dict], routes: dict[str, dict], current: dict[str, dict], *,
                            strength: float = ELIGIBILITY_PRIOR_STRENGTH) -> list[dict]:
    """Learned eligibility changes the evidence now supports.

    `routes`: route key -> {"prior_p": benchmark prior}. Only route-attributed
    failures and successes count (a brief or environment defect never moves a
    route). A route moves when its weighted sample, run spread, posterior bound
    and held-out replay all clear the maturity bar; it moves back to neutral when
    a standing change no longer holds. Factual gates (capabilities, floors,
    quota, quarantine) are not inputs here and cannot be changed by it."""
    out = []
    for key, info in sorted(routes.items()):
        samples = _route_samples(outcomes, key)
        n = len(samples)
        s = sum(1 for o in samples if o["success"])
        runs = len({o["run_id"] for o in samples})
        prior_p = info.get("prior_p", 0.5)
        mean, lo, hi = beta_bounds(s, n - s, prior_p, strength)
        evidence = {"samples": n, "successes": s, "runs": runs, "posterior_mean": round(mean, 3),
                    "lower_90": round(lo, 3), "upper_90": round(hi, 3), "prior_p": round(prior_p, 3)}
        state = (current.get(key) or {}).get("state", "neutral")
        target = None
        if n >= MIN_EFFECTIVE_SAMPLES and runs >= MIN_RUNS:
            if lo >= PROMOTE_LOWER_BOUND:
                target = "learned-eligible"
            elif hi <= DEMOTE_UPPER_BOUND:
                target = "learned-ineligible"
        if target is None and state != "neutral":
            # A standing change no longer supported reverts once mature evidence disagrees.
            if n >= MIN_EFFECTIVE_SAMPLES and not (
                    (state == "learned-eligible" and lo >= PROMOTE_LOWER_BOUND - 0.1)
                    or (state == "learned-ineligible" and hi <= DEMOTE_UPPER_BOUND + 0.1)):
                out.append({"route": key, "state": "neutral", "previous_state": state, "evidence": evidence,
                            "replay": {"validated": True, "reason": "standing change no longer supported"}})
            continue
        if target is None or target == state:
            continue
        replay = replay_check(samples, "promote" if target == "learned-eligible" else "demote", prior_p, strength)
        if not replay["validated"]:
            continue
        out.append({"route": key, "state": target, "previous_state": state, "evidence": evidence, "replay": replay})
    return out


def refresh(con: sqlite3.Connection, role_priors: dict[str, dict[str, dict]] | None = None) -> list[dict]:
    """Persist derived attributions and apply validated eligibility transitions.

    Called at run close/abandon inside the caller's transaction. Every
    transition is an append-only `learned_eligibility` row (auditable); a later
    row reverses it. `role_priors`: role -> route key -> {"prior_p"}; when omitted
    the routes seen in outcomes get a neutral 0.5 prior."""
    ensure_schema(con)
    outcomes = derive_outcomes(con)
    now = datetime.now(timezone.utc).isoformat()
    for o in outcomes:
        con.execute("INSERT OR REPLACE INTO route_attributions VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (o["dispatch_id"], o["run_id"], o["route"], int(o["success"]), o["attribution"],
                     o["attribution_confidence"], o["attribution_provenance"], o["learn_weight"], LEARNER_VERSION, now))
    written = []
    for role in ADAPTIVE_ROLES:
        role_outcomes = [e for e in episodes(outcomes) if e["role"] == role]
        priors = (role_priors or {}).get(role) or {o["route"]: {"prior_p": 0.5} for o in role_outcomes}
        for t in eligibility_transitions(role_outcomes, priors, current_eligibility_all(con, role)):
            row_id = "LE" + uuid.uuid4().hex[:12]
            con.execute("INSERT INTO learned_eligibility VALUES(?,?,?,?,?,?,?,?,?)",
                        (row_id, t["route"], role, t["state"], t["previous_state"], json.dumps(t["evidence"]),
                         json.dumps(t["replay"]), LEARNER_VERSION, now))
            written.append({"id": row_id, "role": role, **t})
    return written


def current_eligibility_all(con: sqlite3.Connection, role: str) -> dict[str, dict]:
    """Like current_eligibility, but keeps neutral rows (the transition base)."""
    try:
        rows = con.execute("SELECT id, route, state, created_at FROM learned_eligibility WHERE role=? "
                           "ORDER BY created_at, rowid", (role,)).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {r[1]: {"event_id": r[0], "state": r[2], "at": r[3]} for r in rows}


def recent_exploration(con: sqlite3.Connection, role: str, window: int = 20) -> list[bool]:
    """Whether each of the last `window` adaptive decisions for `role` explored (newest last)."""
    try:
        rows = con.execute("SELECT explored FROM route_audit WHERE role=? AND phase IN ('plan','dispatch','reroute') "
                           "ORDER BY created_at DESC, rowid DESC LIMIT ?", (role, window)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [bool(r[0]) for r in reversed(rows)]
