"""#862: AA v5.0.1 research-only evidence, observable replay and no cutover."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

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
    assert result["split"]["time_overlap_purged_by_run_cluster"] is True
    assert result["split"]["train_runs"]>=4
    assert result["split"]["test_n"] >= 10
    assert {x["n"] for x in result["held_out_first_pass"].values()} == {result["split"]["test_n"]}
    assert all(0<=x["brier"]<=1 for x in result["held_out_first_pass"].values())
    assert result["formula_parameters"]["C"]["pseudo_outcomes"] in calibration.PRIOR_STRENGTHS
    assert result["shadow_routing"]["counterfactual_model_success"] == "unobserved"
    assert result["operational_outcomes"]["all"]["regression_verified_n"] == 0


def test_benchmark_prior_is_weak_and_genuine_local_data_dominates():
    o={"route":"claude/claude-sonnet-5-5@medium","coding":20,"fc":12,"tb":32,
       "size_class":"M","domain":"backend","ended_at":"2026-11-14T00:00:00+00:00"}
    seen=[{**o,"ended_at":f"2026-11-{day:02}T00:00:00+00:00",
           "first_pass_accepted":True,"learn_weight":1.0} for day in range(1,14)]
    p,n=calibration.posterior(o,seen,{"intercept":-1,"slope":0.5},k=2)
    assert n>10 and p>.8


def test_overlap_runs_are_not_leaked_across_time_boundaries():
    data=_synthetic_episodes()
    for row in data:
        if row["run_id"]=="run-0" and row["task_id"].endswith("-0"):
            row["ended_at"]="2026-09-14T13:00:00+00:00"
    data.sort(key=lambda x:x["ended_at"])
    try:
        pieces,meta=calibration.chrono_split(data)
    except calibration.InsufficientData as exc:
        assert "overlap" in str(exc) or "chronological" in str(exc)
    else:
        assert pieces[0][-1]["ended_at"] < pieces[1][0]["ended_at"]
        assert pieces[1][-1]["ended_at"] < pieces[2][0]["ended_at"]


def test_missing_local_db_stays_blocked_without_fake_comparison():
    result=calibration.run(None)
    assert result["status"]=="blocked_no_raw_outcomes"
    assert "held_out_first_pass" not in result
    assert "M" in result["policy_floor_shadow"]


def test_read_only_sqlite_extract_preserves_hash_and_attributed_ids(tmp_path):
    path=tmp_path/"runs.db"
    con=sqlite3.connect(path)
    statements=[
        "CREATE TABLE runs (id TEXT,playbook TEXT,phase TEXT,risk_json TEXT)",
        "CREATE TABLE tasks (run_id TEXT,id TEXT,status TEXT,accepted_revision_id TEXT)",
        "CREATE TABLE revisions (id TEXT, dispatch_id TEXT)",
        "CREATE TABLE gates (revision_id TEXT, kind TEXT,verdict TEXT)",
        "CREATE TABLE findings (revision_id TEXT,dispatch_id TEXT,category TEXT,severity TEXT,state TEXT)",
        "CREATE TABLE outcome_labels (dispatch_id TEXT,primary_attribution TEXT)",
        "CREATE TABLE dispatches (id TEXT,run_id TEXT,role TEXT,task_id TEXT,triple TEXT,harness TEXT,model TEXT,effort TEXT,started_at TEXT,ended_at TEXT,terminal_classification TEXT,exit_code INTEGER,outcome TEXT,attribution TEXT,stall_kind TEXT,money_actual REAL,descriptor_json TEXT,size_class TEXT,quota_delta REAL)",
    ]
    for sql in statements:
        con.execute(sql)
    con.execute("INSERT INTO runs VALUES ('r1','normal','closed','{}')")
    con.execute("INSERT INTO tasks VALUES ('r1','t1','accepted','v1')")
    con.execute("INSERT INTO revisions VALUES ('v1','d1')")
    con.execute("INSERT INTO gates VALUES ('v1','code_review','ACCEPTED')")
    con.execute("INSERT INTO dispatches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("d1","r1","executor","t1","claude@1/claude-sonnet-5-5@medium",
                 "claude","claude-sonnet-5-5","medium","2026-11-01T00:00:00Z",
                 "2026-11-01T00:05:00Z","completed",0,"accepted",None,None,2.5,
                 '{"evidence_domain":"backend"}',"M",4.0))
    con.commit()
    con.close()
    before=extractor.digest(path)
    rows,receipt=extractor.extract(path)
    assert before==extractor.digest(path)==receipt["sha256_after"]
    assert receipt["episodes"] == 1
    assert rows[0]["run_id"]=="r1" and rows[0]["dispatch_ids"]==["d1"]
    assert rows[0]["first_pass_accepted"] is True
    assert rows[0]["eventual_accepted"] is True
    assert rows[0]["domain"]=="backend"
    assert rows[0]["money_actual"]==2.5
    assert rows[0]["quota_burn"]==4.0
    assert rows[0]["independent_review_passed"] is None
