#!/usr/bin/env python3
"""Shadow-only comparison of v5 coding predictors against attributable Office outcomes (#862).

A: 60/40 FC2/TB4 with a TRAIN-only probability link.
B: domain-specific coding weights shrunk toward 60/40, tuned on chronological VALIDATION.
C: weak benchmark prior (A link) + route/effort/size/domain first-pass outcomes,
   chronological one-episode-at-a-time Beta posterior; strength tuned on VALIDATION.

Only the route actually executed gets a label. Suggestions for unexecuted routes
are COUNTERFACTUAL UNOBSERVED, never scored as successful or failed.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
from datetime import datetime
import json
import math
from pathlib import Path
import random
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))
import aa_v5_evidence

# The currently approved #1143 ADAPTIVE SOFT SEEDS. Grouped equal-rung competitors.
# They are research references, not a claim that current main has shipped these.
OWNER_RUNGS = {
    "XS": [["claude/claude-haiku-5-5@medium"], ["codex/gpt-6.1-terra@low", "agy/gemini-4-hydrogen@low"]],
    "S": [["codex/gpt-6.1-terra@medium", "agy/gemini-4-hydrogen@medium"],
          ["claude/claude-sonnet-5-5@medium"]],
    "M": [["claude/claude-sonnet-5-5@medium", "agy/gemini-4-hydrogen@high"],
          ["codex/gpt-6.1-terra@high"]],
    "L": [["claude/claude-sonnet-5-5@high"], ["agy/gemini-4-hydrogen@high"]],
    "XL": [["claude/claude-opus-5-5@medium"],
           ["codex/gpt-6.1-sol@medium", "agy/gemini-4-argon@high"]],
}
LEGACY_SHADOW_FLOORS = {"XS": 8, "S": 17, "M": 22, "L": 33, "XL": 38}
LEGACY_SHADOW_SOFT_CEILINGS = {"XS": 20, "S": 28, "M": 36, "L": 44, "XL": None}
PRIOR_STRENGTHS = (1, 2, 4, 8, 12)
DOMAIN_WEIGHTS = (0.2, 0.4, 0.6, 0.8)
REGULARIZATIONS = (0.1, 1.0, 5.0)
MIN_VALID_RUNS = 6
MIN_VALID_EPISODES = 30

class InsufficientData(ValueError):
    pass

def coding_score(row, weight=.6):
    f, t = row.get("frontier_code2_percent"), row.get("terminal_bench4_percent")
    return weight * f + (1-weight) * t if f is not None and t is not None else None

def _prob(score, intercept, slope):
    return max(.01, min(.99, 1/(1+math.exp(-max(-60, min(60, intercept + slope * (score-30)/15))))))

def logloss(p, y):
    p = max(.001, min(.999, p))
    return -math.log(p) if y else -math.log(1-p)

def brier(p, y):
    return (p-float(y))**2

def _loss(predictions, field="logloss"):
    metric = logloss if field == "logloss" else brier
    return sum(metric(p, y) * w for p, y, w in predictions)/sum(w for _, _, w in predictions)

def fit_link(train):
    """Small, deterministic, regularized calibration grid, never same-run test fitted."""
    best = (float("inf"), 0., 0.)
    for a in (-2., -1.5, -1., -.5, 0., .5, 1., 1.5, 2.):
        for b in (0., .25, .5, 1., 1.5, 2., 3.):
            loss = _loss([(_prob(o["coding"], a, b), o["first_pass_accepted"], o["learn_weight"]) for o in train])
            loss += .005 * (a*a + b*b)
            if loss < best[0]:
                best = (loss, a, b)
    return {"intercept": best[1], "slope": best[2], "training_objective": round(best[0], 6)}

def _score_record(o, weight=.6):
    return weight*o["fc"] + (1-weight)*o["tb"]

def _score_pred(o, fit, weight=.6):
    return _prob(_score_record(o,weight), fit["intercept"], fit["slope"])

def fit_domain(train, valid, link):
    """Shrunk train-only domain weights. Choose REGULARIZATION on validation."""
    domains = sorted({o["domain"] for o in train})
    candidates = []
    for lam in REGULARIZATIONS:
        weights = {}
        for domain in domains:
            sample = [o for o in train if o["domain"] == domain]
            if len(sample) < 8 or len({o["first_pass_accepted"] for o in sample}) < 2:
                weights[domain] = .6
                continue
            weights[domain] = min(DOMAIN_WEIGHTS,
                key=lambda weight: _loss([(_score_pred(o,link,weight), o["first_pass_accepted"], o["learn_weight"]) for o in sample])
                   + lam*(weight-.6)**2)
        predictions = [(_score_pred(o,link,weights.get(o["domain"],.6)),o["first_pass_accepted"],o["learn_weight"]) for o in valid]
        candidates.append((_loss(predictions),lam,weights))
    val_loss, lam, weights = min(candidates,key=lambda x:(x[0],x[1]))
    return {"regularization": lam, "domain_weights": weights, "validation_logloss": round(val_loss,6)}

def _parse_date(s):
    return datetime.fromisoformat(s.replace("Z","+00:00"))

def posterior(o, past, link, k):
    # Same invocation route/harness/effort only; other sizes/domains contribute
    # at partial authority, and stale outcomes decay. Never borrow a sibling effort.
    p0 = _score_pred(o,link)
    succ, n = 0., 0.
    for e in past:
        if e["route"] != o["route"] or e["ended_at"] >= o["ended_at"]:
            continue
        weight = e["learn_weight"]
        if e["size_class"] != o["size_class"]:
            weight *= .75
        if e["domain"] != o["domain"]:
            weight *= .60
        days = max(0.,(_parse_date(o["ended_at"])-_parse_date(e["ended_at"])).total_seconds()/86400)
        weight *= .5**(days/120.)
        n += weight
        succ += weight * e["first_pass_accepted"]
    return (k*p0 + succ)/(k+n),n

def prequential(block, earlier, link, k):
    previous = list(earlier)
    pred = []
    for o in block:
        p,n = posterior(o, previous, link,k)
        pred.append({"o":o,"p":p,"local_n_before":n})
        previous.append(o)
    return pred

def choose_strength(train,valid,link):
    scores = []
    for k in PRIOR_STRENGTHS:
        preds = prequential(valid,train,link,k)
        scores.append((_loss([(x["p"],x["o"]["first_pass_accepted"],x["o"]["learn_weight"]) for x in preds]),k))
    value,k = min(scores,key=lambda x:(x[0],x[1]))
    return {"pseudo_outcomes":k,"validation_logloss":round(value,6)}

def observed_dataset(outcomes_path, evidence):
    obs = []
    reasons = Counter()
    all_rows = []
    for line_number,line in enumerate(outcomes_path.read_text().splitlines(),1):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError as exc:
            raise ValueError(f"outcome line {line_number}: {exc}") from exc
        need = ("run_id","task_id","dispatch_ids","route","ended_at","size_class","domain","role",
                "first_pass_accepted","eventual_accepted","attribution","learn_weight")
        if any(k not in r for k in need):
            raise ValueError(f"outcome line {line_number} lacks fields (use aa_v5_outcomes.py)")
        if type(r["first_pass_accepted"]) is not bool or type(r["eventual_accepted"]) is not bool:
            raise ValueError("acceptance fields must be actual boolean observations")
        if r["first_pass_accepted"] and not r["eventual_accepted"]:
            raise ValueError("first-pass cannot pass if eventual did not pass")
        if not isinstance(r["dispatch_ids"],list) or not r["dispatch_ids"] or not r["run_id"] or not r["task_id"]:
            raise ValueError("actual attributable run/task/dispatch IDs mandatory")
        if r["role"] not in ("executor","worker"):
            reasons["not_executor_worker"] += 1
            continue
        if not isinstance(r["learn_weight"],(float,int)) or not 0 <= r["learn_weight"] <= 1:
            raise ValueError("invalid attribution weight")
        if not r["ended_at"]:
            raise ValueError("missing chronological timestamp")
        _parse_date(r["ended_at"])
        all_rows.append(r)
        ev = evidence.get(r["route"])
        if ev is None:
            reasons["route_not_in_evidence"] += 1
            continue
        fc,tb = ev["frontier_code2_percent"],ev["terminal_bench4_percent"]
        if fc is None or tb is None:
            reasons["missing_same_effort_components"] += 1
            continue
        if r["learn_weight"] <= 0:
            reasons["operational_not_model_attributable"] += 1
            continue
        if r["size_class"] not in OWNER_RUNGS:
            reasons["unknown_size"] += 1
            continue
        d = {**r, "fc":fc,"tb":tb,"coding":.6*fc+.4*tb}
        obs.append(d)
    obs.sort(key=lambda o:(o["ended_at"],o["run_id"],o["task_id"],o["route"]))
    # Each dispatch counted once, no fabricated comparison labels for other routes.
    triples = [((o["run_id"],o["task_id"],o["route"])) for o in obs]
    if len(triples) != len(set(triples)):
        raise ValueError("duplicate task-route episode; cannot assume independent evidence")
    return obs,all_rows,dict(reasons)

def chrono_split(observations):
    """Non-overlapping chronological holdouts: no run straddles a boundary.

    Runs with overlapping episode windows form indivisible time clusters, so
    even a slow train-run finishing after a validation-run began cannot leak.
    """
    byrun=defaultdict(list)
    for o in observations:
        byrun[o["run_id"]].append(o)
    intervals=sorted(((min(x["ended_at"] for x in rows),
                       max(x["ended_at"] for x in rows),run)
                      for run,rows in byrun.items()), key=lambda r:(r[0],r[1],r[2]))
    if len(intervals)<MIN_VALID_RUNS or len(observations)<MIN_VALID_EPISODES:
        raise InsufficientData("need >=6 attributed runs and >=30 exact-score episodes for train/val/test")
    clusters=[]
    for start,end,run in intervals:
        if clusters and start <= clusters[-1]["end"]:
            clusters[-1]["runs"].add(run)
            clusters[-1]["end"]=max(end,clusters[-1]["end"])
        else:
            clusters.append({"start":start,"end":end,"runs":{run}})
    if len(clusters)<3:
        raise InsufficientData("runs overlap into fewer than three non-leaking chronological windows")
    # Target chronological sample fractions with intact run/time clusters.
    total=sum(len(byrun[r]) for r in byrun)
    cumulative=[]
    n=0
    for cl in clusters:
        n+=sum(len(byrun[r]) for r in cl["runs"])
        cumulative.append(n)
    cut1=min(range(1,len(clusters)-1),key=lambda i:abs(cumulative[i-1]/total-.70))
    cut2=min(range(cut1+1,len(clusters)),key=lambda i:abs(cumulative[i-1]/total-.85))
    groups=[clusters[:cut1],clusters[cut1:cut2],clusters[cut2:]]
    sets=[set().union(*(c["runs"] for c in group)) for group in groups]
    pieces=[[o for o in observations if o["run_id"] in keys] for keys in sets]
    if not all(pieces):
        raise InsufficientData("chronological train/val/test holdout is empty")
    if not (pieces[0][-1]["ended_at"] < pieces[1][0]["ended_at"] and
            pieces[1][-1]["ended_at"] < pieces[2][0]["ended_at"]):
        raise ValueError("chronological time leakage")
    return pieces,{"train_runs":len(sets[0]),"validation_runs":len(sets[1]),"test_runs":len(sets[2]),
                   "train_n":len(pieces[0]),"validation_n":len(pieces[1]),"test_n":len(pieces[2]),
                   "time_bounds":[[p[0]["ended_at"],p[-1]["ended_at"]] for p in pieces],
                   "time_overlap_purged_by_run_cluster":True}

def metrics(pairs):
    if not pairs:
        return {"n":0}
    b=[brier(p,o["first_pass_accepted"]) for o,p in pairs]
    l=[logloss(p,o["first_pass_accepted"]) for o,p in pairs]
    mean=sum(o["first_pass_accepted"] for o,p in pairs)/len(pairs)
    # ECE is a descriptive, fixed 5-bin measure; nonparametric CI by run.
    ece=0.
    for binid in range(5):
        g=[(o,p) for o,p in pairs if min(4,int(p*5))==binid]
        if g:
            ece += len(g)/len(pairs)*abs(sum(p for o,p in g)/len(g)-sum(o["first_pass_accepted"] for o,p in g)/len(g))
    return {"n":len(pairs),"first_pass_rate":round(mean,4),
            "brier":round(statistics.mean(b),6),"logloss":round(statistics.mean(l),6),
            "ece_5bin":round(ece,6),"mean_predicted":round(statistics.mean(p for _,p in pairs),4)}

def cluster_uncertainty(pred, draws=1000):
    """Bootstrap whole runs (not dependent attempts) on the same held-out IDs."""
    grouped=defaultdict(list)
    for key,pairs in pred.items():
        for o,p in pairs:
            grouped[o["run_id"]].append((key,o,p))
    runs=sorted(grouped)
    if len(runs)<3:
        return {"status":"insufficient_test_runs_for_cluster_ci","test_runs":len(runs)}
    rng=random.Random(862)
    results=defaultdict(list)
    for _ in range(draws):
        grouped_sample=defaultdict(list)
        for run in (rng.choice(runs) for __ in runs):
            for key,o,p in grouped[run]:
                grouped_sample[key].append((o,p))
        for key,items in grouped_sample.items():
            results[key].append(sum(brier(p,o["first_pass_accepted"]) for o,p in items)/len(items))
        if "A" in grouped_sample and "C" in grouped_sample:
            results["delta_C_minus_A"].append(results["C"][-1]-results["A"][-1])
    def ci(values):
        ordered=sorted(values)
        return [round(ordered[int(.025*(len(ordered)-1))],6),
                round(ordered[int(.975*(len(ordered)-1))],6)]
    return {"method":"run-cluster-bootstrap-1000","brier_95_ci":{k:ci(v) for k,v in results.items()}}

def operational_stats(all_rows):
    bysize=defaultdict(list)
    bydomain=defaultdict(list)
    for o in all_rows:
        bysize[o["size_class"]].append(o)
        bydomain[o["domain"]].append(o)
    def aggregate(rows):
        n=len(rows)
        if not n:
            return {"n":0}
        accepted=[r for r in rows if r["eventual_accepted"]]
        money=[r["money_actual"] for r in rows if isinstance(r.get("money_actual"),(float,int))]
        costed_accepted=[r for r in accepted if isinstance(r.get("money_actual"),(float,int))]
        time=[r["time_seconds"] for r in accepted if isinstance(r.get("time_seconds"),(float,int))]
        quotas=[r["quota_burn"] for r in rows if isinstance(r.get("quota_burn"),(float,int))]
        return {
            "n":n,"first_pass_n":sum(r["first_pass_accepted"] for r in rows),
            "eventually_accepted_n":len(accepted),
            "recheck_episodes_n":sum((r.get("recheck_count") or 0)>0 for r in rows),
            "review_rounds_total":sum((r.get("recheck_count") or 0) for r in rows),
            "operational_failures_n":sum(bool(r.get("operational_failure")) for r in rows),
            "model_quality_failures_n":sum(bool(r.get("model_quality_failure")) for r in rows),
            "time_to_accept_seconds_mean":round(statistics.mean(time),1) if time else None,
            "accepted_time_coverage":len(time),
            "observed_cost_per_accepted_usd":round(sum(r["money_actual"] for r in costed_accepted)/len(costed_accepted),4) if costed_accepted else None,
            "accepted_cost_coverage":len(costed_accepted),
            "cost_coverage_all":len(money),
            "quota_delta_total":sum(quotas) if quotas else None,
            "quota_delta_coverage":len(quotas),
            "regression_verified_n":sum(r.get("regression_observed") is not None for r in rows),
            "independent_review_verified_n":sum(r.get("independent_review_passed") is not None for r in rows),
        }
    return {"all":aggregate(all_rows),"by_size":{s:aggregate(bysize[s]) for s in ("XS","S","M","L","XL","unknown")},
            "by_domain":{d:aggregate(rows) for d,rows in sorted(bydomain.items())}}

def policy_coverage(evidence):
    out={}
    for size,rungs in OWNER_RUNGS.items():
        entries=[]
        scores=[]
        for i,group in enumerate(rungs):
            for route in group:
                ev=evidence.get(route)
                sc=coding_score(ev) if ev else None
                if sc is not None:
                    scores.append(sc)
                entries.append({"rung":i+1,"route":route,"office_60_40_score":round(sc,2) if sc is not None else None,
                                "measured_source": bool(ev and ev.get("confidence") == "official-public-page-exact-effort"),
                                "production_eligibility":"not evaluated; apply trust, conformance, v4 and #1143 gates",
                                "old_shadow_floor_pass":sc >= LEGACY_SHADOW_FLOORS[size] if sc is not None else None})
        # Coverage-only floor hypothesis can never make a dispatchable candidate.
        suggested=math.floor(min(scores)) if scores and len(scores)==len(entries) else None
        out[size]={"approved_soft_rungs":entries,"old_shadow_floor":LEGACY_SHADOW_FLOORS[size],
                   "old_soft_ceiling":LEGACY_SHADOW_SOFT_CEILINGS[size],
                   "coverage_only_floor_ceiling":suggested,
                   "blocked_by_old_floor":[x["route"] for x in entries if x["old_shadow_floor_pass"] is False]}
    return out

def run(outcome_path,folder=aa_v5_evidence.FOLDER):
    verification=aa_v5_evidence.validate(folder)
    ev={r["route_key"]:r for r in aa_v5_evidence.rows(folder)}
    report={"analysis":"research-only-aa-v5.0.1-shadow","production_index":"unchanged-v4.3.2",
            "owner_policy":"#1143-unchanged","data_reconciliation":verification,
            "policy_floor_shadow":policy_coverage(ev),"formulae": {
                "A":"60% FrontierCode 2.0 + 40% Terminal-Bench 4.0; TRAIN-calibrated link",
                "B":"domain-regularized FrontierCode/Terminal blend; fit TRAIN, hyperparameter VALIDATION",
                "C":"A as weak logistic benchmark prior; route+effort+size+domain Beta evidence, chronological; tune k on VALIDATION",
            }}
    if outcome_path is None or not outcome_path.exists():
        report.update(status="blocked_no_raw_outcomes",reason="Nov 14 empirical routing report is SECONDARY only. Current local runs.db and rico-mode/state/executor-scoreboard.jsonl are inaccessible from this environment. No attributed local labels; no formula winner can be established.",
                      next_command="python eval/aa_v5_outcomes.py --db /path/to/consistent/runs-copy.db --out /private/aa-v5-episodes.jsonl; python eval/aa_v5_calibrate.py --outcomes /private/aa-v5-episodes.jsonl --report /private/aa-v5-analysis.json")
        return report
    obs,all_rows,reasons=observed_dataset(outcome_path,ev)
    report["data_coverage"]={"observed_episodes":len(all_rows),"exact_score_attributable_episodes":len(obs),
                             "excluded_reasons":reasons}
    report["operational_outcomes"]=operational_stats(all_rows)
    report["shadow_routing"]={"status":"factual-only","counterfactual_model_success":"unobserved",
        "preferred_factual_episodes":sum(o["route"] in sum(OWNER_RUNGS.get(o["size_class"],[]),[]) for o in obs),
        "observed_candidate_episodes":len(obs),
        "no_changes_to_dispatch_or_policy":True}
    try:
        (train,val,test),split=chrono_split(obs)
    except InsufficientData as exc:
        report.update(status="insufficient_chronological_holdout",reason=str(exc))
        return report
    link=fit_link(train)
    B=fit_domain(train,val,link)
    C=choose_strength(train,val,link)
    cpreds=prequential(test,train+val,link,C["pseudo_outcomes"])
    pred={
        "A":[(o,_score_pred(o,link)) for o in test],
        "B":[(o,_score_pred(o,link,B["domain_weights"].get(o["domain"],.6))) for o in test],
        "C":[(z["o"],z["p"]) for z in cpreds],
    }
    # Same exact executed test episodes for all three. No invented alternative-route labels.
    testkeys={k: [(o["run_id"],o["task_id"],o["route"]) for o,p in v] for k,v in pred.items()}
    if not testkeys["A"]==testkeys["B"]==testkeys["C"]:
        raise ValueError("evaluation identity mismatch")
    summary={k:metrics(v) for k,v in pred.items()}
    report.update(status="calibrated_shadow_only", split=split, formula_parameters={"A":link,"B":B,"C":C},
                  held_out_first_pass=summary, uncertainty=cluster_uncertainty(pred),
                  empirical_ranking="undetermined" if len(test)<30 or split["test_runs"]<3 else
                    min(summary,key=lambda k:summary[k]["brier"]))
    # Winning a score is not a production cutover authorization.
    report["approval_required"]="owner signoff on formula/index/floors/reviewer isolation plus representative real outcomes"
    return report

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--outcomes",type=Path)
    p.add_argument("--folder",type=Path,default=aa_v5_evidence.FOLDER)
    p.add_argument("--report",type=Path)
    args=p.parse_args()
    result=run(args.outcomes,args.folder)
    serialized=json.dumps(result,indent=2,sort_keys=True)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(serialized+"\n")
    print(serialized)

if __name__=="__main__":
    main()
