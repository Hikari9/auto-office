"""Harness preference weights (`routing.harness_preference`).

The same model can launch through more than one harness (claude and hermes both run Sonnet). Weights
say how often each harness should win among the routes already qualified for one model and effort:

    routing:
      harness_preference:
        anthropic: {claude: 95, hermes: 5}      # a vendor (the lineage provider), or
        claude-sonnet-5-5: {claude: 50, hermes: 50}   # one model, which beats its vendor

The pick is a pure function of the pinned weights, the run id, the task id and the model, so one
(run, task) always lands on the same harness. It only chooses among candidates that survived every
gate (role deny, quota, cooldown, demotion, missing auth, not installed): a harness weighted 0, or
not listed, is never drawn first but remains the fallback when no weighted harness is left. With no
key set nothing here runs and routing output is unchanged.
"""
from __future__ import annotations

import hashlib
import math

KEY = "harness_preference"


def settings(config: dict | None) -> dict:
    """The pinned `routing.harness_preference` mapping, empty when unset."""
    raw = ((config or {}).get("routing") or {}).get(KEY)
    return raw if isinstance(raw, dict) else {}


def validate(config: dict) -> list[str]:
    raw = ((config or {}).get("routing") or {}).get(KEY)
    if raw is None:
        return []
    if not isinstance(raw, dict):
        return [f"routing.{KEY} must be a mapping of vendor or model to {{harness: weight}}"]
    from office import adapters
    known = adapters.harness_ids()
    problems = []
    for scope, weights in raw.items():
        where = f"routing.{KEY}.{scope}"
        if not isinstance(weights, dict):
            problems.append(f"{where} must be a mapping of harness to weight")
            continue
        for harness, weight in weights.items():
            if harness not in known:
                problems.append(f"{where}: unknown harness {harness!r} (known: {', '.join(known)})")
            elif isinstance(weight, bool) or not isinstance(weight, (int, float)) or not math.isfinite(weight):
                problems.append(f"{where}.{harness} must be a number, got {weight!r}")
            elif weight < 0:
                problems.append(f"{where}.{harness} must not be negative, got {weight!r}")
    return problems


def _model(c: dict) -> str | None:
    return c.get("alias_resolved_to") or c.get("model_id")


def scope_for(prefs: dict | None, c: dict) -> tuple[str, dict] | None:
    """The weights that apply to a candidate and the key that supplied them: its model, else its vendor."""
    if not prefs:
        return None
    lineage = c.get("lineage_key") or ""
    for key in (_model(c), c.get("model_id"), lineage, lineage.split("/", 1)[0]):
        if key and isinstance(prefs.get(key), dict):
            return key, prefs[key]
    return None


def _draw(run_id: str | None, task_id: str | None, scope: str, model: str | None) -> float:
    digest = hashlib.sha256(f"{KEY}|{run_id}|{task_id}|{scope}|{model}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2 ** 64


def decide(request: dict, ranked: list[dict]) -> dict | None:
    """Pick the harness for `ranked[0]`'s model and effort from the candidates in `ranked` (best first).

    Returns None when no weights apply. Otherwise a record: scope, weights, the draw, `pick` (the harness)
    and `why`; `route` is the winning candidate, the best-ranked one of that harness.
    """
    best = ranked[0]
    found = scope_for(request.get("harness_preference"), best)
    if not found:
        return None
    scope, weights = found
    same = [c for c in ranked if _model(c) == _model(best) and c.get("effort") == best.get("effort")]
    by_harness: dict[str, dict] = {}
    for c in same:
        by_harness.setdefault(c.get("harness") or "", c)
    live = {h: float(weights[h]) for h in sorted(by_harness) if float(weights.get(h, 0)) > 0}
    record = {"scope": scope, "weights": dict(weights), "model": _model(best), "effort": best.get("effort"),
              "available": sorted(by_harness)}
    if not live:
        return {**record, "pick": best.get("harness"), "draw": None, "route": best,
                "why": "no weighted harness is available for this model; kept the best-ranked route"}
    draw = _draw(request.get("run_id"), request.get("task_id"), scope, _model(best))
    total = sum(live.values())
    point, acc, pick = draw * total, 0.0, None
    for harness, w in live.items():
        acc += w
        if point < acc:
            pick = harness
            break
    pick = pick or next(reversed(live))
    share = f"{live[pick] / total:.0%}"
    return {**record, "pick": pick, "draw": round(draw, 6), "route": by_harness[pick],
            "why": f"weighted draw {draw:.4f} over {', '.join(f'{h}={w:g}' for h, w in live.items())} "
                   f"picked {pick} ({share} of the available weight)"}


def public(record: dict) -> dict:
    """The record without its candidate, for an audit or disclosure."""
    return {k: v for k, v in record.items() if k != "route"}
