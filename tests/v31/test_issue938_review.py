"""Issue #938: required test-strength proof, one-round producer review and security convergence."""
from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from office import briefs, contract, convergence, preflight, review_parse
from office import self_review_proof as proof, self_review_subagents as audits


def git(wt, *args):
    p = subprocess.run(["git", "-C", str(wt), *args], capture_output=True, text=True, check=True)
    return p.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    wt = tmp_path / "repo"
    wt.mkdir()
    git(wt, "init", "-q")
    git(wt, "config", "user.name", "office-tests")
    git(wt, "config", "user.email", "tests@example.invalid")
    (wt / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "base")
    base = git(wt, "rev-parse", "HEAD")
    (wt / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (wt / "tests").mkdir()
    (wt / "tests" / "test_calc.py").write_text(
        "import unittest\nfrom calc import add\n"
        "class Check(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        self.assertEqual(add(3, 2), 5)\n")
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "fix")
    return wt, base, git(wt, "rev-parse", "HEAD")


TEST = "python3 -m unittest discover -s tests -p test_calc.py"
GEAR = {"id": "r", "gates": {"review_contract": contract.CONVERGENCE, "gear_model_version": "run-gear-1"}}


def test_main_change_proof_is_red_then_green_and_never_mutates_worktree(repo):
    wt, base, head = repo
    status_before = git(wt, "status", "--porcelain")
    worktrees_before = git(wt, "worktree", "list", "--porcelain")
    got = proof.prove(wt, base, head, ["calc.py", "tests/test_calc.py"], TEST, GEAR)
    assert got["ok"] and got["red_exit"] != 0 and got["green_exit"] == 0, got
    assert git(wt, "status", "--porcelain") == status_before
    assert git(wt, "worktree", "list", "--porcelain") == worktrees_before


def test_vacuous_test_is_rejected_even_if_test_command_passes(repo):
    wt, base, head = repo
    (wt / "tests" / "test_calc.py").write_text(
        "import unittest\nclass Check(unittest.TestCase):\n"
        "    def test_vacuous(self): self.assertTrue(True)\n")
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "weaken test")
    head = git(wt, "rev-parse", "HEAD")
    got = proof.prove(wt, base, head, ["calc.py", "tests/test_calc.py"], TEST, GEAR)
    assert not got["ok"] and got["red_exit"] == 0 and got["green_exit"] == 0, got


def test_submit_cannot_skip_red_green_preflight(repo):
    wt, base, head = repo
    class Fake:
        def execute(self, sql, params):
            assert sql.startswith("SELECT")
            class Rows:
                def fetchone(self): return None
            return Rows()
    task = {"id": "T1", "scope": ["calc.py", "tests/**"], "checks": [TEST]}
    got = proof.gate(Fake(), GEAR, task, wt, base, head, ["calc.py", "tests/test_calc.py"], execute=False)
    assert not got["ok"] and "preflight" in got["reason"]


def test_missing_targeted_test_blocks_main_change_proof(repo):
    wt, base, head = repo
    task = {"id": "T1", "scope": ["calc.py"], "checks": ["python3 -m compileall calc.py"]}
    got = proof.gate(None, GEAR, task, wt, base, head, ["calc.py"], execute=False)
    assert not got["ok"] and "targeted test command" in got["reason"]


def test_docs_only_and_legacy_runs_require_no_main_change_proof():
    assert proof._sources(["docs/README.md", "tests/test_calc.py", ".github/workflows/test.yml"]) == []
    assert not proof.applicable({"gates": {"review_contract": contract.LEGACY, "gear_model_version": "run-gear-1"}})
    assert proof.applicable(GEAR)


def ledger(head, *, platform="skipped not in diff", security="skipped checked at convergence", round=1, extra=""):
    return "\n".join([f"COMMIT {head}", f"ROUND {round}",
                      "LENS edge-cases reviewed", "LENS test-strength reviewed",
                      f"LENS platform {platform}", f"LENS security {security}", extra]) + "\n"


def test_new_ledger_requires_one_round_and_strength_lens(repo):
    wt, _, head = repo
    text = ledger(head, extra="TRUNCATED self-review truncated: budget cap")
    assert preflight.check_ledger(text, head, ["add correct"], ["calc.py", "tests/**"], wt,
                                  "single", ["calc.py", "tests/test_calc.py"]) == ([], [])
    old = ledger(head, round=2)
    stop, fixes = preflight.check_ledger(old, head, [], ["calc.py"], wt, "single", ["calc.py"])
    assert any("one-round" in f for f in fixes)
    missing_strength = ledger(head).replace("LENS test-strength reviewed", "LENS test-strength skipped bad")
    _, fixes = preflight.check_ledger(missing_strength, head, [], ["calc.py"], wt, "single", ["calc.py"])
    assert any("test-strength must be reviewed" in f for f in fixes)


def test_platform_lens_is_required_by_changed_diff(repo):
    wt, _, head = repo
    text = ledger(head)
    _, fix = preflight.check_ledger(text, head, [], ["scripts/**"], wt, "single", ["scripts/build.sh"])
    assert any("platform/build must be reviewed" in f for f in fix)
    assert audits.platform_relevant(["scripts/build.sh", "src/main.py"])
    assert not audits.platform_relevant(["src/main.py"])


def test_budget_truncation_line_is_strict_and_recorded(repo):
    wt, _, head = repo
    raw = ledger(head, extra="TRUNCATED self-review truncated: budget cap")
    parsed, errors = preflight.parse_ledger(raw)
    assert not errors and parsed["truncated"] is True
    parsed, errors = preflight.parse_ledger(raw + "TRUNCATED self-review truncated: budget cap\n")
    assert errors


def test_sensitive_surface_selection_is_not_based_on_gear():
    for path in ("src/auth/login.py", "src/permissions.py", "src/input_parser.py",
                 "src/secrets.py", "scripts/production_write.sh"):
        assert audits.security_relevant([path])
    assert not audits.security_relevant(["src/math.py", "docs/overview.md"])
    run = dict(GEAR, gear="XS")
    assert audits.rubrics(run, {"scope": ["src/auth/login.py"]}) == ()
    assert audits.rubrics({**run, "gear": "XL"}, {"scope": ["src/math.py"]}) == ("combined",)


@pytest.mark.parametrize("verdict", ["APPROVED", "RECHECK", "INVALID_SHA"])
def test_mandatory_independent_security_gate_blocks_or_routes_verdict(monkeypatch, tmp_path, verdict):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE gates(id TEXT, run_id TEXT, subject TEXT, scope TEXT, task_id TEXT, "
                 "plan_version INTEGER, kind TEXT, input_key TEXT, status TEXT, created_at TEXT,"
                 "review_status TEXT, verdict TEXT, summary TEXT, route TEXT, reviewer_dispatch_id TEXT,"
                 "finished_at TEXT)")
    monkeypatch.setattr(convergence, "_changed", lambda c, r, t: {"src/auth/login.py"})
    from office import state, paths
    monkeypatch.setattr(state, "emit", lambda *a, **k: None)
    monkeypatch.setattr(paths, "run_dir", lambda _: tmp_path / "run")
    run = {**GEAR, "plan_version": 1, "repo_root": str(tmp_path)}
    scope = {"id": "L-T1", "tasks": ["T1"]}
    tasks = [{"id": "T1", "scope": ["src/auth/login.py"]}]
    commit = "a" * 40
    called = []

    def reviewer(c, r, gate, role, brief, **kwargs):
        called.append(brief)
        reply = tmp_path / "run" / "dispatches" / "Dsecurity" / "reply.txt"
        reply.parent.mkdir(parents=True, exist_ok=True)
        actual = "b" * 40 if verdict == "INVALID_SHA" else commit
        reply.write_text(f"REVIEWED_COMMIT {actual}\nVERDICT: APPROVED\nNEXT proceed\n")
        v = "APPROVED" if verdict == "INVALID_SHA" else verdict
        return {"status": contract.COMPLETED, "dispatch_id": "Dsecurity",
                "verdict": v, "parsed": review_parse.Parsed(verdict=v, contract=contract.CONVERGENCE)}
    got = audits.audit_security(conn, run, scope, tasks, tmp_path, commit, "diff", reviewer=reviewer)
    assert called and "mandatory" in called[0].lower() and "SECURITY" in called[0]
    if verdict == "INVALID_SHA":
        assert got["status"] == contract.EVIDENCE_BLOCKED
    else:
        assert got["status"] == contract.COMPLETED and got["verdict"] == verdict
        if verdict == "RECHECK":
            assert got["outcome"]["parsed"].verdict == "RECHECK"
    stored = conn.execute("SELECT status, review_status, verdict FROM gates WHERE kind='convergence_security'").fetchone()
    assert stored["status"] == "done"
    assert stored["review_status"] == (contract.EVIDENCE_BLOCKED if verdict == "INVALID_SHA" else contract.COMPLETED)


def test_sensitive_added_code_in_generic_handler_forces_security():
    diff = ("diff --git a/src/handlers.py b/src/handlers.py\n"
            "+++ b/src/handlers.py\n"
            "+def authorize_user(request): return check_permissions(request)\n")
    assert audits.security_relevant(["src/handlers.py"], diff)
    diff = ("diff --git a/tests/test_handler.py b/tests/test_handler.py\n"
            "+def authorize_user(request): return check_permissions(request)\n")
    assert not audits.security_relevant(["tests/test_handler.py"], diff)


def test_sensitive_lane_forces_convergence_even_when_gear_does_not_fund_it(monkeypatch):
    monkeypatch.setattr(convergence, "_changed", lambda c, r, t: {"src/auth/login.py"})
    from office import state
    monkeypatch.setattr(state, "get_task", lambda c, rid, tid: {"id": tid})
    assert not convergence.review_required({"gates": {}})
    assert convergence.mandatory_security(None, {"id": "r", "gates": {}}, {"tasks": ["T1"]})
    monkeypatch.setattr(convergence, "_changed", lambda c, r, t: {"src/calc.py"})
    assert not convergence.mandatory_security(None, {"id": "r", "gates": {}}, {"tasks": ["T1"]})
