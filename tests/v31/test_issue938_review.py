"""#938: new runs self-review in one focused round, Office proves the named test by reverting source,
and security belongs to the convergence reviewer. A run pinned to v3.1 keeps its four-lens contract."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from office import briefs, contract, preflight, self_review_proof, self_review_subagents

NEW_RUN = {"id": "r", "gear": "M", "risk_json": None, "gates": {"review_contract": contract.CONVERGENCE}}
OLD_RUN = {"id": "r", "gear": "M", "risk_json": None, "gates": {"review_contract": contract.LEGACY}}
HEAD = "a" * 40


def _git(wt: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(wt), *args], check=True, capture_output=True, text=True).stdout.strip()


def _ledger(*lines: str, rnd: int = 1, lenses=("edge-cases", "test-strength", "platform")) -> str:
    return "\n".join([f"COMMIT {HEAD}", f"ROUND {rnd}", *(f"LENS {n} reviewed" for n in lenses), *lines]) + "\n"


def _check(text: str, changed=("calc.py",), tmp=Path(".")):
    return preflight.check_ledger(text, HEAD, ["a"], ["calc.py", "tests/**"], tmp, briefs.FOCUSED, list(changed))


# ------------------------------------------------------------------ briefs and tiers

def test_new_runs_take_the_focused_tier_whatever_the_gear_or_risk():
    for gear in ("XS", "M", "XL", "full", "direct"):
        assert briefs.run_self_review_tier({**NEW_RUN, "gear": gear,
                                            "risk_json": '{"irreversible": true}'}) == briefs.FOCUSED


def test_a_v31_run_keeps_the_gear_and_risk_tiers():
    assert briefs.run_self_review_tier({**OLD_RUN, "gear": "XL"}) == "deep"
    assert briefs.run_self_review_tier({**OLD_RUN, "gear": "direct", "risk_json": '{"blast_radius": "local"}'}) == "inline"


def test_the_focused_brief_is_one_subagent_one_round_and_leaves_security_out():
    text = "\n".join(briefs.self_review_lines("abc", briefs.FOCUSED))
    assert "tier: focused" in text and "exactly one subagent" in text and "ROUND 1" in text
    assert "edge cases" in text and "test strength" in text
    assert "Security is not yours" in text
    for gone in ("four parallel", "ROUND <1-3>", "inline tier", "(a) security"):
        assert gone not in text


def test_the_v31_brief_keeps_four_lenses_and_three_rounds():
    text = "\n".join(briefs.self_review_lines("abc", "deep"))
    assert "four parallel subagents" in text and "(a) security" in text and "ROUND <1-3>" in text


def test_the_runtime_review_line_names_one_subagent_and_the_convergence_security_cover():
    new = "\n".join(briefs.runtime_review_lines({**NEW_RUN, "gear": "XL", "gates": {**NEW_RUN["gates"], "gear_model_version": "run-gear-1"}}))
    old = "\n".join(briefs.runtime_review_lines({**OLD_RUN, "gear": "XL", "gates": {"gear_model_version": "run-gear-1"}}))
    assert "one independent self-review subagent" in new and "covers security" in new
    assert "specialist" not in new and "four independent specialist" in old


# ------------------------------------------------------------------ the focused ledger

def test_a_valid_focused_ledger_passes_and_the_round_cap_is_one():
    assert _check(_ledger()) == ([], [])
    stop, fix = _check(_ledger(rnd=2))
    assert any("one round" in f for f in fix)


@pytest.mark.parametrize("lens", ["edge-cases", "test-strength"])
def test_edge_cases_and_test_strength_cannot_be_skipped(lens):
    text = _ledger().replace(f"LENS {lens} reviewed", f"LENS {lens} skipped n/a")
    assert any(f"{lens} must be reviewed" in f for f in _check(text)[1])


def test_platform_may_be_skipped_only_when_the_diff_has_no_platform_file():
    text = _ledger().replace("LENS platform reviewed", "LENS platform skipped none")
    assert _check(text, changed=["calc.py"]) == ([], [])
    assert any("platform must be reviewed" in f for f in _check(text, changed=["scripts/run.sh"])[1])


def test_a_security_lens_line_is_neither_required_nor_an_error():
    """Security is not asked of the executor, and a ledger that still carries the old line is not refused."""
    assert "security" not in "\n".join(briefs.focused_review_lines("abc")).split("LEDGER")[1]
    assert _check(_ledger("LENS security reviewed")) == ([], [])


def test_an_open_high_finding_stops_at_the_one_round_cap():
    stop, _ = _check(_ledger("FINDING high edge-cases calc.py:3 | crashes on empty | open"))
    assert any("round 1 ended with a finding still open" in s for s in stop)


def test_the_same_ledger_under_v31_still_wants_security_and_allows_round_three():
    legacy = "\n".join([f"COMMIT {HEAD}", "ROUND 3", *(f"LENS {n} reviewed" for n in briefs.LEDGER_LENSES)]) + "\n"
    assert preflight.check_ledger(legacy, HEAD, ["a"], ["calc.py"], Path("."), "deep") == ([], [])
    stop, fix = preflight.check_ledger(_ledger(), HEAD, ["a"], ["calc.py"], Path("."), "deep")
    assert any("lens security has no line" in f for f in fix)


# ------------------------------------------------------------------ security by path segment

@pytest.mark.parametrize("path", ["src/auth/login.py", "app/token_store.py", "lib/secrets.py", "api/oauth-client.ts",
                                  "src/Permissions/check.py", "crypto.go"])
def test_a_sensitive_path_word_triggers_the_security_cover(path):
    assert self_review_subagents.security_relevant([path])


@pytest.mark.parametrize("path", ["src/office/product.py", "src/office/writer.py", "src/prod_writer.py",
                                  "src/author.py", "src/tokenizer.py", "src/input_parse.py", "README.md"])
def test_substrings_do_not_trigger_it(path):
    assert not self_review_subagents.security_relevant([path])


def test_a_new_run_funds_one_combined_subagent_whatever_it_touches():
    run = {**NEW_RUN, "gates": {**NEW_RUN["gates"], "gear_model_version": "run-gear-1"}, "gear": "XL"}
    assert self_review_subagents.rubrics(run, {"scope": ["src/auth/x.py", "scripts/a.sh"]}) == ("combined",)
    old = {**run, "gates": {"gear_model_version": "run-gear-1"}}
    assert len(self_review_subagents.rubrics(old, {"scope": ["src/auth/x.py"]})) > 1


# ------------------------------------------------------------------ the revert-and-fail proof

RUNNER = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
FIXED = "def add(a, b):\n    return a + b\n"
BROKEN = "def add(a, b):\n    return 0\n"
GOOD_TEST = "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"


def _repo(tmp_path: Path, test_text: str, source: str = FIXED) -> tuple[Path, str, str]:
    wt = tmp_path / "wt"
    wt.mkdir()
    _git(wt, "init", "-q")
    _git(wt, "config", "user.email", "t@example.test")
    _git(wt, "config", "user.name", "t")
    (wt / "calc.py").write_text(BROKEN)
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "base")
    base = _git(wt, "rev-parse", "HEAD")
    (wt / "calc.py").write_text(source)
    (wt / "tests").mkdir()
    (wt / "tests" / "test_calc.py").write_text(test_text)
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "work")
    return wt, base, _git(wt, "rev-parse", "HEAD")


def _prove(wt, base, head, test="tests/test_calc.py", runner=RUNNER):
    return self_review_proof.prove(wt, base, head, ["calc.py", "tests/test_calc.py"], runner, [test], NEW_RUN)


def test_a_test_that_fails_reverted_and_passes_restored_is_proven(tmp_path):
    wt, base, head = _repo(tmp_path, GOOD_TEST)
    result = _prove(wt, base, head)
    assert result["ok"], result
    assert _git(wt, "status", "--porcelain") == "" and _git(wt, "worktree", "list").count("\n") == 0


def test_a_test_that_passes_without_the_fix_blocks(tmp_path):
    wt, base, head = _repo(tmp_path, "def test_x():\n    assert True\n")
    result = _prove(wt, base, head)
    assert not result["ok"] and "passes without the fix" in result["reason"]


def test_a_collection_error_is_not_a_failing_test(tmp_path):
    """The reverted source lacks the name the test imports: pytest exits 2 at collection, which proves nothing."""
    wt, base, head = _repo(tmp_path, "from calc import extra\n\ndef test_extra():\n    assert extra() == 1\n",
                           source=FIXED + "\ndef extra():\n    return 1\n")
    result = _prove(wt, base, head)
    assert not result["ok"] and "not a test failure" in result["reason"]


def test_a_missing_runner_command_is_not_proof(tmp_path):
    wt, base, head = _repo(tmp_path, GOOD_TEST)
    result = _prove(wt, base, head, runner=["no-such-runner-938", "pytest"])
    assert not result["ok"]


def test_a_named_test_that_is_not_in_the_commit_or_looks_like_an_option_blocks(tmp_path):
    wt, base, head = _repo(tmp_path, GOOD_TEST)
    for bad in ("tests/test_missing.py", "--collect-only", "../outside.py", "/etc/passwd"):
        assert not _prove(wt, base, head, test=bad)["ok"], bad


def test_only_the_ledgers_named_medium_and_high_tests_are_run():
    text = _ledger("FINDING medium edge-cases calc.py:2 | a | fixed tests/test_a.py mutation=failed",
                   "FINDING low edge-cases calc.py:3 | b | fixed tests/test_b.py mutation=failed",
                   "FINDING high test-strength calc.py:4 | c | fixed tests/test_c.py::test_c mutation=failed",
                   "FINDING high test-strength calc.py:5 | d | fixed tests/test_a.py mutation=failed")
    assert self_review_proof.named_tests(text) == ["tests/test_a.py", "tests/test_c.py::test_c"]
    assert self_review_proof.named_tests(None) == []


def test_the_runner_keeps_the_checks_prefix_and_drops_selectors_and_paths():
    task = {"checks": ["uv run --frozen --extra test pytest -n 2 --all -q -k thing -m slow tests/v31/test_x.py"]}
    assert self_review_proof._runner(task) == ["uv", "run", "--frozen", "--extra", "test", "pytest", "--all", "-q"]
    assert self_review_proof._runner({"checks": ["scripts/validate.sh --pr", "make test"]}) is None


def test_no_named_test_or_no_pytest_command_records_an_exemption_not_a_block(tmp_path):
    wt, base, head = _repo(tmp_path, GOOD_TEST)
    task = {"id": "T1", "scope": ["calc.py", "tests/**"], "checks": ["pytest -q"]}
    changed = ["calc.py", "tests/test_calc.py"]
    result = self_review_proof.gate(None, NEW_RUN, task, wt, base, head, changed, execute=True)
    assert result["ok"] and result["status"] == "exempt" and "names no test" in result["reason"]
    (wt / briefs.LEDGER_FILE).write_text(_ledger(
        "FINDING medium edge-cases calc.py:2 | a | fixed tests/test_calc.py mutation=failed"))
    no_runner = {**task, "checks": ["scripts/validate.sh --pr"]}
    result = self_review_proof.gate(None, NEW_RUN, no_runner, wt, base, head, changed, execute=True)
    assert result["ok"] and result["status"] == "exempt" and "no pytest command" in result["reason"]


def test_more_named_tests_than_the_proof_runs_blocks_instead_of_proving_a_subset(tmp_path):
    wt, base, head = _repo(tmp_path, GOOD_TEST)
    task = {"id": "T1", "scope": ["calc.py", "tests/**"], "checks": ["pytest -q"]}
    count = self_review_proof._MAX_TESTS + 1
    (wt / briefs.LEDGER_FILE).write_text(_ledger(*(
        f"FINDING medium edge-cases calc.py:{i} | f{i} | fixed tests/test_calc.py::test_{i} mutation=failed"
        for i in range(count))))
    result = self_review_proof.gate(None, NEW_RUN, task, wt, base, head, ["calc.py", "tests/test_calc.py"],
                                    execute=True)
    assert not result["ok"] and result["status"] == "blocked"
    assert f"names {count} tests" in result["reason"] and f"at most {self_review_proof._MAX_TESTS}" in result["reason"]


def test_a_v31_run_and_a_test_only_change_need_no_proof(tmp_path):
    wt, base, head = _repo(tmp_path, GOOD_TEST)
    task = {"id": "T1", "scope": ["calc.py"], "checks": ["pytest -q"]}
    assert self_review_proof.gate(None, OLD_RUN, task, wt, base, head, ["calc.py"], execute=True)["status"] == "not-required"
    assert self_review_proof.gate(None, NEW_RUN, task, wt, base, head, ["tests/test_calc.py"],
                                  execute=True)["status"] == "not-required"
