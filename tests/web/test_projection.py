"""The Office projection reports recorded evidence and nothing else."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from office import db
from office.web import projection, synthetic


def _col(run, column):
    return {n["id"]: n for n in run["agents"]["columns"][column]}


@pytest.fixture
def r1(mini, make_observer):
    return make_observer(mini["path"], runs_dir=mini["runs_dir"]).run("R1")


def test_run_fields(r1):
    assert r1["id"] == "run:R1" and r1["goal"].startswith("Fix #99") and r1["phase"] == "executing"
    assert r1["gear"] == "M" and r1["office_version"] == "3.3.3" and r1["release_line"] == "3.3"
    assert r1["runtime"] == "runtime:3.3.3"
    assert r1["end_state"] == {"value": "merged", "source": "requirements", "requirements_version": 1}
    issue = r1["issue"]
    assert issue["number"] == 42 and issue["provenance"] == "office-record" and issue["source"] == "landing_json.issue"
    assert r1["owner"]["kind"] == "session" and r1["owner"]["id"] == "session:R1/claude/S-active"
    assert r1["liveness"] == "live"


def test_pr_links_come_only_from_pr_json(r1):
    # The goal mentions #99 and a branch name; neither becomes a link.
    assert [(p["number"], p["provenance"], p["source"]) for p in r1["prs"]] == [
        (7, "office-record", "tasks.pr_json"), (8, "office-record", "tasks.pr_json")]
    stacked = r1["prs"][1]
    assert stacked["stacked_on"] == "T4" and stacked["base"] == "office/R1/T4"
    assert all(p["number"] != 99 for p in r1["prs"])


def test_office_gates_are_separate_from_github_checks(r1):
    office = r1["gates"]["office"]
    assert office["total"] == 2 and office["by_kind"]["code_review"]["verdict"] == {"PASS": 1}
    assert r1["gates"]["github_checks"]["status"] == "unavailable"


def test_progress_is_weighted_by_task_status_only(r1, mini, make_observer):
    # T1..T6 with T5 cancelled: 1 accepted of 5 counted.
    assert r1["progress"]["accepted_weight"] == 1 and r1["progress"]["total_weight"] == 5
    assert r1["progress"]["value"] == pytest.approx(0.2) and "task" in r1["progress"]["basis"]
    assert make_observer(mini["path"]).run("R2")["progress"] is None


def test_liveness_classes(mini, make_observer):
    con = mini["con"]
    with db.transaction(con):
        synthetic.insert_run(con, "DONE", git_common_dir="/src/a/.git", phase="closed")
    obs = make_observer(mini["path"])
    assert obs.run("R2")["liveness"] == "resumable" and obs.run("R2")["owner"] == {"kind": "none"}
    assert obs.run("DONE")["liveness"] == "terminal"


def test_agent_topology_current_nodes_and_history(r1):
    cols = r1["agents"]["columns"]
    assert set(cols) == set(projection.COLUMNS)
    assert list(_col(r1, "orchestrators")) == ["session:R1/claude/S-active"]  # the ended binding is not a node
    assert set(_col(r1, "executors")) == {"dispatch:D2", "dispatch:D3", "dispatch:D4"}
    assert set(_col(r1, "code_reviewers")) == {"dispatch:D5"}
    assert set(_col(r1, "visual_verifiers")) == {"dispatch:D7"}
    assert set(_col(r1, "plan_reviewers")) == {"dispatch:D8"}
    assert set(_col(r1, "other")) == {"dispatch:D6"}  # planner: not relabelled from phase
    assert [h["id"] for h in r1["history"]] == ["dispatch:D1"]
    d2 = _col(r1, "executors")["dispatch:D2"]
    assert d2["harness"] == "codex" and d2["model"] == "gpt-6-luna" and d2["effort"] == "high"
    assert d2["current_work"]["task"] == "task:R1/T1"


def test_agent_state_evidence_is_kept_distinct(r1):
    ex = _col(r1, "executors")
    d2, d3, d4 = ex["dispatch:D2"]["state"], ex["dispatch:D3"]["state"], ex["dispatch:D4"]["state"]
    assert d2["quota_wait"] == {"active": True, "kind": "usage_limit", "resets_at": "2026-10-07T17:00:00Z",
                                "label": "resets 5pm"}
    assert d2["process"] == "unknown" and d2["activity"] == "unknown"  # probes default to unknown
    assert not d2["paused"] and not d2["complete"]
    assert d3["paused"] and d3["activity"] == "idle" and not d3["quota_wait"]["active"]
    assert d4["complete"] and d4["process"] == "exited"
    assert _col(r1, "visual_verifiers")["dispatch:D7"]["state"]["unavailable"]
    assert _col(r1, "plan_reviewers")["dispatch:D8"]["state"]["stale"]


def test_blocked_task_state(mini, make_observer):
    con = mini["con"]
    with db.transaction(con):
        synthetic.insert_dispatch(con, "D9", "R1", task_id="T3", status="running", at=20)
    run = make_observer(mini["path"]).run("R1")
    assert _col(run, "executors")["dispatch:D9"]["state"]["blocked"] is True


def test_visual_roles_map_to_visual_verifiers_and_await_ingestion(mini, make_observer):
    con = mini["con"]
    with db.transaction(con):
        synthetic.insert_dispatch(con, "D10", "R1", role="browser_verifier", task_id="T3", status="running", at=21)
        synthetic.insert_dispatch(con, "D11", "R1", role="visual_reviewer", task_id="T2", status="running", at=22)
    replies = {"D10": "VERDICT: PASS\n"}
    run = make_observer(mini["path"], fs=lambda path: replies.get(path.parent.name),
                        runs_dir=mini["runs_dir"]).run("R1")
    visual = _col(run, "visual_verifiers")
    assert {"dispatch:D10", "dispatch:D11"} <= set(visual)
    assert visual["dispatch:D10"]["state"]["reply_written_awaiting_ingestion"] is True
    assert visual["dispatch:D11"]["state"]["reply_written_awaiting_ingestion"] is False
    assert not any(n["role"] in ("browser_verifier", "visual_reviewer") for n in run["agents"]["columns"]["other"])


def test_synthetic_workspace_populates_visual_verifiers(tmp_path):
    ws = synthetic.build_workspace(tmp_path / "ws", "small", seed=7)
    con = db.connect(ws["db"])
    try:
        rows = con.execute("SELECT role, status, ended_at IS NULL FROM dispatches "
                           "WHERE role IN ('visual_reviewer', 'browser_verifier')").fetchall()
    finally:
        con.close()
    assert {r[0] for r in rows} == {"visual_reviewer", "browser_verifier"}
    assert ("done", 0) in {(r[1], r[2]) for r in rows} and ("running", 1) in {(r[1], r[2]) for r in rows}
    did = ws["specials"]["visual_awaiting_ingestion"]
    assert any(p.name == "reply.txt" for p in ws["runs_dir"].glob(f"*/dispatches/{did}/reply.txt"))


def test_reply_awaiting_ingestion_uses_the_injected_reader(mini, make_observer):
    reads = []

    def fs(text):
        def read(path: Path):
            reads.append(path)
            return text
        return read

    def d5(text):
        run = make_observer(mini["path"], fs=fs(text), runs_dir=mini["runs_dir"]).run("R1")
        return _col(run, "code_reviewers")["dispatch:D5"]["state"]["reply_written_awaiting_ingestion"]

    assert d5("VERDICT: PASS\n") is True
    assert reads[-1] == mini["runs_dir"] / "R1" / "dispatches" / "D5" / "reply.txt"
    assert d5("  \n") is False and d5(None) is False
    # Executors are never checked for a reply.
    assert {p.parent.name for p in reads} == {"D5", "D8"}  # only unended reviewers


def test_reply_awaiting_ingestion_ignores_ended_reviewer(mini, make_observer):
    con = mini["con"]
    with db.transaction(con):
        con.execute("UPDATE dispatches SET ended_at='t', status='done' WHERE id='D5'")
    run = make_observer(mini["path"], fs=lambda _p: "VERDICT: PASS").run("R1")
    assert _col(run, "code_reviewers")["dispatch:D5"]["state"]["reply_written_awaiting_ingestion"] is False


def test_liveness_probes_are_injectable(mini, make_observer):
    run = make_observer(mini["path"], process=lambda kind, row: "alive", activity=lambda kind, row: "busy").run("R1")
    d2 = _col(run, "executors")["dispatch:D2"]["state"]
    assert d2["process"] == "alive" and d2["activity"] == "busy"
    assert _col(run, "executors")["dispatch:D4"]["state"]["process"] == "exited"  # recorded end wins
    assert _col(run, "orchestrators")["session:R1/claude/S-active"]["state"]["process"] == "alive"


def test_telemetry_is_never_a_zero_default(r1, mini, make_observer):
    for column in r1["agents"]["columns"].values():
        for node in column:
            for name, metric in node["telemetry"].items():
                assert set(metric) == {"status", "value", "unit", "source", "observed_at"}
                if not (name == "quota" and metric["status"] == "measured"):
                    assert metric["status"] == "unknown" and metric["value"] is None
    d2 = _col(r1, "executors")["dispatch:D2"]["telemetry"]["quota"]
    assert d2["status"] == "measured" and d2["source"] == "dispatches.stall_kind"

    def telemetry(kind, row):
        return {"cpu": {"status": "measured", "value": 12.5, "source": "ps", "observed_at": "t"}}

    run = make_observer(mini["path"], telemetry=telemetry).run("R1")
    cpu = _col(run, "executors")["dispatch:D3"]["telemetry"]["cpu"]
    assert cpu == {"status": "measured", "value": 12.5, "unit": "percent", "source": "ps", "observed_at": "t"}


def test_route_projection_reads_the_recorded_audit(r1):
    tasks = {t["task_id"]: t for t in r1["tasks"]}
    route = tasks["T1"]["route"]
    assert route["available"] is True
    assert route["primary"] == "claude/claude-opus-5-5/high" and route["fallbacks"] == ["codex/gpt-6-luna/high"]
    assert route["reason"] and route["strength"].endswith("strength") and route["weakness"].endswith("weakness")
    assert route["fallbacks_taken"] == [{"route": "codex/gpt-6-luna/high", "reason": "usage limit"}]
    assert route["planner_override"]["why"] == "opus handles this shape"
    assert [a["id"] for a in route["audits"]] == ["RA1"]
    missing = tasks["T2"]["route"]
    assert missing["available"] is False and missing["reason"] and missing["audits"] == []


def test_route_projection_without_route_audit_table(writer, make_observer):
    path, con = writer
    with db.transaction(con):
        synthetic.insert_run(con, "L", git_common_dir="/src/l/.git", office_version="3.0.4")
        synthetic.insert_task(con, "L", "T1", status="running")
        synthetic.insert_dispatch(con, "DL", "L", task_id="T1")
    run = make_observer(path).run("L")
    assert run["capabilities"]["route_audit"] is False
    assert run["capabilities"]["read_only"] is True and run["capabilities"]["legacy_runtime"] is True
    route = run["tasks"][0]["route"]
    assert route["available"] is False and route["dispatched"] == "codex/gpt-6-luna/high"


def test_capabilities_for_current_runs(r1):
    caps = r1["capabilities"]
    assert all(caps[k] for k in ("tasks", "issue_link", "pr_links", "gates", "route_audit", "session_bindings",
                                 "quota_wait", "activity"))
    assert caps["read_only"] is False


def test_small_synthetic_workspace(workspace, make_observer):
    assert workspace["repos"] == 3 and workspace["issues"] == 30
    gh = json.loads(Path(workspace["github"]).read_text())
    numbers = {}
    for issue in gh["issues"]:
        numbers.setdefault(issue["number"], set()).add(issue["repo"])
    assert any(len(repos) > 1 for repos in numbers.values())
    assert any(p["stacked"] for p in gh["prs"])
    obs = make_observer(workspace["db"], runs_dir=workspace["runs_dir"],
                        repo_slugs=synthetic.repo_slugs(workspace["github"]))
    ws = obs.workspace()
    runs = {r["run_id"]: r for r in ws["runs"]}
    sp = workspace["specials"]
    for key in ("legacy_route", "terminal_started", "stacked_pr", "paused", "blocked", "quota_wait",
                "awaiting_ingestion", "terminal"):
        assert key in sp, key
    assert all(not t["route"]["available"] for t in runs[sp["legacy_route"]]["tasks"])
    assert runs[sp["legacy_route"]]["capabilities"]["read_only"] is True
    assert runs[sp["terminal"]]["liveness"] == "terminal"
    started = runs[sp["terminal_started"]]
    assert started["owner"] == {"kind": "none"}
    nodes = [n for r in ws["runs"] for c in r["agents"]["columns"].values() for n in c]
    by_id = {n["id"]: n for n in nodes}
    assert by_id[f"dispatch:{sp['quota_wait']}"]["state"]["quota_wait"]["active"]
    assert by_id[f"dispatch:{sp['awaiting_ingestion']}"]["state"]["reply_written_awaiting_ingestion"]
    paused_run, paused_task = sp["paused"].split("/")
    assert any(n["state"]["paused"] for n in runs[paused_run]["agents"]["columns"]["executors"])
    issue_refs = [r["issue"]["ref"] for r in ws["runs"]]
    assert len(issue_refs) > len(set(issue_refs))  # two runs for one issue
    assert all(ref.startswith("issue:repo:github.com/") for ref in issue_refs)
    assert len(ws["repos"]) == 3


def test_synthetic_is_deterministic(tmp_path, dump_hash):
    a = synthetic.build_workspace(tmp_path / "a", "small", seed=7)
    b = synthetic.build_workspace(tmp_path / "b", "small", seed=7)
    c = synthetic.build_workspace(tmp_path / "c", "small", seed=8)
    def logical(ws, root):  # state_dir paths name the build directory
        return dump_hash(ws["db"], replace=(str(root), "ROOT"))

    assert logical(a, tmp_path / "a") == logical(b, tmp_path / "b") != logical(c, tmp_path / "c")
    assert Path(a["github"]).read_text() == Path(b["github"]).read_text()


def test_large_synthetic_workspace(tmp_path, make_observer):
    ws = synthetic.build_workspace(tmp_path / "big", "large")
    assert ws["repos"] >= 40 and ws["issues"] >= 2000 and ws["runs"] >= 300
    assert ws["tasks"] >= 3000 and ws["dispatches"] >= 8000
    con = db.connect(ws["db"])
    try:
        assert con.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0] == ws["dispatches"]
        assert con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == ws["tasks"]
    finally:
        con.close()
    projected = make_observer(ws["db"], runs_dir=ws["runs_dir"]).workspace()
    assert len(projected["runs"]) == ws["runs"]
