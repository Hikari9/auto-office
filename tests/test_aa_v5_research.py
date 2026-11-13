"""#862: AA v5.0.1 research-only evidence, observable replay and no cutover."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
import statistics

import pytest

from eval import aa_v5_evidence as evidence
from eval import aa_v5_calibrate as calibration
from eval import aa_v5_outcomes as extractor


def _obs():
    assert evidence.validate()["verified_model_effort"] >= 20
    return {r["route_key"]:r for r in evidence.rows()}


def test_research_dataset_has_source_provenance_and_exact_missing_reasons():
    data = evidence.validate()
    assert data["rows"] == 26
    assert data["verified_model_effort"] == 25
    obs = _obs()
    for key,row in obs.items():
        assert row["index_version"] == evidence.EDITION
        if row["confidence"] == "official-public-page-exact-effort":
            assert row["source_html_sha256"] and row["fetched_at"]
            assert row["page_effort"] == key.rsplit("@",1)[-1]
            assert row["source_url"].startswith("https://artificialanalysis.ai/models/")
        for field in evidence.NUMERIC:
            assert row.get(field) is not None or row["missing_reasons"].get(field)
    assert obs["agy/gemini-4-argon@medium"]["frontier_code2_percent"] is None
    assert "exact-effort" in obs["agy/gemini-4-argon@medium"]["missing_reasons"]["frontier_code2_percent"]
    assert obs["agy/gemini-4-hydrogen@xhigh"]["page_effort"] == "xhigh"
    assert all(row["role"] == "planner" for key,row in obs.items() if "fable" in key)
    assert all(row["role"] == "executor" for key,row in obs.items() if "hydrogen" in key)
    assert all(row["engineering_index_edition"] is None for row in obs.values())
    assert all(row["engineering_index_edition_missing_reason"] for row in obs.values())


def test_eng_index_reverses_coding_order_without_policy_activation():
    obs = _obs()
    haiku = obs["claude/claude-haiku-5-5@high"]
    sonnet = obs["claude/claude-sonnet-5-5@medium"]
    assert haiku["engineering_index"] < sonnet["engineering_index"]
    assert calibration.coding_score(haiku) > calibration.coding_score(sonnet)
    assert haiku["official_intelligence_index"] == 30
    assert sonnet["official_intelligence_index"] == 31
    policy = calibration.policy_coverage(obs)
    assert "claude/claude-sonnet-5-5@medium" in policy["M"]["blocked_by_old_floor"]
    assert policy["M"]["coverage_only_floor_ceiling"] == 18
    # Equal-rung competitors are never reordered by Intelligence Index in research.
    assert calibration.OWNER_RUNGS["S"][0] == [
        "codex/gpt-6.1-terra@medium", "agy/gemini-4-hydrogen@medium"]
    assert calibration.OWNER_RUNGS["XL"][0] == ["claude/claude-opus-5-5@medium"]


def test_v4_production_pin_and_unchanged_catalog():
    from office import shipped_catalog
    m = shipped_catalog.load()
    assert m["benchmark_index_version"] == "Artificial Analysis Intelligence Index v4.3.2"
    assert m["models"]
    assert all("Artificial Analysis Intelligence Index v5.0.1" not in (row.get("benchmark_indexes") or {})
               for row in m["models"])
    from office import adaptive
    assert adaptive.settings({})["priors"]["benchmark_index"] == m["benchmark_index_version"]
    # The extracted evidence is standalone and import never mutates v4 catalog.
    assert evidence.manifest()["production_index_unchanged"] == m["benchmark_index_version"]


def test_untrusted_source_or_cross_edition_must_fail_closed(tmp_path):
    folder = tmp_path / "evidence"
    folder.mkdir()
    (folder/"manifest.json").write_bytes((evidence.FOLDER/"manifest.json").read_bytes())
    src = evidence.rows()
    src[0]["source_url"] = "https://artificialanalysis.ai.attacker.invalid/models/claude"
    (folder/"observations.jsonl").write_text("".join(json.dumps(x)+"\n" for x in src))
    with pytest.raises(evidence.EvidenceError, match="source URL"):
        evidence.validate(folder)
    src[0]["source_url"] = "https://artificialanalysis.ai/models/claude-haiku-5-5-medium"
    src[0]["index_version"] = "Artificial Analysis Intelligence Index v4.3.2"
    (folder/"observations.jsonl").write_text("".join(json.dumps(x)+"\n" for x in src))
    with pytest.raises(evidence.EvidenceError, match="mismatch"):
        evidence.validate(folder)
    src[0]["index_version"] = evidence.EDITION
    src[0]["frontier_code2_percent"] = None
    src[0]["missing_reasons"].pop("frontier_code2_percent",None)
    (folder/"observations.jsonl").write_text("".join(json.dumps(x)+"\n" for x in src))
    with pytest.raises(evidence.EvidenceError, match="null missing reason"):
        evidence.validate(folder)


def test_html_edition_and_effort_are_strict():
    row = {"slug":"example-high","effort":{"slug":"high"},
           "intelligenceIndex":33.8,"frontierCode20":0.2,"terminalBench40":0.3,
           "capabilities":{"engineering":40.0},"price1mInputTokens":1.5}
    payload = ["$", "N", None, {"initialModels":[row]}]
    flight = json.dumps([1, "10:" + json.dumps(payload)])
    html = f'<h1>{evidence.EDITION}</h1><div>Output speed 120 tok/s</div><script>self.__next_f.push({flight})</script>'
    scored = evidence.extract_html(html,"example-high","high")
    assert scored["frontier_code2_percent"] == 20
    assert scored["official_intelligence_index"] == 34
    assert scored["output_speed_tokens_per_second"] == 120
    with pytest.raises(evidence.EvidenceError,match="exact"):
        evidence.extract_html(html,"example-high","low")
    with pytest.raises(evidence.EvidenceError,match="v5.0.1"):
        evidence.extract_html(html.replace(evidence.EDITION,"Artificial Analysis Intelligence Index v4.3.2"),"example-high","high")


def _synthetic_episodes():
    # Fixture only, never shipped as empirical model outcomes.
    keys = ["claude/claude-sonnet-5-5@medium",
            "claude/claude-haiku-5-5@high",
            "codex/gpt-6.1-terra@medium",
            "agy/gemini-4-hydrogen@medium"]
    rows = []
    for run in range(16):
        for j in range(12):
            k = keys[(run+j)%len(keys)]
            success = ((run*3+j*7)%11)<7
            eventual = success or ((run+j)%3==0)
            time = (datetime(2026,9,1,tzinfo=timezone.utc)+timedelta(days=run,hours=j)).isoformat()
            rows.append({"run_id":f"run-{run}","task_id":f"task-{run}-{j}",
                         "dispatch_ids":[f"dispatch-{run}-{j}"],"route":k,
                         "size_class":"S" if j%2 else "M",
                         "domain":"backend" if j%3 else "frontend",
                         "role":"executor","ended_at":time,
                         "first_pass_accepted":success,"eventual_accepted":eventual,
                         "learn_weight":1.0,"attribution":"route",
                         "money_actual":1.+(j%4),"time_seconds":90+j,
                         "quota_burn":None,"recheck_count":0 if success else 1,
                         "operational_failure":False,"model_quality_failure":not eventual,
                         "regression_observed":None,"independent_review_passed":None})
    return rows


def test_replay_identical_factual_rows_with_chronological_holdouts(tmp_path):
    path=tmp_path/"private-outcomes.jsonl"
    path.write_text("".join(json.dumps(o)+"\n" for o in _synthetic_episodes()))
    result=calibration.run(path)
    assert result["status"] == "calibrated_shadow_only"
    cmp=result["comparisons"]["sized_XS_XL:first_pass_accepted"]
    assert cmp["split"]["straddling_runs_purged"] is True
    assert cmp["split"]["train_runs"]>=4
    assert cmp["split"]["test_n"] >= 10
    # N, A, B and C are scored on the same held-out attempts.
    assert {x["n"] for x in cmp["held_out"].values()} == {cmp["split"]["test_n"]}
    assert set(cmp["held_out"]) == {"N","A","B","C"}
    assert all(0<=x["brier"]<=1 for x in cmp["held_out"].values())
    assert cmp["formula_parameters"]["C"]["pseudo_outcomes"] in calibration.PRIOR_STRENGTHS
    unc=cmp["uncertainty"]
    if cmp["split"]["test_runs"]>=3:
        assert "delta_C_minus_N" in unc["brier_95_ci"]
    else:
        assert unc["status"]=="insufficient_test_runs_for_cluster_ci"
        assert cmp["empirical_ranking"]=="undetermined"
    assert result["shadow_routing"]["counterfactual_model_success"] == "unobserved"
    assert result["operational_outcomes"]["all"]["regression_rate"] is None
    # A winner needs a CI that excludes zero against the no-benchmark yardstick.
    ranking=cmp["empirical_ranking"]
    if isinstance(ranking,dict):
        assert ranking["winner"] in (None, ranking["lowest_brier_benchmark_formula"])
    # Unexecuted approved routes are reported unobserved, never as outcomes.
    replay=result["owner_replay_1143"]["S"]["rungs"]
    hydrogen=[e for e in replay if "hydrogen" in e["route"]][0]
    assert hydrogen["factual"]["n"] > 0  # the fixture does run it


def test_owner_replay_marks_unexecuted_routes_unobserved():
    rows=[{"size_class":"M","route":"claude/claude-sonnet-5-5@medium","first_route":True,
           "first_pass_accepted":True,"eventual_accepted":True}]
    replay=calibration.owner_replay(rows)
    m={e["route"]:e for e in replay["M"]["rungs"]}
    assert m["agy/gemini-4-hydrogen@high"]["factual"] == "unobserved"
    assert m["claude/claude-sonnet-5-5@medium"]["factual"]["n"] == 1
    assert replay["M"]["first_route_was_approved_rung1"] == 1
    assert replay["XL"]["rungs"][1]["factual"] == "unobserved"


def test_purged_split_drops_runs_that_straddle_a_cut():
    data=_synthetic_episodes()
    for row in data:
        row["ended_at"]=row["ended_at"]
    # Stretch run-0 across the whole period so it would straddle every cut.
    late=max(r["ended_at"] for r in data)
    for row in data:
        if row["run_id"]=="run-0" and row["task_id"].endswith("-11"):
            row["ended_at"]=late
    data.sort(key=lambda x:x["ended_at"])
    pieces,meta=calibration.chrono_split(data)
    assert meta["purged_runs"]>=1
    assert all(o["run_id"]!="run-0" for p in pieces for o in p)
    assert pieces[0][-1]["ended_at"] < pieces[1][0]["ended_at"] < pieces[2][0]["ended_at"]
    runs=[{o["run_id"] for o in p} for p in pieces]
    assert not (runs[0]&runs[1] or runs[1]&runs[2] or runs[0]&runs[2])


def test_benchmark_prior_is_weak_and_genuine_local_data_dominates():
    o={"route":"claude/claude-sonnet-5-5@medium","coding":20,"fc":12,"tb":32,
       "size_class":"M","domain":"backend","ended_at":"2026-11-14T00:00:00+00:00"}
    seen=[{**o,"ended_at":f"2026-11-{day:02}T00:00:00+00:00",
           "y":True,"learn_weight":1.0} for day in range(1,14)]
    p,n=calibration.posterior(o,seen,{"intercept":-1,"slope":0.5},k=2)
    assert n>10 and p>.8


def test_missing_local_db_stays_blocked_without_fake_comparison():
    result=calibration.run(None)
    assert result["status"]=="blocked_no_raw_outcomes"
    assert "held_out_first_pass" not in result
    assert "M" in result["policy_floor_shadow"]


def _office_db(path):
    """Minimal slice of the real runs.db schema (convergence-v1 era)."""
    con=sqlite3.connect(path)
    for sql in [
        "CREATE TABLE runs (id TEXT, phase TEXT, risk_json TEXT)",
        "CREATE TABLE tasks (run_id TEXT, id TEXT, status TEXT, accepted_revision_id TEXT, descriptor_json TEXT)",
        "CREATE TABLE revisions (id TEXT, dispatch_id TEXT)",
        "CREATE TABLE gates (run_id TEXT, task_id TEXT, revision_id TEXT, kind TEXT, verdict TEXT, review_status TEXT,"
        " status TEXT, members_json TEXT, created_at TEXT, finished_at TEXT, independence TEXT, reviewer_dispatch_id TEXT)",
        "CREATE TABLE outcome_labels (dispatch_id TEXT, label TEXT, primary_attribution TEXT)",
        "CREATE TABLE dispatches (id TEXT, run_id TEXT, task_id TEXT, role TEXT, harness TEXT, model TEXT, effort TEXT,"
        " started_at TEXT, ended_at TEXT, size_class TEXT, descriptor_json TEXT, money_actual REAL, quota_delta REAL,"
        " wall_clock_seconds REAL)",
    ]:
        con.execute(sql)
    con.execute("INSERT INTO runs VALUES ('r1','closed','{\"size_class\":\"XL\"}')")
    # t1: haiku@high gets a shared-gate RECHECK, then is replaced by opus (alias row) which lands.
    # t2: never sized at task level (run risk says XL); terra@medium ends with no submission.
    con.execute("INSERT INTO tasks VALUES ('r1','t1','accepted','v2','{\"task_size\":\"M\"}')")
    con.execute("INSERT INTO tasks VALUES ('r1','t2','cancelled',NULL,'{}')")
    con.execute("INSERT INTO tasks VALUES ('r1','t3','accepted','v3','{}')")
    d="INSERT INTO dispatches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    con.execute(d,("d1","r1","t1","executor","claude","claude-haiku-5-5","high","2026-11-01T00:00:00Z","2026-11-01T00:10:00Z","M","{}",None,None,600.))
    con.execute(d,("d2","r1","t1","executor","claude","opus","medium","2026-11-01T01:00:00Z","2026-11-01T01:20:00Z","M","{}",None,None,1200.))
    con.execute(d,("d3","r1","t2","executor","codex","gpt-6.1-terra","medium","2026-11-01T00:00:00Z","2026-11-01T00:01:00Z",None,"{}",None,None,60.))
    con.execute(d,("d4","r1","t3","executor","codex","gpt-6-luna","high","2026-11-01T00:00:00Z","2026-11-01T00:30:00Z",None,"{}",None,None,None))
    con.execute(d,("rv1","r1",None,"code_reviewer","codex","gpt-6-luna","high","2026-11-01T01:30:00Z","2026-11-01T01:40:00Z",None,"{}",None,None,1.))
    con.execute(d,("rv2","r1",None,"code_reviewer","codex","gpt-6-luna","high","2026-11-01T00:40:00Z","2026-11-01T00:45:00Z",None,"{}",None,None,1.))
    con.execute("INSERT INTO revisions VALUES ('v1','d1')")
    con.execute("INSERT INTO revisions VALUES ('v2','d2')")
    con.execute("INSERT INTO revisions VALUES ('v3','d4')")
    g="INSERT INTO gates VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
    # Shared gate owned by t3 names t1 as a member: the RECHECK counts for t1's live segment.
    con.execute(g,("r1","t3",None,"convergence_review","RECHECK",None,"done",'["t1"]',"2026-11-01T00:20:00Z","2026-11-01T00:25:00Z","independent","rv2"))
    con.execute(g,("r1","t1","v2","convergence_review","APPROVED",None,"done",None,"2026-11-01T01:30:00Z","2026-11-01T01:45:00Z","independent","rv1"))
    con.execute(g,("r1","t3","v3","convergence_review","APPROVED",None,"done",None,"2026-11-01T00:40:00Z","2026-11-01T00:50:00Z","independent","rv2"))
    con.execute("INSERT INTO outcome_labels VALUES ('d2','verified_no_observed_failure',NULL)")
    con.commit()
    con.close()


def test_read_only_extract_counts_convergence_rechecks_and_never_borrows_run_size(tmp_path):
    path=tmp_path/"runs.db"
    _office_db(path)
    before=extractor.digest(path)
    rows,receipt=extractor.extract(path)
    assert before==extractor.digest(path)==receipt["source_sha256_after"]
    by={(r["task_id"],r["segment_index"]):r for r in rows}
    haiku,opus=by[("t1",0)],by[("t1",1)]
    # route_learning would report zero review rounds here.
    assert haiku["recheck_count"]==1 and haiku["outcome"]=="rerouted"
    assert haiku["model_quality_failure"] and not haiku["operational_failure"] and haiku["learn_weight"]==1.0
    assert opus["eventual_accepted"] and opus["first_pass_accepted"] and opus["recheck_count"]==0
    # Legacy alias row resolves to its pinned exact invocation, never a sibling.
    assert opus["route"]=="claude/claude-opus-5-5@medium"
    assert opus["post_land_labels"]==["verified_no_observed_failure"]
    assert opus["regression_observed"] is None
    assert opus["independent_review_passed"] is True
    # Luna executor approved by a Luna reviewer is not independent review.
    assert by[("t3",0)]["independent_review_passed"] is False
    assert by[("t3",0)]["time_seconds"] is None
    terra=by[("t2",0)]
    # Run risk says XL; the task never declared a size.
    assert terra["size_class"]=="unknown" and terra["size_source"]=="none-recorded"
    assert terra["attribution"]=="plan" and terra["learn_weight"]==0.0
    assert all(r["money_actual"] is None and r["quota_burn"] is None for r in rows)
    assert receipt["attempts"]==4 and receipt["money_actual_recorded"]==0


def test_attempt_cost_join_is_explicit_and_private(tmp_path):
    path=tmp_path/"runs.db"
    _office_db(path)
    costs=tmp_path/"tasks-cost.json"
    costs.write_text(json.dumps([{"run":"r1","task":"t1","atts":[
        {"pos":0,"covered":True,"usd":2.0,"revUsd":0.5,"usageLimit":False},
        {"pos":1,"covered":False,"usd":0,"revUsd":0,"usageLimit":True}]}]))
    rows,receipt=extractor.extract(path,attempt_costs=costs)
    by={(r["task_id"],r["segment_index"]):r for r in rows}
    assert by[("t1",0)]["estimated_cost_usd"]==2.5 and by[("t1",0)]["money_actual"] is None
    assert by[("t1",1)]["estimated_cost_usd"] is None and by[("t1",1)]["usage_limit_hit"] is True
    assert by[("t2",0)]["usage_limit_hit"] is None
    assert receipt["estimated_cost_joined"]==1


def test_route_identity_resolution_uses_exact_invocations_only():
    aliases=extractor.canonical_routes()
    assert aliases["claude/claude-opus-5-5@medium"]=="claude/claude-opus-5-5@medium"
    assert aliases["claude/opus@medium"]=="claude/claude-opus-5-5@medium"
    assert aliases["agy/gemini-3.8-flash-medium@medium"]=="agy/gemini-3.8-flash@medium"
    assert aliases["agy/Claude Sonnet 5.5 (High)@high"]=="agy/claude-sonnet-5-5@high"
    # The floating sonnet alias invokes a different model; it must not borrow 5.5 evidence.
    assert aliases.get("claude/sonnet@high")!="claude/claude-sonnet-5-5@high"
    assert "claude/sonnet@medium" not in aliases


def test_report_replay_bootstrap_is_the_reports_own_and_deterministic():
    from eval import aa_v5_report_replay as replay
    xs=[1.0,2.0,3.0,4.0,10.0]
    assert replay.lcg_bootstrap(xs)==replay.lcg_bootstrap(xs)
    lo,hi=replay.lcg_bootstrap(xs)
    assert 1.0<=lo<statistics.mean(xs)<hi<=10.0
    assert replay.lcg_bootstrap(xs[:4]) is None
    tasks=[{"run":f"r{i}","task":"T1","size":"M","outcome":"accepted",
            "atts":[{"route":"claude/sonnet@medium","pos":0,"kind":"oneshot" if i%2 else "accepted_after_recheck",
                     "outcome":"accepted","covered":True,"usd":1.0+i,"revUsd":0.5}]} for i in range(10)]
    row=replay.first_route_table(tasks)[0]
    assert row["tasks"]==10 and row["landed_on_rung1"]==1.0 and row["one_shot"]==0.5
    assert row["chain_usd_mean"]==6.0 and row["cost_n"]==10


def test_committed_local_replay_is_sanitized_and_names_no_winner():
    data=json.loads((evidence.FOLDER/"local-replay-2026-11-14.json").read_text())
    text=json.dumps(data)
    assert "/Users/" not in text and "@favor" not in text
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{24}", text)  # run ids
    assert not re.search(r'"D[0-9a-f]{8}"', text)  # dispatch ids
    rec=data["extractor_reconciliation"]
    assert rec["frozen_report_attempts"]==rec["attempts_matching_dispatches_outcome_and_recheck"]==1362
    assert data["report_reproduction"]["population"]=={"frozen_attempts":1362,"frozen_tasks":1019,
        "frozen_tasks_missing_or_changed":0,"scoreboard_attempts":1374,"scoreboard_tasks":1027}
    cal=data["calibration"]
    assert cal["production_index"]=="unchanged-v4.3.2"
    for cmp in cal["comparisons"].values():
        if "held_out" in cmp:
            assert cmp["empirical_ranking"]=="undetermined" or cmp["empirical_ranking"]["winner"] is None
