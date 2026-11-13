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
            loss = _loss([(_prob(o["coding"], a, b), o["y"], o["learn_weight"]) for o in train])
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
            if len(sample) < 8 or len({o["y"] for o in sample}) < 2:
                weights[domain] = .6
                continue
            weights[domain] = min(DOMAIN_WEIGHTS,
                key=lambda weight: _loss([(_score_pred(o,link,weight), o["y"], o["learn_weight"]) for o in sample])
                   + lam*(weight-.6)**2)
        predictions = [(_score_pred(o,link,weights.get(o["domain"],.6)),o["y"],o["learn_weight"]) for o in valid]
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
        succ += weight * e["y"]
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
        scores.append((_loss([(x["p"],x["o"]["y"],x["o"]["learn_weight"]) for x in preds]),k))
    value,k = min(scores,key=lambda x:(x[0],x[1]))
    return {"pseudo_outcomes":k,"validation_logloss":round(value,6)}

TARGETS = ("first_pass_accepted", "eventual_accepted")


def _when(r):
    return r.get("label_at") or r.get("ended_at")


def observed_dataset(outcomes_path, evidence, *, include_unknown_size=False):
    """Executed attempts with exact same-route, same-effort v5 components.

    An attempt joins only the evidence row for its own resolved catalog route.
    Unresolved aliases, sibling efforts and unexecuted routes never join.
    """
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
        need = ("run_id","task_id","dispatch_ids","route","size_class","domain","role",
                "first_pass_accepted","eventual_accepted","attribution","learn_weight")
        if any(k not in r for k in need):
            raise ValueError(f"outcome line {line_number} lacks fields (use aa_v5_outcomes.py)")
        if not _when(r):
            reasons["no_label_time"] += 1
            continue
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
        _parse_date(_when(r))
        all_rows.append(r)
        ev = evidence.get(r["route"]) if r["route"] else None
        if r["route"] is None:
            reasons["route_unresolved_to_exact_catalog_identity"] += 1
            continue
        if ev is None:
            reasons["route_not_in_evidence"] += 1
            continue
        fc,tb = ev["frontier_code2_percent"],ev["terminal_bench4_percent"]
        if fc is None or tb is None:
            reasons["missing_same_effort_components"] += 1
            continue
        if r["learn_weight"] <= 0:
            reasons["operational_or_plan_not_model_attributable"] += 1
            continue
        if r["size_class"] not in OWNER_RUNGS and not include_unknown_size:
            reasons["unknown_size"] += 1
            continue
        obs.append({**r, "ended_at": _when(r), "fc":fc,"tb":tb,"coding":.6*fc+.4*tb})
    obs.sort(key=lambda o:(o["ended_at"],o["run_id"],o["task_id"],o.get("segment_index",0)))
    keys = [(o["run_id"],o["task_id"],o.get("segment_index",o["route"])) for o in obs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate task attempt; cannot assume independent evidence")
    return obs,all_rows,dict(reasons)


def chrono_split(observations, fractions=(.70, .85)):
    """Purged chronological train/validation/test split.

    Cut times sit at the label-time quantiles. A run whose labels fall on both
    sides of a cut is purged from the analysis rather than assigned, so no run
    contributes to two windows and every training label precedes every
    validation label, which precedes every test label.
    """
    if len({o["run_id"] for o in observations})<MIN_VALID_RUNS or len(observations)<MIN_VALID_EPISODES:
        raise InsufficientData("need >=6 attributed runs and >=30 exact-score attempts for train/val/test")
    times=sorted(o["ended_at"] for o in observations)
    cuts=[times[min(len(times)-1,int(f*len(times)))] for f in fractions]
    def window(t):
        return 0 if t<cuts[0] else 1 if t<cuts[1] else 2
    byrun=defaultdict(set)
    for o in observations:
        byrun[o["run_id"]].add(window(o["ended_at"]))
    purged={r for r,w in byrun.items() if len(w)>1}
    pieces=[[o for o in observations if o["run_id"] not in purged and window(o["ended_at"])==i] for i in range(3)]
    if not all(pieces):
        raise InsufficientData("purged chronological train/val/test holdout is empty")
    if not (pieces[0][-1]["ended_at"] < pieces[1][0]["ended_at"] and
            pieces[1][-1]["ended_at"] < pieces[2][0]["ended_at"]):
        raise ValueError("chronological time leakage")
    runs=[{o["run_id"] for o in p} for p in pieces]
    return pieces,{"train_runs":len(runs[0]),"validation_runs":len(runs[1]),"test_runs":len(runs[2]),
                   "train_n":len(pieces[0]),"validation_n":len(pieces[1]),"test_n":len(pieces[2]),
                   "purged_runs":len(purged),"purged_attempts":sum(o["run_id"] in purged for o in observations),
                   "time_bounds":[[p[0]["ended_at"],p[-1]["ended_at"]] for p in pieces],
                   "cuts":cuts,"straddling_runs_purged":True}


def metrics(pairs):
    if not pairs:
        return {"n":0}
    b=[brier(p,o["y"]) for o,p in pairs]
    l=[logloss(p,o["y"]) for o,p in pairs]
    mean=sum(o["y"] for o,p in pairs)/len(pairs)
    # ECE is a descriptive, fixed 5-bin measure; nonparametric CI by run.
    ece=0.
    for binid in range(5):
        g=[(o,p) for o,p in pairs if min(4,int(p*5))==binid]
        if g:
            ece += len(g)/len(pairs)*abs(sum(p for o,p in g)/len(g)-sum(o["y"] for o,p in g)/len(g))
    return {"n":len(pairs),"observed_rate":round(mean,4),
            "brier":round(statistics.mean(b),6),"logloss":round(statistics.mean(l),6),
            "ece_5bin":round(ece,6),"mean_predicted":round(statistics.mean(p for _,p in pairs),4)}


def cluster_uncertainty(pred, draws=1000):
    """Bootstrap whole test runs (not dependent attempts); paired Brier deltas."""
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
        sample=defaultdict(list)
        for run in (rng.choice(runs) for __ in runs):
            for key,o,p in grouped[run]:
                sample[key].append((o,p))
        means={k:sum(brier(p,o["y"]) for o,p in v)/len(v) for k,v in sample.items()}
        for k,v in means.items():
            results[k].append(v)
        for a,b in (("B","A"),("C","A"),("A","N"),("B","N"),("C","N")):
            if a in means and b in means:
                results[f"delta_{a}_minus_{b}"].append(means[a]-means[b])
    def ci(values):
        ordered=sorted(values)
        return [round(ordered[int(.025*(len(ordered)-1))],6),
                round(ordered[int(.975*(len(ordered)-1))],6)]
    return {"method":"run-cluster-bootstrap-1000-seed-862","test_runs":len(runs),
            "brier_95_ci":{k:ci(v) for k,v in sorted(results.items())}}


def _rate(rows, key):
    return round(sum(bool(r[key]) for r in rows)/len(rows),4) if rows else None


def _mean(values, digits=4):
    values=[v for v in values if isinstance(v,(int,float))]
    return round(statistics.mean(values),digits) if values else None


def aggregate(rows):
    """Sanitized factual outcome aggregate for a set of executed attempts."""
    n=len(rows)
    if not n:
        return {"n":0}
    accepted=[r for r in rows if r["eventual_accepted"]]
    costed=[r for r in rows if isinstance(r.get("estimated_cost_usd"),(int,float))]
    costed_ok=[r for r in accepted if isinstance(r.get("estimated_cost_usd"),(int,float))]
    known_ind=[r for r in accepted if r.get("independent_review_passed") is not None]
    usage=[r for r in rows if r.get("usage_limit_hit") is not None]
    return {
        "n":n,"first_pass_rate":_rate(rows,"first_pass_accepted"),"eventual_rate":_rate(rows,"eventual_accepted"),
        "recheck_mean":_mean([r.get("recheck_count") for r in rows],3),
        "recheck_any_rate":round(sum((r.get("recheck_count") or 0)>0 for r in rows)/n,4),
        "operational_failures_n":sum(bool(r.get("operational_failure")) for r in rows),
        "model_quality_failures_n":sum(bool(r.get("model_quality_failure")) for r in rows),
        "usage_limit_hits_n":sum(bool(r.get("usage_limit_hit")) for r in usage) if usage else None,
        "usage_limit_coverage":len(usage),
        "exec_minutes_mean":_mean([r["time_seconds"]/60 for r in rows if isinstance(r.get("time_seconds"),(int,float))],1),
        "estimated_cost_per_attempt_usd":_mean([r["estimated_cost_usd"] for r in costed],2),
        "estimated_cost_per_accepted_attempt_usd":_mean([r["estimated_cost_usd"] for r in costed_ok],2),
        "estimated_cost_coverage":len(costed),
        "money_actual_coverage":sum(isinstance(r.get("money_actual"),(int,float)) for r in rows),
        "quota_delta_coverage":sum(isinstance(r.get("quota_burn"),(int,float)) for r in rows),
        "independent_review_rate_of_accepted":_rate(known_ind,"independent_review_passed"),
        "independent_review_known_n":len(known_ind),
        "regression_rate":None,
    }


def operational_stats(all_rows):
    bysize=defaultdict(list)
    bydomain=defaultdict(list)
    for o in all_rows:
        bysize[o["size_class"]].append(o)
        bydomain[o["domain"]].append(o)
    return {"all":aggregate(all_rows),"by_size":{s:aggregate(bysize[s]) for s in ("XS","S","M","L","XL","unknown")},
            "by_domain":{d:aggregate(rows) for d,rows in sorted(bydomain.items())}}


def route_table(all_rows, evidence, min_n=3):
    """Per size x exact executed route; first-route and all-position views."""
    out=[]
    groups=defaultdict(list)
    for r in all_rows:
        if r.get("route"):
            groups[(r["size_class"],r["route"])].append(r)
    for (size,route),rows in sorted(groups.items()):
        if len(rows)<min_n:
            continue
        ev=evidence.get(route)
        out.append({"size":size,"route":route,"office_60_40_score":round(coding_score(ev),2) if ev and coding_score(ev) is not None else None,
                    "all_positions":aggregate(rows),
                    "first_route":aggregate([r for r in rows if r.get("first_route")])})
    return out


def _spearman(xs, ys):
    def ranks(v):
        order=sorted(range(len(v)),key=lambda i:v[i])
        r=[0.]*len(v)
        i=0
        while i<len(v):
            j=i
            while j+1<len(v) and v[order[j+1]]==v[order[i]]:
                j+=1
            for k in range(i,j+1):
                r[order[k]]=(i+j)/2
            i=j+1
        return r
    rx,ry=ranks(xs),ranks(ys)
    mx,my=statistics.mean(rx),statistics.mean(ry)
    num=sum((a-mx)*(b-my) for a,b in zip(rx,ry))
    den=math.sqrt(sum((a-mx)**2 for a in rx)*sum((b-my)**2 for b in ry))
    return round(num/den,3) if den else None


def benchmark_rank_agreement(table, min_n=5):
    """Does the 60/40 benchmark order exact routes the way observed outcomes do?"""
    out={}
    for size in ("XS","S","M","L","XL","unknown"):
        rows=[r for r in table if r["size"]==size and r["office_60_40_score"] is not None
              and r["all_positions"]["n"]>=min_n]
        if len(rows)<3:
            out[size]={"routes":len(rows),"spearman_first_pass":None,"spearman_eventual":None,
                       "reason":"fewer than three exact-score routes with >=5 attempts"}
            continue
        sc=[r["office_60_40_score"] for r in rows]
        out[size]={"routes":len(rows),
                   "spearman_first_pass":_spearman(sc,[r["all_positions"]["first_pass_rate"] for r in rows]),
                   "spearman_eventual":_spearman(sc,[r["all_positions"]["eventual_rate"] for r in rows])}
    return out


def owner_replay(all_rows):
    """#1143 soft preference rungs against FACTUAL attempts only.

    For each approved route: what actually happened when it ran at that size.
    A route that never ran at that size is 'unobserved', never a success.
    Also: how often the factual first route already was an approved rung-1 route.
    """
    out={}
    for size,rungs in OWNER_RUNGS.items():
        sized=[r for r in all_rows if r["size_class"]==size]
        firsts=[r for r in sized if r.get("first_route")]
        entries=[]
        for i,group in enumerate(rungs):
            for route in group:
                rows=[r for r in sized if r.get("route")==route]
                entries.append({"rung":i+1,"route":route,
                                "factual":aggregate(rows) if rows else "unobserved",
                                "factual_first_route":aggregate([r for r in rows if r.get("first_route")]) if rows else "unobserved"})
        out[size]={"rungs":entries,"tasks_with_first_route":len(firsts),
                   "first_route_was_approved_rung1":sum(r.get("route") in rungs[0] for r in firsts),
                   "counterfactual_outcomes":"unobserved; tasks that ran another route are not relabeled"}
    return out


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


def compare(obs, target):
    """A/B/C plus a no-benchmark base-rate yardstick N on identical held-out attempts."""
    data=[{**o,"y":bool(o[target])} for o in obs]
    (train,val,test),split=chrono_split(data)
    link=fit_link(train)
    B=fit_domain(train,val,link)
    C=choose_strength(train,val,link)
    base=sum(o["y"] for o in train+val)/len(train+val)
    cpreds=prequential(test,train+val,link,C["pseudo_outcomes"])
    pred={
        "N":[(o,max(.01,min(.99,base))) for o in test],
        "A":[(o,_score_pred(o,link)) for o in test],
        "B":[(o,_score_pred(o,link,B["domain_weights"].get(o["domain"],.6))) for o in test],
        "C":[(z["o"],z["p"]) for z in cpreds],
    }
    keys={k:[(o["run_id"],o["task_id"],o.get("segment_index",o["route"])) for o,p in v] for k,v in pred.items()}
    if not keys["N"]==keys["A"]==keys["B"]==keys["C"]:
        raise ValueError("evaluation identity mismatch")
    summary={k:metrics(v) for k,v in pred.items()}
    unc=cluster_uncertainty(pred)
    domains={o["domain"] for o in train}
    def coverage(rows):
        return {"size":dict(Counter(o["size_class"] for o in rows)),"route":dict(Counter(o["route"] for o in rows))}
    ranking="undetermined"
    if len(test)>=30 and split["test_runs"]>=3 and "brier_95_ci" in unc:
        best=min(("A","B","C"),key=lambda k:(summary[k]["brier"],k))
        ci=unc["brier_95_ci"]
        ranking={"lowest_brier_overall":min(("N","A","B","C"),key=lambda k:(summary[k]["brier"],k)),
                 "lowest_brier_benchmark_formula":best,
                 "benchmark_formula_beats_baseline_at_95":ci.get(f"delta_{best}_minus_N",[0,0])[1]<0,
                 "winner":best if ci.get(f"delta_{best}_minus_N",[0,0])[1]<0 else None,
                 "note":"a lower point Brier without a run-cluster CI excluding zero is not a winner"}
    return {"split":split,"formula_parameters":{"N":{"train_val_base_rate":round(base,4)},"A":link,"B":{**B,
                "degenerate":len(domains)<2,"degenerate_reason":"one domain recorded; B equals A" if len(domains)<2 else None},"C":C},
            "held_out":summary,"uncertainty":unc,"empirical_ranking":ranking,
            "coverage":{"train":coverage(train),"validation":coverage(val),"test":coverage(test)},
            "feature_note":"A and B use one benchmark score per exact route, so within a route every attempt gets the same prediction; they can only rank routes."}


def run(outcome_path,folder=aa_v5_evidence.FOLDER):
    verification=aa_v5_evidence.validate(folder)
    ev={r["route_key"]:r for r in aa_v5_evidence.rows(folder)}
    report={"analysis":"research-only-aa-v5.0.1-shadow","production_index":"unchanged-v4.3.2",
            "owner_policy":"#1143-unchanged","data_reconciliation":verification,
            "policy_floor_shadow":policy_coverage(ev),"formulae": {
                "N":"no-benchmark yardstick: train+validation base rate",
                "A":"60% FrontierCode 2.0 + 40% Terminal-Bench 4.0; TRAIN-calibrated link",
                "B":"domain-regularized FrontierCode/Terminal blend; fit TRAIN, hyperparameter VALIDATION",
                "C":"A as weak logistic benchmark prior; same exact route Beta evidence (size/domain discounted), prequential; tune k on VALIDATION",
            }}
    if outcome_path is None or not outcome_path.exists():
        report.update(status="blocked_no_raw_outcomes",reason="No private attempt file supplied. Run the local extraction first; no formula winner can be established without executed outcomes.",
                      next_command="python eval/aa_v5_outcomes.py --db /path/to/consistent/runs-copy.db --out /private/aa-v5-attempts.jsonl; python eval/aa_v5_calibrate.py --outcomes /private/aa-v5-attempts.jsonl --report /private/aa-v5-analysis.json")
        return report
    obs,all_rows,reasons=observed_dataset(outcome_path,ev)
    obs_all,_,reasons_all=observed_dataset(outcome_path,ev,include_unknown_size=True)
    report["data_coverage"]={"observed_attempts":len(all_rows),"exact_score_attributable_sized":len(obs),
                             "exact_score_attributable_any_size":len(obs_all),"excluded_reasons":reasons}
    report["operational_outcomes"]=operational_stats(all_rows)
    table=route_table(all_rows,ev)
    report["route_outcomes"]=table
    report["benchmark_rank_agreement"]=benchmark_rank_agreement(table)
    report["owner_replay_1143"]=owner_replay(all_rows)
    report["shadow_routing"]={"status":"factual-only","counterfactual_model_success":"unobserved",
        "no_changes_to_dispatch_or_policy":True}
    comparisons={}
    for scope,data in (("sized_XS_XL",obs),("any_size_sensitivity",obs_all)):
        for target in TARGETS:
            try:
                comparisons[f"{scope}:{target}"]=compare(data,target)
            except InsufficientData as exc:
                comparisons[f"{scope}:{target}"]={"status":"insufficient_chronological_holdout","reason":str(exc)}
    report["comparisons"]=comparisons
    report["status"]="calibrated_shadow_only" if any("held_out" in v for v in comparisons.values()) else "insufficient_chronological_holdout"
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
