"""Replay and calibration for adaptive executor routing (#300).

    python eval/routing_replay.py                 # synthetic scenarios only
    python eval/routing_replay.py --runs-db PATH  # plus read-only replay of local history

The synthetic part demonstrates each routing claim on fixed fixtures. The
history part opens runs.db read-only and prints only aggregates: a walk-forward
calibration of the learner against the benchmark prior alone (each episode is
predicted from outcomes that ended before it), and how many distinct primaries
the legacy balanced band and the adaptive policy pick over the same decision
points. No route-level private record leaves the machine.
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from office import adaptive, route_learning, routing  # noqa: E402

IDX = "Artificial Analysis Intelligence Index v4.3.2"


def cand(harness, model, effort="medium", *, out, inp, idx, tps=120.0, quota=None, caps=("builder",)):
    return {"harness": harness, "harness_version": "2", "model_id": model, "invocation_model_id": model,
            "invocation_source": "documented:fixture", "effort": effort, "benchmark_indexes": {IDX: idx},
            "capabilities": list(caps), "price_fields": {"output_per_mtok": out, "input_per_mtok": inp},
            "speed_fields": {"output_tok_per_s": tps, "ttft_ms": 3000}, "cost": {"money_estimate": out},
            "quota": ({"status": "ok", "tightest_remaining_percent": quota} if quota is not None
                      else {"status": "unknown", "tightest_remaining_percent": None})}


FIXTURE = [  # the #300 shape: one very cheap route, stronger but pricier ones
    cand("codex", "gpt-6-luna", "xhigh", out=0.5, inp=0.1, idx=34, tps=152),
    cand("codex", "gpt-6-luna", "high", out=0.5, inp=0.1, idx=32, tps=145),
    cand("agy", "gemini-3.8-flash", "medium", out=3.75, inp=0.75, idx=40, tps=300),
    cand("claude", "claude-sonnet-5-5", "high", out=10, inp=2, idx=47, tps=80),
    cand("claude", "claude-opus-5-5", "low", out=20, inp=4, idx=42, tps=80),
]


def evidence(**routes):
    return {"as_of": "2026-10-05T00:00:00+00:00", "pooling": {"prior_strength": 8}, "context": {},
            "routes": {k: {"n_effective": s + f, "n_raw": s + f, "runs": 5, "successes": s, "failures": f,
                           "review_rounds_mean": 0.5, "attempts_mean": a, "wall_seconds_median": None,
                           "money_actual_median": None} for k, (s, f, a) in routes.items()}}


def request(cands, *, ev=None, seed="replay", cfg=None, **extra):
    return {"role": "worker", "playbook": "Change", "candidates": cands, "policy": {"cost_policy": "balanced"},
            "evidence": ev or {"routes": {}, "pooling": {"prior_strength": 8}}, "routing_seed": seed,
            "adaptive_config": {"exploration": {"rate": 0.0}, **(cfg or {})}, **extra}


def show(title, d):
    print(f"\n== {title}")
    if d.get("routing"):
        for line in adaptive.render_slate(d["slate"]):
            print(line)
    else:
        print(f"  selected {d.get('selected')}")
    for r in d.get("rejected", [])[:6]:
        print(f"  rejected {r['candidate']} stage {r['stage']}: {r['reason']}")


def synthetic() -> None:
    legacy = routing.route({**request(FIXTURE), "adaptive": False})
    show("1. legacy balanced 20% band (pre-#300) on the #300 fixture", legacy)
    show("2. adaptive slate, same fixture, cold start", routing.route(request(FIXTURE)))

    a, b = cand("x", "smart", out=2, inp=0.4, idx=56), cand("y", "plain", out=2, inp=0.4, idx=36)
    show("3. benchmark-only cold start", routing.route(request([a, b], cfg={"competitive_band": 0})))
    ev = evidence(**{"x/smart@medium": (5, 25, 2.0), "y/plain@medium": (27, 3, 1.1)})
    show("4. local evidence takes over (smart 5/30, plain 27/30)",
         routing.route(request([a, b], ev=ev, cfg={"competitive_band": 0})))

    p, q = cand("x", "m1", out=2, inp=0.4, idx=45), cand("y", "m2", out=2, inp=0.4, idx=45)
    picks = Counter(routing.route(request([p, q], seed=f"s{i}"))["slate"][0]["label"] for i in range(100))
    repeat = {routing.route(request([p, q], seed="fixed"))["decision_hash"] for _ in range(5)}
    weak = cand("z", "weak", out=2, inp=0.4, idx=25)
    weak_wins = sum(routing.route(request([p, weak], seed=f"s{i}"))["slate"][0]["label"] == "z/weak@medium"
                    for i in range(200))
    print("\n== 5. close-call draw")
    print(f"  equal routes over 100 seeds: {dict(picks)}; one seed, 5 runs -> {len(repeat)} distinct decision hash")
    print(f"  clearly weaker route won {weak_wins}/200 seeded draws")

    print("\n== 6. quota-forced fallback")
    print("  covered end to end by tests/v31/test_adaptive_dispatch.py::"
          "test_fresh_quota_forces_the_recorded_fallback_and_says_why (dispatch re-qualifies the planned slate "
          "with fresh quota and takes fallback 1, recording why)")

    print("\n== 7. environment- vs route-attributed failure")
    common = dict(label=None, task_status="accepted", run_phase="closed", outcome=None, attribution=None, stall=None,
                  rounds=0, checks_failed=0, gate_verdicts=[])
    env = route_learning.attribute_failure(**common, term="signal", exit_code=None, has_revision=False, findings=[])
    code = route_learning.attribute_failure(**common, term="success", exit_code=0, has_revision=True,
                                            findings=[("code_review", "material", "resolved")])
    for name, (attr, conf, prov) in (("session killed", env), ("code-review defect", code)):
        w = route_learning.ATTRIBUTION_WEIGHT[attr] * (0.5 + 0.5 * conf)
        print(f"  {name}: {attr} (confidence {conf}, {prov}) -> learning weight {w:.2f}")

    print("\n== 8. automatic eligibility only after maturity and replay")
    def eps(outcomes, runs=4):
        return [{"run_id": f"R{i % runs}", "task_id": f"T{i}", "route": "r", "role": "executor", "success": ok,
                 "attribution": "route", "ended_at": f"2026-09-{10 + i:02d}"} for i, ok in enumerate(outcomes)]
    for label, sample in (("4 failures", eps([False] * 4)), ("15 failures, 4 runs", eps([True] + [False] * 14)),
                          ("15 failures, 1 run", eps([False] * 15, runs=1)),
                          ("18 successes then 4 failures", eps([True] * 18 + [False] * 4))):
        t = route_learning.eligibility_transitions(sample, {"r": {"prior_p": 0.5}}, {})
        print(f"  {label}: " + (f"{t[0]['state']} (replay: {t[0]['replay']['reason']})" if t else "no change"))


def history(db_path: str) -> None:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    outcomes = route_learning.derive_outcomes(con)
    eps = route_learning.episodes(outcomes)
    print(f"\n== history: {len(outcomes)} dispatch outcomes, {len(eps)} task-route episodes "
          f"({sum(e['success'] for e in eps)} landed)")
    attr = Counter(e["attribution"] for e in eps if not e["success"])
    print(f"  failed-episode attribution: {dict(attr)}")

    # Walk-forward calibration: learner posterior vs benchmark prior alone.
    from office import candidates, config
    cfg = config.resolve(None)[0]
    s = adaptive.settings(cfg)
    cands, _ = candidates.build_candidates(con, "executor", probe=False)
    prior = {route_learning.candidate_key(c): adaptive.benchmark_prior(c, s)[0] for c in cands}
    loss = {"prior": [], "learned": []}
    buckets: dict[str, list] = {}
    for i, e in enumerate(eps):
        if e["route"] not in prior or e["learn_weight"] == 0:
            continue
        before = [o for o in outcomes if (o.get("ended_at") or "") < (e.get("ended_at") or "")]
        c = next(x for x in cands if route_learning.candidate_key(x) == e["route"])
        st = route_learning.evidence_for(before, [c], {"playbook": e.get("playbook"), "size_class": e.get("size_class")},
                                         as_of=e.get("ended_at"))["routes"][e["route"]]
        p0 = prior[e["route"]]
        p, _, _ = route_learning.beta_bounds(st["successes"], st["failures"], p0, 8.0)
        y = 1.0 if e["success"] else 0.0
        for name, pr in (("prior", p0), ("learned", p)):
            pr = min(max(pr, 1e-3), 1 - 1e-3)
            loss[name].append(-(y * math.log(pr) + (1 - y) * math.log(1 - pr)))
        n = st["n_effective"]
        bucket = "n<3" if n < 3 else "3<=n<15" if n < 15 else "n>=15"
        buckets.setdefault(bucket, []).append((p0, p, y))
    if loss["prior"]:
        k = len(loss["prior"])
        print(f"  walk-forward log loss over {k} episodes: benchmark prior {sum(loss['prior']) / k:.3f}, "
              f"learner {sum(loss['learned']) / k:.3f} (lower is better)")
        for b in ("n<3", "3<=n<15", "n>=15"):
            rows = buckets.get(b) or []
            if rows:
                brier0 = sum((p0 - y) ** 2 for p0, _, y in rows) / len(rows)
                brier1 = sum((p - y) ** 2 for _, p, y in rows) / len(rows)
                print(f"    {b:<9} {len(rows):>4} episodes: Brier prior {brier0:.3f}, learner {brier1:.3f}")

    # Policy comparison at each decision point (every 5th episode start).
    legacy, adaptive_picks = Counter(), Counter()
    for e in eps[::5]:
        before = [o for o in outcomes if (o.get("ended_at") or "") < (e.get("ended_at") or "")]
        ev = route_learning.evidence_for(before, cands, {"playbook": e.get("playbook")}, as_of=e.get("ended_at"))
        req = {"role": "worker", "playbook": e.get("playbook"), "candidates": cands, "policy": {"cost_policy": "balanced"},
               "evidence": ev, "routing_seed": f"{e['run_id']}:{e['task_id']}",
               "adaptive_config": cfg.get("routing", {}).get("adaptive", {})}
        legacy[routing.route({**req, "adaptive": False}).get("selected")] += 1
        adaptive_picks[routing.route(req).get("selected")] += 1
    total = sum(legacy.values())
    if not total:
        print("  no settled episodes to replay as decision points")
        return
    print(f"  {total} decision points: legacy band picked {len(legacy)} distinct primaries "
          f"(top share {max(legacy.values()) / total:.0%}); adaptive picked {len(adaptive_picks)} "
          f"(top share {max(adaptive_picks.values()) / total:.0%})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs-db", help="replay this runs.db read-only (aggregates only)")
    args = ap.parse_args()
    synthetic()
    if args.runs_db:
        history(args.runs_db)


if __name__ == "__main__":
    main()
