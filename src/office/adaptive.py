"""Adaptive executor/worker recommendation (#300).

`routing.route` qualifies candidates (hard exclusions, trust, capabilities,
floor, task shape, quota). For executor and worker routes it then hands the
qualifying set here, which compares them and returns a slate: a primary and up
to two fallbacks, each with a reason, a strength and a weakness, plus the full
per-candidate evidence for `office inspect route`.

Scoring is offline and reproducible: every input arrives in the request (the
candidates with their probed quota, the learner's evidence snapshot, the
routing seed and the exploration history), so the same request yields the same
decision. The objective is expected cost to a successful task plus speed, not
raw token price:

    p_success     Beta posterior; the benchmark prior is worth `prior_strength`
                  pseudo-outcomes, so comparable local evidence takes over as it grows
    attempt cost  catalog price x effort token multiplier (prior), or local spend
    cost to success  attempt cost x (1 + rework per review round) / p_success
    time to success  attempt wall x (1 + rework per review round) / p_success
    utility       weighted sum of effectiveness, cost, speed, quota headroom and
                  user preference, minus a small spread penalty

Candidates within the competitive band of the best utility are a close call:
a reproducible draw from the routing seed picks among them. Exploration, at a
small bounded rate, may promote an under-tested qualifying route whose utility
and cost stay near the best.
"""
from __future__ import annotations

import hashlib
import math

from office import route_learning, task_descriptors
from office.util import sha256_obj

POLICY_VERSION = "adaptive-2-task"

# Calibration defaults. Coefficients are provisional and replayable; config
# (`routing.adaptive`) overrides them. None of them is a hard gate except the
# budget ceiling, which only removes routes many times the cheapest cost. A new run
# applies a ceiling only when the user set one (#494); a run pinned before that keeps its own.
DEFAULTS = {
    "weights": {
        "balanced": {"effectiveness": 0.40, "cost": 0.25, "speed": 0.15, "quota": 0.10, "preference": 0.10},
        "money_saver": {"effectiveness": 0.30, "cost": 0.40, "speed": 0.10, "quota": 0.10, "preference": 0.10},
        "quota_saver": {"effectiveness": 0.30, "cost": 0.15, "speed": 0.15, "quota": 0.30, "preference": 0.10},
    },
    "competitive_band": 0.05,          # relative utility gap that counts as a close call
    "budget_ceiling_usd": 25.0,        # drop a route whose expected cost to success exceeds this (null: off); a
                                       # pinned config that omits it keeps this old value, a #494-era run sets it null
    "cost_scale_usd": 25.0,            # cost score falls linearly from 1 at $0 to 0 at this expected cost
    "spread_penalty": 0.03,            # per same-wave task already planned on the route (max 2)
    "exploration": {"rate": 0.08, "margin": 0.10, "min_samples_mature": 6.0, "max_percent_rolling_20": 10.0,
                    "max_per_run": 1, "max_cost_vs_primary_percent": 125.0},
    "priors": {
        "effort_token_multiplier": {"none": 0.4, "low": 0.6, "medium": 1.0, "high": 1.5, "xhigh": 2.2, "max": 3.0},
        # Effective (cache-adjusted) tokens per medium-effort attempt; replace with
        # local spend once dispatches record money_actual.
        "input_mtok_per_attempt": 0.05, "output_mtok_per_attempt": 0.06,
        "rework_per_review_round": 0.35, "review_rounds_prior": 0.6, "attempts_prior": 1.3,
        "benchmark_index": "Artificial Analysis Intelligence Index v4.3.2",
        "benchmark_midpoint": 38.0, "benchmark_scale": 8.0, "unbenchmarked_p": 0.45,
    },
}

SLATE_SIZE = 3
RANK_LABELS = ("PRIMARY", "FALLBACK 1", "FALLBACK 2")


def settings(config: dict | None) -> dict:
    """DEFAULTS deep-merged with `routing.adaptive` from config."""
    cfg = ((config or {}).get("routing") or {}).get("adaptive") or {}
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    for key, value in cfg.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            merged = dict(out[key])
            for k2, v2 in value.items():
                merged[k2] = {**merged[k2], **v2} if isinstance(v2, dict) and isinstance(merged.get(k2), dict) else v2
            out[key] = merged
        else:
            out[key] = value
    return out


def validate(cfg: dict) -> list[str]:
    """Config problems in `routing.adaptive` (empty when valid)."""
    s = settings(cfg)
    problems = []
    ceiling = s.get("budget_ceiling_usd")
    if ceiling is not None and (not isinstance(ceiling, (int, float)) or ceiling <= 0):
        problems.append("routing.adaptive.budget_ceiling_usd must be a positive number or null")
    scale = s.get("cost_scale_usd")
    if not isinstance(scale, (int, float)) or scale <= 0:
        problems.append("routing.adaptive.cost_scale_usd must be a positive number")
    band = s.get("competitive_band")
    if not isinstance(band, (int, float)) or not 0 <= band <= 0.25:
        problems.append("routing.adaptive.competitive_band must be between 0 and 0.25")
    for policy, w in (s.get("weights") or {}).items():
        if any(not isinstance(v, (int, float)) or v < 0 for v in w.values()):
            problems.append(f"routing.adaptive.weights.{policy} must be non-negative numbers")
        elif w.get("preference", 0) > 0.25:
            problems.append(f"routing.adaptive.weights.{policy}.preference must be <= 0.25 so preference cannot "
                            "steer every decision")
    rate = (s.get("exploration") or {}).get("rate", 0)
    if not isinstance(rate, (int, float)) or not 0 <= rate <= 0.25:
        problems.append("routing.adaptive.exploration.rate must be between 0 and 0.25")
    return problems


def _draw(seed: str, label: str) -> float:
    """A reproducible uniform draw in [0, 1) from the routing seed."""
    digest = hashlib.sha256(f"{seed}:{label}".encode()).hexdigest()
    return int(digest[:13], 16) / float(16 ** 13)


def benchmark_prior(c: dict, s: dict) -> tuple[float, str]:
    pri = s["priors"]
    score = (c.get("benchmark_indexes") or {}).get(pri["benchmark_index"])
    if score is None:
        return pri["unbenchmarked_p"], "no pinned benchmark score; neutral prior"
    p = 1.0 / (1.0 + math.exp(-(float(score) - pri["benchmark_midpoint"]) / pri["benchmark_scale"]))
    return min(max(p, 0.15), 0.92), f"{pri['benchmark_index']} = {score:g}"


def _attempt_cost_prior(c: dict, s: dict, descriptor: dict | None = None) -> float | None:
    price, cost = c.get("price_fields") or {}, c.get("cost") or {}
    tier = task_descriptors.price_tier(price, descriptor)
    is_tiered = type(price.get("prompt_token_threshold")) is int
    out = tier.get("output_per_mtok") if is_tiered else price.get(
        "output_per_mtok", cost.get("output_per_mtok", cost.get("money_estimate")))
    inp = tier.get("input_per_mtok") if is_tiered else price.get("input_per_mtok", cost.get("input_per_mtok"))
    if type(out) not in (int, float):
        return None
    pri = s["priors"]
    mult = pri["effort_token_multiplier"].get(c.get("effort") or "medium", 1.0)
    d = descriptor or {}
    input_tokens = d.get("estimated_input_tokens")
    output_tokens = d.get("estimated_output_tokens")
    input_mtok = input_tokens / 1e6 if type(input_tokens) is int else pri["input_mtok_per_attempt"] * mult
    output_mtok = output_tokens / 1e6 if type(output_tokens) is int else pri["output_mtok_per_attempt"] * mult
    return float(out) * output_mtok + (float(inp) if type(inp) in (float, int) else 0.0) * input_mtok


def _attempt_wall_prior(c: dict, s: dict, descriptor: dict | None = None) -> float | None:
    speed = c.get("speed_fields") or (c.get("cost") or {}).get("speed_fields") or {}
    tps, ttft = speed.get("output_tok_per_s"), speed.get("ttft_ms")
    if not isinstance(tps, (int, float)) or tps <= 0:
        return None
    pri = s["priors"]
    mult = pri["effort_token_multiplier"].get(c.get("effort") or "medium", 1.0)
    expected = (descriptor or {}).get("estimated_output_tokens")
    tokens = expected if type(expected) is int else pri["output_mtok_per_attempt"] * 1e6 * mult
    return (float(ttft or 0) / 1000.0) + tokens / float(tps)


def _shrink(prior: float | None, local: float | None, k: float, n: float) -> tuple[float | None, str]:
    if local is None:
        return prior, "prior" if prior is not None else "unknown"
    if prior is None:
        return local, "local"
    return (k * prior + n * local) / (k + n), "blended"


def _quota_component(c: dict, reserve: float) -> tuple[float, str]:
    q = c.get("quota") or {}
    rem = q.get("tightest_remaining_percent")
    if q.get("status") != "ok" or rem is None:
        return 0.35, "unknown"
    burn = float(q.get("projected_burn_percent") or 0)
    headroom = (float(rem) - burn - reserve) / max(1.0, 100.0 - reserve)
    return 0.5 + 0.5 * min(max(headroom, 0.0), 1.0), f"{float(rem):g}% left"


def _preference_component(c: dict, preferred_seed) -> tuple[float, int | None]:
    from office.routing import preferred_rank
    rank = preferred_rank(c, preferred_seed)
    return (1.0 / (1 + rank), rank) if rank is not None else (0.0, None)


def label(c: dict) -> str:
    return f"{c.get('harness')}/{c.get('model_id')}@{c.get('effort')}"


def score(candidates: list[dict], request: dict, s: dict) -> list[dict]:
    """Inspectable score rows for the qualifying `candidates` (input order kept)."""
    from office.routing import candidate_id
    evidence = (request.get("evidence") or {}).get("routes") or {}
    descriptor = _task_descriptor(request)
    reserve = float((request.get("policy") or {}).get("quota_reserve_percent", 5.0))
    k = float(((request.get("evidence") or {}).get("pooling") or {}).get(
        "prior_strength", route_learning.DEFAULTS["prior_strength"]))
    pri = s["priors"]
    rows = []
    for c in candidates:
        ev = evidence.get(route_learning.candidate_key(c)) or {}
        n = float(ev.get("n_effective") or 0.0)
        p0, p0_source = benchmark_prior(c, s)
        fit = task_descriptors.benchmark_fit(c, descriptor) if descriptor else None
        if fit and fit["applied"]:
            p0 = task_descriptors.apply_fit(p0, fit)
            p0_source += "; calibrated task benchmarks"
        k_eff = k if p0_source != "no pinned benchmark score; neutral prior" else k / 2
        p, lo, hi = route_learning.beta_bounds(float(ev.get("successes") or 0), float(ev.get("failures") or 0), p0, k_eff)
        rounds, rounds_basis = _shrink(pri["review_rounds_prior"], ev.get("review_rounds_mean"), k_eff, n)
        tier = task_descriptors.price_tier(c.get("price_fields") or {}, descriptor)
        # No prompt estimate: preserve the conservative published tier rather than
        # substitute spend measured from unknown (often short) context episodes.
        local_money = None if tier["tier"] == "unknown-conservative" else ev.get("money_actual_median")
        cost, cost_basis = _shrink(_attempt_cost_prior(c, s, descriptor), local_money, k_eff, n)
        wall, wall_basis = _shrink(_attempt_wall_prior(c, s, descriptor), ev.get("wall_seconds_median"), k_eff, n)
        per_episode, _ = _shrink(pri["attempts_prior"], ev.get("attempts_mean"), k_eff, n)
        attempts = per_episode / max(p, 0.05)
        rework = 1.0 + pri["rework_per_review_round"] * (rounds or 0.0)
        quota_score, quota_note = _quota_component(c, reserve)
        pref_score, pref_rank = _preference_component(c, request.get("preferred_seed"))
        rows.append({
            "route": candidate_id(c), "label": label(c), "evidence_key": route_learning.candidate_key(c),
            "effort": c.get("effort"),
            "benchmark": {"prior_p": round(p0, 4), "source": p0_source,
                          "authority": round(k_eff / (k_eff + n), 3) if (k_eff + n) else 1.0,
                          **({"task_fit": fit} if fit else {})},
            "local": {"n_effective": round(n, 3), "n_raw": ev.get("n_raw", 0), "runs": ev.get("runs", 0),
                      "successes": ev.get("successes", 0), "failures": ev.get("failures", 0),
                      "failure_attribution": ev.get("failure_attribution") or {},
                      "stale_outcomes": ev.get("stale_outcomes", 0)},
            "p_success": round(p, 4), "p_interval_90": [round(lo, 4), round(hi, 4)],
            "review_rounds": round(rounds, 3) if rounds is not None else None, "review_rounds_basis": rounds_basis,
            "attempt_cost": round(cost, 5) if cost is not None else None, "cost_basis": cost_basis,
            "pricing": tier,
            "attempt_wall_seconds": round(wall, 1) if wall is not None else None, "wall_basis": wall_basis,
            "expected_attempts": round(attempts, 3),
            "cost_to_success": round(cost * rework * attempts, 5) if cost is not None else None,
            "time_to_success_seconds": round(wall * rework * attempts, 1) if wall is not None else None,
            "quota": {"score": round(quota_score, 4), "state": quota_note},
            "preference": {"score": round(pref_score, 4), "seed_rank": pref_rank},
        })
    return rows


def _relative(values: list[float | None], span: float) -> list[float]:
    """1.0 for the lowest known value, falling with log ratio to 0 at `span` x; unknown -> 0.5."""
    known = [v for v in values if v is not None and v > 0]
    if not known:
        return [0.5] * len(values)
    best = min(known)
    out = []
    for v in values:
        if v is None or v <= 0:
            out.append(0.5)
        else:
            out.append(min(1.0, max(0.0, 1.0 - math.log(v / best) / math.log(max(span, 1.0001)))))
    return out


def recommend(candidates: list[dict], request: dict, *, config: dict | None = None) -> dict:
    """Rank qualifying candidates and build the slate and its audit record."""
    s = settings(config or {"routing": {"adaptive": request.get("adaptive_config") or {}}})
    policy = request.get("cost_policy") or (request.get("policy") or {}).get("cost_policy") or "balanced"
    weights = s["weights"].get(policy) or s["weights"]["balanced"]
    candidates, aliases = _dedupe(candidates)
    rows = score(candidates, request, s)
    by_id = {r["route"]: c for r, c in zip(rows, candidates)}
    rejected = []

    # Budget ceiling: the only cost-based removal. It is absolute (per task), so a
    # very cheap route never makes every stronger route "too expensive".
    ceiling = s.get("budget_ceiling_usd")
    if ceiling:
        kept = []
        for r in rows:
            if r["cost_to_success"] and r["cost_to_success"] > float(ceiling):
                rejected.append({"candidate": r["route"], "stage": 8,
                                 "reason": f"expected cost to success ~${r['cost_to_success']:.2f} is over the "
                                           f"${float(ceiling):g} budget ceiling"})
            else:
                kept.append(r)
        rows = kept
    if not rows:
        return {"rows": [], "rejected": rejected, "slate": [], "audit": {}}

    # Absolute, not relative: $0.05 vs $0.30 is not the gap $1 vs $6 is.
    scale = float(s["cost_scale_usd"])
    cost_scores = [0.5 if r["cost_to_success"] is None else max(0.0, 1.0 - r["cost_to_success"] / scale)
                   for r in rows]
    speed_scores = _relative([r["time_to_success_seconds"] for r in rows], 8.0)
    load = request.get("wave_load") or {}
    for r, cs, ss in zip(rows, cost_scores, speed_scores):
        comps = {"effectiveness": r["p_success"], "cost": round(cs, 4), "speed": round(ss, 4),
                 "quota": r["quota"]["score"], "preference": r["preference"]["score"]}
        contrib = {k: round(weights.get(k, 0.0) * v, 4) for k, v in comps.items()}
        spread = round(-float(s["spread_penalty"]) * min(int(load.get(r["route"], 0)), 2), 4)
        r["components"] = comps
        r["contributions"] = {**contrib, "spread": spread}
        r["utility"] = round(sum(contrib.values()) + spread, 4)
    rows.sort(key=lambda r: (-r["utility"], r["route"]))
    numeric_order = [r["route"] for r in rows]
    top = rows[0]["utility"]
    band = float(s["competitive_band"])
    for r in rows:
        r["in_competitive_band"] = r["utility"] >= top - abs(top) * band
        r["gap_to_best"] = round(top - r["utility"], 4)

    seed = request.get("routing_seed") or sha256_obj({k: request.get(k) for k in ("run_id", "task_id", "role")})
    clincher = {"used": False, "band": [r["route"] for r in rows if r["in_competitive_band"]], "seed": seed}
    if len(clincher["band"]) > 1:
        draw = _draw(seed, "clincher")
        members = [r for r in rows if r["in_competitive_band"]]
        total = sum(max(r["utility"], 1e-6) for r in members)
        acc, picked = 0.0, members[-1]
        for r in members:
            acc += max(r["utility"], 1e-6) / total
            if draw < acc:
                picked = r
                break
        clincher.update({"used": True, "draw": round(draw, 6), "picked": picked["route"],
                         "rule": "utility-weighted draw among routes within the competitive band"})
        rows.remove(picked)
        rows.insert(0, picked)

    exploration = _explore(rows, request, s, seed)
    if exploration.get("picked"):
        chosen = next(r for r in rows if r["route"] == exploration["picked"])
        rows.remove(chosen)
        rows.insert(0, chosen)

    for i, r in enumerate(rows):
        r["rank"] = i + 1
    slate = [_slate_entry(i, r, rows, clincher, exploration) for i, r in enumerate(rows[:SLATE_SIZE])]
    audit = {
        "policy_version": POLICY_VERSION, "learner_version": route_learning.LEARNER_VERSION,
        "cost_policy": policy, "weights": weights, "competitive_band": band, "budget_ceiling_usd": ceiling,
        "cost_scale_usd": s["cost_scale_usd"],
        **({"task_descriptor": _task_descriptor(request),
            "descriptor_version": task_descriptors.VERSION} if _task_descriptor(request) else {}),
        "context": (request.get("evidence") or {}).get("context") or request.get("context") or {},
        "evidence_as_of": (request.get("evidence") or {}).get("as_of"),
        "evidence_digest": sha256_obj((request.get("evidence") or {}).get("routes") or {}),
        **({"budget_ceiling_source": (request.get("budget_ceiling") or {}).get("source")}
           if request.get("budget_ceiling") else {}),
        "numeric_order": numeric_order, "clincher": clincher, "exploration": exploration,
        "spread": {"wave_load": load, "applied": any(r["contributions"]["spread"] for r in rows)},
        "preferred_seed": request.get("preferred_seed"), "aliases": aliases,
        "candidates": rows, "slate": slate,
    }
    return {"rows": rows, "rejected": rejected, "slate": slate, "audit": audit, "by_id": by_id}


def trial_rows(pool: list[dict], rec: dict, request: dict, config: dict | None = None) -> dict[str, dict]:
    """Score discovery candidates against the decision's primary (#494).

    A candidate here has not qualified for the slate and never enters it. Its utility
    is computed with the same formula and weights the slate used, in the slate's own
    frame (its speed score is relative to the slate's times), so the exploration margin
    and cost bounds mean the same thing for a trial. An unbenchmarked candidate keeps
    the neutral, uncertain prior `score` gives it. Returns route -> {row, utility,
    within_margin, within_cost, within_ceiling, reason}.
    """
    from office.routing import candidate_id
    s = settings(config or {"routing": {"adaptive": request.get("adaptive_config") or {}}})
    policy = request.get("cost_policy") or (request.get("policy") or {}).get("cost_policy") or "balanced"
    weights = s["weights"].get(policy) or s["weights"]["balanced"]
    kept, _ = _dedupe(pool)
    scored = score(kept, request, s)
    main = rec.get("rows") or []
    primary = next((r for r in main if r["route"] == rec["slate"][0]["route"]), None) if rec.get("slate") else None
    ex, scale, ceiling = s["exploration"], float(s["cost_scale_usd"]), s.get("budget_ceiling_usd")
    out = {}
    for c, r in zip(kept, scored):
        speed = _relative([x["time_to_success_seconds"] for x in main] + [r["time_to_success_seconds"]], 8.0)[-1]
        cost = 0.5 if r["cost_to_success"] is None else max(0.0, 1.0 - r["cost_to_success"] / scale)
        comps = {"effectiveness": r["p_success"], "cost": round(cost, 4), "speed": round(speed, 4),
                 "quota": r["quota"]["score"], "preference": r["preference"]["score"]}
        utility = round(sum(round(weights.get(k, 0.0) * v, 4) for k, v in comps.items()), 4)
        margin_ok = primary is None or utility >= primary["utility"] - float(ex["margin"])
        cost_ok = (primary is None or r["cost_to_success"] is None or primary["cost_to_success"] is None
                   or r["cost_to_success"] <= primary["cost_to_success"] * float(ex["max_cost_vs_primary_percent"]) / 100)
        ceiling_ok = not (ceiling and r["cost_to_success"] and r["cost_to_success"] > float(ceiling))
        reason = ("utility more than the exploration margin behind the primary" if not margin_ok
                  else "expected cost over the exploration cost bound" if not cost_ok
                  else f"expected cost over the ${float(ceiling):g} budget ceiling" if not ceiling_ok else None)
        out[candidate_id(c)] = {"row": {**r, "components": comps, "utility": utility}, "utility": utility,
                                "within_margin": margin_ok, "within_cost": cost_ok, "within_ceiling": ceiling_ok,
                                "reason": reason}
    return out


def _task_descriptor(request: dict) -> dict:
    """The task descriptor scoring used: request context first, else the evidence context."""
    return ((request.get("context") or (request.get("evidence") or {}).get("context") or {})
            .get("task_descriptor") or {})


def _dedupe(candidates: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """One row per invocation: an alias (`sonnet`) and the concrete model it
    resolves to are the same route. The concrete row is kept."""
    from office.routing import candidate_id
    keep: dict[str, dict] = {}
    aliases: dict[str, str] = {}
    for c in candidates:
        key = route_learning.candidate_key(c)
        cur = keep.get(key)
        concrete = c.get("model_id") == c.get("invocation_model_id")
        if cur is None:
            keep[key] = c
        elif concrete and cur.get("model_id") != cur.get("invocation_model_id"):
            aliases[candidate_id(cur)] = candidate_id(c)
            keep[key] = c
        else:
            aliases[candidate_id(c)] = candidate_id(cur)
    return [c for c in candidates if keep.get(route_learning.candidate_key(c)) is c], aliases


def _explore(rows: list[dict], request: dict, s: dict, seed: str) -> dict:
    ex = s["exploration"]
    out = {"active": False, "rate": ex["rate"], "picked": None}
    history = request.get("exploration_history") or []
    window = history[-20:]
    cap = int(20 * float(ex["max_percent_rolling_20"]) / 100.0)
    if sum(window) >= cap:
        out["blocked"] = f"rolling cap reached ({sum(window)}/{cap} of the last 20 decisions explored)"
        return out
    if int(request.get("run_explorations") or 0) >= int(ex["max_per_run"]):
        out["blocked"] = "this run already explored its allowance"
        return out
    primary = rows[0]
    eligible = [r for r in rows[1:]
                if r["local"]["n_effective"] < float(ex["min_samples_mature"])
                and r["utility"] >= primary["utility"] - float(ex["margin"])
                and (r["cost_to_success"] is None or primary["cost_to_success"] is None
                     or r["cost_to_success"] <= primary["cost_to_success"] * float(ex["max_cost_vs_primary_percent"]) / 100)]
    out["eligible"] = [r["route"] for r in eligible]
    if not eligible:
        return out
    draw = _draw(seed, "explore")
    out["draw"] = round(draw, 6)
    if draw < float(ex["rate"]):
        out.update({"active": True, "picked": eligible[0]["route"],
                    "rule": "under-tested qualifying route within the utility margin and cost bound"})
    return out


# ------------------------------------------------------------------ disclosure text

def _pct(x: float) -> str:
    return f"{100 * x:.0f}%"


def _strength(r: dict, rows: list[dict]) -> str:
    n = r["local"]["n_effective"]
    best_cost = min((x["cost_to_success"] for x in rows if x["cost_to_success"]), default=None)
    best_time = min((x["time_to_success_seconds"] for x in rows if x["time_to_success_seconds"]), default=None)
    if r["preference"]["seed_rank"] is not None and r["preference"]["seed_rank"] == 0:
        return "your preferred route"
    if n >= 6 and r["p_success"] >= 0.6:
        return f"strong local evidence ({_pct(r['p_success'])} success, n={n:.0f})"
    if best_cost and r["cost_to_success"] and r["cost_to_success"] <= best_cost * 1.05:
        return "lowest expected cost to success"
    if best_time and r["time_to_success_seconds"] and r["time_to_success_seconds"] <= best_time * 1.05:
        return "fastest expected completion"
    if r["quota"]["state"] != "unknown" and r["quota"]["score"] >= 0.75:
        return f"healthy quota ({r['quota']['state']})"
    if r["benchmark"]["authority"] > 0.5 and r["benchmark"]["prior_p"] >= 0.6:
        return "strong benchmark prior"
    if r["preference"]["seed_rank"] is not None:
        return f"preferred seed #{r['preference']['seed_rank'] + 1}"
    return f"{_pct(r['p_success'])} expected success"


def _weakness(r: dict, rows: list[dict]) -> str:
    n = r["local"]["n_effective"]
    best_cost = min((x["cost_to_success"] for x in rows if x["cost_to_success"]), default=None)
    if r["quota"]["state"] == "unknown":
        return "quota unknown"
    if r["quota"]["score"] < 0.6:
        return f"quota pressure ({r['quota']['state']})"
    if n < 3:
        return "little local evidence" + (f" (n={r['local']['n_raw']})" if r["local"]["n_raw"] else "")
    if best_cost and r["cost_to_success"] and r["cost_to_success"] > best_cost * 2:
        return f"{r['cost_to_success'] / best_cost:.1f}x the cheapest expected cost"
    if r["p_success"] < 0.5:
        return f"{_pct(r['p_success'])} expected success"
    if r["cost_basis"] == "unknown":
        return "no price data"
    return "no notable risk"


def _slate_entry(i: int, r: dict, rows: list[dict], clincher: dict, exploration: dict) -> dict:
    if i == 0 and exploration.get("picked") == r["route"]:
        reason = "exploration: under-tested, close to the best"
    elif i == 0 and clincher.get("used") and clincher.get("picked") == r["route"]:
        reason = "close call, won the seeded draw"
    elif i == 0:
        reason = "best success/cost/speed fit"
    else:
        # What this fallback offers over the primary, by weighted contribution.
        primary = rows[0]["contributions"]
        c = r["contributions"]
        edge = max(("effectiveness", "cost", "speed", "quota", "preference"), key=lambda k: c[k] - primary[k])
        if c[edge] - primary[edge] > 0.005:
            reason = {"effectiveness": "more likely to succeed than the primary",
                      "cost": "cheaper to success than the primary", "speed": "faster than the primary",
                      "quota": "more quota headroom than the primary",
                      "preference": "closer to your preference"}[edge]
        else:
            reason = "next best overall"
        if clincher.get("used") and r["route"] in clincher.get("band", []):
            reason += "; close call"
        elif r["gap_to_best"] > 0:
            reason += f" ({r['gap_to_best']:.3f} behind)"
    return {"rank": RANK_LABELS[i], "route": r["route"], "label": r["label"], "utility": r["utility"],
            "reason": reason, "strength": _strength(r, rows), "weakness": _weakness(r, rows)}


def render_slate(slate: list[dict], *, indent: str = "  ", notes: list[str] | None = None) -> list[str]:
    """The Inline Slate: primary and fallbacks, a reason each, one + and one -."""
    if not slate:
        return [f"{indent}ROUTING  no qualifying route"]
    width = max(len(e["label"]) for e in slate)
    lines = [f"{indent}ROUTING" + (f"  ({'; '.join(notes)})" if notes else "")]
    for e in slate:
        lines.append(f"{indent}{e['rank']:<11} {e['label']:<{width}}  {e['reason']}")
        lines.append(f"{indent}{'':<11} + {e['strength']}   - {e['weakness']}")
    return lines


# ------------------------------------------------------------------ planner choice

def resolve_route_ref(ref: str, rows: list[dict]) -> dict | None:
    """A planner's route reference (`harness/model@effort` or a full route id)."""
    ref = ref.strip()
    for r in rows:
        if ref in (r["route"], r["label"]):
            return r
    return None


def _rejected_route_ref(ref: str, rejected: list[dict]) -> dict | None:
    """Find a staged-out route by full identity or its planner-facing label."""
    for entry in rejected:
        candidate = str(entry.get("candidate") or "")
        parts = candidate.split("/", 1)
        label = f"{parts[0].split('@', 1)[0]}/{parts[1]}" if len(parts) == 2 else candidate
        if ref in (candidate, label):
            return entry
    return None


def apply_planner_choice(audit: dict, choice: dict | None) -> dict:
    """The planner's primary + fallbacks over the router's slate.

    `choice`: {"routes": [ref, ...], "why": str}. Any qualifying candidate may be
    named, not only the displayed slate. A primary that departs from the numeric
    best by more than the competitive band needs a concrete `why`; without it the
    router's slate stands and the result says why. Fallbacks the planner did not
    name are filled from the router's order."""
    rows = audit.get("candidates") or []
    router = [e["route"] for e in audit.get("slate") or []]
    out = {"chooser": "router", "primary": router[0] if router else None, "fallbacks": router[1:SLATE_SIZE]}
    if not choice or not choice.get("routes"):
        return out
    picked, rejected, unknown = [], [], []
    for ref in choice["routes"][:SLATE_SIZE]:
        r = resolve_route_ref(ref, rows)
        if r is None:
            reason = _rejected_route_ref(ref, audit.get("rejected") or [])
            if reason:
                rejected.append((ref, reason))
            else:
                unknown.append(ref)
        elif r["route"] not in picked:
            picked.append(r["route"])
    if rejected:
        details = "; ".join(f"{ref} rejected at stage {entry['stage']}: {entry['reason']}"
                             for ref, entry in rejected)
        return {**out, "planner_error": details}
    if unknown:
        return {**out, "planner_error": f"not a candidate: {', '.join(unknown)}"}
    if not picked:
        return out
    top_u = max((r["utility"] for r in rows), default=0.0)
    primary_row = next(r for r in rows if r["route"] == picked[0])
    gap = round(top_u - primary_row["utility"], 4)
    departs = gap > abs(top_u) * float(audit.get("competitive_band", 0.05))
    why = (choice.get("why") or "").strip()
    if departs and len(why.replace(" ", "")) < 10:
        return {**out, "planner_error": (f"{primary_row['label']} is {gap:.3f} utility behind the best route; "
                                         "a primary outside the close-call band needs route_why")}
    fallbacks = picked[1:] + [x for x in router if x not in picked]
    return {"chooser": "planner", "primary": picked[0], "fallbacks": fallbacks[:SLATE_SIZE - 1],
            "override": picked != router[:len(picked)],
            "departs_from_ranking": departs, "gap_to_best": gap, "why": why or None}


def slate_for(audit: dict, plan: dict) -> list[dict]:
    """Slate entries in the order a planner choice set."""
    rows = {r["route"]: r for r in audit.get("candidates") or []}
    order = [plan.get("primary"), *(plan.get("fallbacks") or [])]
    base = {e["route"]: e for e in audit.get("slate") or []}
    out = []
    for i, rid in enumerate(x for x in order if x and x in rows):
        r = rows[rid]
        entry = dict(base.get(rid) or _slate_entry(i, r, list(rows.values()), {}, {}))
        entry["rank"] = RANK_LABELS[i]
        if i == 0 and plan.get("chooser") == "planner" and plan.get("override"):
            entry["reason"] = "planner choice: " + (plan.get("why") or "within the close-call band")
        out.append(entry)
    return out
