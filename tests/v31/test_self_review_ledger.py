"""The executor's self-review findings ledger: what the brief tells the executor to write, what
`office preflight` demands of it, and that submit consumes it without counting it as scope."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from conftest import GOOD_ADD, approved_run, task_row

ROOT = Path(__file__).resolve().parents[2]
EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}
TASK = {"scope": ["calc.py", "tests/**"], "accept": ["calc.add(2, 3) == 5", "no CLI"]}
LENSES = ("security", "edge-cases", "platform", "test-strength")


def _git(wt: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(wt), *args], check=True, capture_output=True, text=True).stdout.strip()


def ledger_text(head: str, *, rnd: int = 1, findings=(), skip=(), drop=()) -> str:
    lines = [f"COMMIT {head}", f"ROUND {rnd}"]
    for lens in LENSES:
        if lens in drop:
            continue
        lines.append(f"LENS {lens} skipped {skip[lens] if isinstance(skip, dict) else 'n/a'}" if lens in skip
                     else f"LENS {lens} reviewed")
    return "\n".join(lines + list(findings)) + "\n"


def write_ledger(wt: Path, text: str | None = None, **kw) -> Path:
    from office import briefs
    path = wt / briefs.LEDGER_FILE
    path.write_text(text if text is not None else ledger_text(_git(wt, "rev-parse", "HEAD"), **kw))
    return path


@pytest.fixture
def repo(tmp_path):
    """A worktree whose base commit holds README.md and whose HEAD adds in-scope calc.py and a test."""
    wt = tmp_path / "wt"
    wt.mkdir()
    _git(wt, "init", "-q")
    _git(wt, "config", "user.email", "t@example.test")
    _git(wt, "config", "user.name", "t")
    (wt / "README.md").write_text("x\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "base")
    (wt / "calc.py").write_text(GOOD_ADD)
    (wt / "tests").mkdir()
    (wt / "tests" / "test_calc.py").write_text("def test_x():\n    pass\n")
    commit_all(wt, "work")
    return wt


def commit_all(wt: Path, msg: str = "more") -> None:
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", msg)


def verdict(wt: Path, task=TASK, tier="deep"):
    """What `office preflight` computes: the committed diff since the first commit, then the ledger verdict."""
    from office import preflight
    base = _git(wt, "rev-list", "--max-parents=0", "HEAD")
    changed = [f for f in _git(wt, "diff", "--name-only", "-z", base, "HEAD").split("\0") if f]
    return preflight.ledger_verdict(wt, task, changed, _git(wt, "rev-parse", "HEAD"), tier)


def fixes(wt: Path, text: str, **kw) -> list[str]:
    write_ledger(wt, text)
    stop, fix = verdict(wt, **kw)
    assert not stop, stop
    return fix


# ------------------------------------------------------------------ when a ledger is owed

def test_no_ledger_with_a_nonempty_diff_is_a_fix(repo):
    stop, fix = verdict(repo)
    assert stop == [] and len(fix) == 1 and "no OFFICE_SELF_REVIEW.md" in fix[0], fix


def test_empty_diff_needs_no_ledger(tmp_path):
    wt = tmp_path / "empty"
    wt.mkdir()
    _git(wt, "init", "-q")
    _git(wt, "-c", "user.email=t@e.test", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "base")
    assert verdict(wt) == ([], [])
    (wt / "stray.txt").write_text("x\n")  # out of scope: still nothing to review
    assert verdict(wt) == ([], [])


def test_scope_none_needs_no_ledger_even_with_changes(repo):
    assert verdict(repo, task={"scope": [], "accept": []}) == ([], [])


def test_only_out_of_scope_changes_need_no_ledger(repo):
    assert verdict(repo, task={"scope": ["docs/**"], "accept": []}) == ([], [])


def test_an_in_scope_harness_file_edit_still_owes_a_ledger(tmp_path):
    wt = tmp_path / "h"
    wt.mkdir()
    _git(wt, "init", "-q")
    _git(wt, "-c", "user.email=t@e.test", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "base")
    (wt / ".claude").mkdir()
    (wt / ".claude" / "settings.json").write_text("{}")
    stop, fix = verdict(wt, task={"scope": [".claude/**"], "accept": []})
    assert any("uncommitted in-scope changes (.claude/settings.json)" in f for f in fix) and any("no OFFICE_SELF" in f for f in fix)
    commit_all(wt)
    stop, fix = verdict(wt, task={"scope": [".claude/**"], "accept": []})
    assert fix == ["ledger: no OFFICE_SELF_REVIEW.md; run the SELF-REVIEW and write it in this worktree root in the format "
                   "the brief gives"], fix


@pytest.mark.parametrize("how", ["edit", "new-file"])
def test_uncommitted_in_scope_work_is_a_fix_even_with_a_current_ledger(repo, how):
    write_ledger(repo)
    assert verdict(repo) == ([], [])
    if how == "edit":
        (repo / "calc.py").write_text(GOOD_ADD + "\n")
    else:
        (repo / "tests" / "test_new.py").write_text("x\n")
    stop, fix = verdict(repo)
    assert stop == [] and len(fix) == 1 and "uncommitted in-scope changes" in fix[0] and "commit them first" in fix[0], fix


def test_uncommitted_out_of_scope_files_are_not_a_fix(repo):
    write_ledger(repo)
    (repo / "notes.txt").write_text("x\n")
    assert verdict(repo) == ([], [])


def test_the_ledger_alone_is_not_a_change(tmp_path):
    wt = tmp_path / "l"
    wt.mkdir()
    _git(wt, "init", "-q")
    _git(wt, "-c", "user.email=t@e.test", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "base")
    write_ledger(wt, "garbage\n")
    assert verdict(wt, task={"scope": ["**"], "accept": []}) == ([], [])


# ------------------------------------------------------------------ ready, stale, lenses

def test_all_dispositions_resolved_is_ready(repo):
    findings = [
        "FINDING high security calc.py:3 | add overflows | fixed tests/test_calc.py mutation=failed",
        "FINDING medium security calc.py:4 | add drops negatives | fixed tests/test_calc.py mutation=failed",
        "FINDING low security calc.py:5 | naming | fixed",
        "FINDING medium security README.md:1 | doc drift | out-of-scope",
        "FINDING low security calc.py:6 | false positive | dismissed the call never happens",
    ]
    write_ledger(repo, ledger_text(_git(repo, "rev-parse", "HEAD"), findings=findings, skip={"platform": "pure python"}))
    assert verdict(repo, tier="inline") == ([], [])


def test_a_ledger_with_no_findings_is_ready(repo):
    write_ledger(repo)
    assert verdict(repo) == ([], [])


def test_blank_lines_and_indentation_are_fine(repo):
    head = _git(repo, "rev-parse", "HEAD")
    write_ledger(repo, "\n  COMMIT " + head + "\n\nROUND 1\n" + "".join(f"  LENS {x} reviewed\n" for x in LENSES))
    assert verdict(repo) == ([], [])


def test_ledger_naming_a_stale_commit_is_a_fix(repo):
    stale = _git(repo, "rev-parse", "HEAD")
    write_ledger(repo)
    (repo / "calc.py").write_text(GOOD_ADD + "\n")
    _git(repo, "add", "calc.py")
    _git(repo, "commit", "-qm", "later")
    stop, fix = verdict(repo)
    head = _git(repo, "rev-parse", "HEAD")
    assert stop == [] and len(fix) == 1, fix
    assert f"names commit {stale[:12]} but HEAD is {head[:12]}" in fix[0] and "stale" in fix[0], fix


def test_commit_in_upper_case_is_the_same_sha(repo):
    write_ledger(repo, ledger_text(_git(repo, "rev-parse", "HEAD").upper()))
    assert verdict(repo) == ([], [])


@pytest.mark.parametrize("length", [7, 12, 39])
def test_a_commit_prefix_of_head_is_malformed_not_current(repo, length):
    head = _git(repo, "rev-parse", "HEAD")
    write_ledger(repo, ledger_text(head[:length]))
    stop, fix = verdict(repo)
    assert stop == [], stop
    assert any(f.startswith("ledger line 1:") and "COMMIT needs the full sha of HEAD" in f for f in fix), fix
    assert any("no COMMIT line" in f for f in fix), "a prefix must not count as naming HEAD"


@pytest.mark.parametrize("length", [41, 63, 65])
def test_a_commit_that_is_no_sha_length_is_malformed(repo, length):
    write_ledger(repo, ledger_text("a" * length))
    stop, fix = verdict(repo)
    assert any(f.startswith("ledger line 1:") and "COMMIT needs the full sha of HEAD" in f for f in fix), fix


def test_a_sha256_commit_is_read_and_compared_in_full(repo):
    from office import preflight
    head = "ab" * 32
    ok = ledger_text(head)
    assert preflight.parse_ledger(ok)[1] == []
    assert preflight.check_ledger(ok, head, [], ["calc.py"], repo) == ([], [])
    stop, fix = preflight.check_ledger(ok, head[:-1] + "c", [], ["calc.py"], repo)
    assert stop == [] and len(fix) == 1 and "but HEAD is" in fix[0], fix
    stop, fix = preflight.check_ledger(ledger_text(head[:40]), head, [], ["calc.py"], repo)
    assert stop == [] and len(fix) == 1 and "but HEAD is" in fix[0], "a 40-hex prefix of a sha256 head is stale, not current"


def test_a_wrong_sha_that_looks_valid_is_stale(repo):
    stop, fix = verdict(write_ledger(repo, ledger_text("0" * 40)).parent)
    assert any("but HEAD is" in f for f in fix), fix


@pytest.mark.parametrize("lens", LENSES)
def test_a_lens_missing_without_a_skip_reason_is_a_fix(repo, lens):
    write_ledger(repo, drop=(lens,))
    stop, fix = verdict(repo)
    assert stop == [] and len(fix) == 1 and f"lens {lens} has no line" in fix[0], fix


def test_a_skipped_lens_needs_its_reason(repo):
    head = _git(repo, "rev-parse", "HEAD")
    text = ledger_text(head, drop=("platform",)) + "LENS platform skipped\n"
    fix = fixes(repo, text)
    assert any("line 6" in f and "LENS needs" in f for f in fix), fix
    assert any("lens platform has no line" in f for f in fix), fix


# ------------------------------------------------------------------ findings

@pytest.mark.parametrize("severity", ["high", "medium", "low"])
def test_an_open_finding_is_a_fix_naming_it(repo, severity):
    head = _git(repo, "rev-parse", "HEAD")
    fix = fixes(repo, ledger_text(head, findings=[f"FINDING {severity} security calc.py:9 | add skips zero | open"]))
    assert len(fix) == 1 and f"{severity} calc.py:9 add skips zero is open" in fix[0], fix
    assert "`fixed <test path> mutation=failed`" in fix[0] and "`dismissed <reason>`" in fix[0], fix


def test_a_medium_fixed_without_a_test_path_is_a_fix(repo):
    head = _git(repo, "rev-parse", "HEAD")
    for sev in ("medium", "high"):
        fix = fixes(repo, ledger_text(head, findings=[f"FINDING {sev} security calc.py:9 | add skips zero | fixed"]))
        assert len(fix) == 1 and "without a test path" in fix[0] and "calc.py:9" in fix[0], fix


@pytest.mark.parametrize("path", ["tests/missing.py", "../outside.py", "/etc/hosts", "tests"])
def test_a_fixed_medium_must_name_a_real_test_file_inside_the_worktree(repo, path, tmp_path):
    (tmp_path / "outside.py").write_text("x\n")
    head = _git(repo, "rev-parse", "HEAD")
    fix = fixes(repo, ledger_text(head, findings=[f"FINDING medium security calc.py:9 | add skips zero | fixed {path}"]))
    assert len(fix) == 1 and "is not a file in this worktree" in fix[0], fix


def test_a_low_fix_may_omit_its_test(repo):
    head = _git(repo, "rev-parse", "HEAD")
    assert fixes(repo, ledger_text(head, findings=["FINDING low security calc.py:9 | nit | fixed"])) == []


def test_a_fixed_medium_or_high_needs_the_mutation_result(repo):
    head = _git(repo, "rev-parse", "HEAD")
    for sev in ("medium", "high"):
        fix = fixes(repo, ledger_text(head, findings=[f"FINDING {sev} security calc.py:9 | add skips zero | fixed tests/test_calc.py"]))
        assert len(fix) == 1 and "no mutation proof" in fix[0] and "mutation=failed" in fix[0] and "calc.py:9" in fix[0], fix
    ok = ledger_text(head, findings=["FINDING high security calc.py:9 | add skips zero | fixed tests/test_calc.py mutation=failed"])
    assert fixes(repo, ok) == []


@pytest.mark.parametrize("gap", ["  ", "\t", " \t ", "   "])
def test_extra_whitespace_between_the_test_and_the_mutation_result_is_the_same_proof(repo, gap):
    head = _git(repo, "rev-parse", "HEAD")
    line = f"FINDING high security calc.py:9 | add skips zero | fixed tests/test_calc.py{gap}mutation=failed"
    assert fixes(repo, ledger_text(head, findings=[line])) == []


def test_a_fixed_medium_or_high_test_must_be_tracked_by_git(repo):
    (repo / "tests" / "test_new.py").write_text("def test_n():\n    pass\n")  # out of nothing: untracked
    head = _git(repo, "rev-parse", "HEAD")
    fix = fixes(repo, ledger_text(head, findings=["FINDING high security calc.py:9 | x | fixed tests/test_new.py mutation=failed"]))
    assert any("HEAD does not contain" in f and "test_new.py" in f for f in fix), fix


def test_a_staged_but_uncommitted_test_is_not_committed(repo):
    (repo / "tests" / "test_new.py").write_text("def test_n():\n    pass\n")
    _git(repo, "add", "tests/test_new.py")
    head = _git(repo, "rev-parse", "HEAD")
    fix = fixes(repo, ledger_text(head, findings=["FINDING high security calc.py:9 | x | fixed tests/test_new.py mutation=failed"]))
    assert any("HEAD does not contain" in f for f in fix), fix


def test_a_glob_named_untracked_file_is_not_the_tracked_file_it_matches(repo):
    (repo / "tests" / "test_a1.py").write_text("def test_a():\n    pass\n")
    commit_all(repo, "a1")
    (repo / "tests" / "test_a[1].py").write_text("def test_a():\n    pass\n")
    head = _git(repo, "rev-parse", "HEAD")
    fix = fixes(repo, ledger_text(head, findings=["FINDING high security calc.py:9 | x | fixed tests/test_a[1].py mutation=failed"]))
    assert any("HEAD does not contain" in f for f in fix), fix


def test_a_symlink_loop_as_the_test_is_a_fix_not_a_crash(repo, monkeypatch):
    """Path.resolve() raises RuntimeError on a loop before Python 3.13; force it on every version."""
    from office import preflight
    os.symlink("test_loop.py", repo / "tests" / "test_loop.py")
    commit_all(repo, "loop")

    def boom(self, *a, **k):
        raise RuntimeError("Symlink loop")

    monkeypatch.setattr(Path, "resolve", boom)
    assert preflight._test_file_exists(repo, "tests/test_loop.py") is False
    assert preflight._names_a_test(repo, "tests/test_loop.py") is False
    assert preflight._finding_file(repo, str(repo / "tests" / "test_loop.py") + ":3") is None


def test_a_node_id_must_name_something_in_the_file(repo):
    (repo / "tests" / "test_x.py").write_text("def test_real():\n    pass\n")
    commit_all(repo, "x")
    head = _git(repo, "rev-parse", "HEAD")
    good = "FINDING high security calc.py:9 | x | fixed tests/test_x.py::test_real mutation=failed"
    param = "FINDING high security calc.py:9 | x | fixed tests/test_x.py::test_real[a-b] mutation=failed"
    nope = "FINDING high security calc.py:9 | x | fixed tests/test_x.py::test_missing mutation=failed"
    assert fixes(repo, ledger_text(head, findings=[good, param])) == []
    fix = fixes(repo, ledger_text(head, findings=[nope]))
    assert len(fix) == 1 and "not a test file" in fix[0], fix


def test_a_path_with_dot_segments_is_the_same_committed_file(repo):
    head = _git(repo, "rev-parse", "HEAD")
    line = "FINDING high security calc.py:9 | x | fixed tests/../tests/test_calc.py mutation=failed"
    assert fixes(repo, ledger_text(head, findings=[line])) == []


def test_a_test_named_link_to_a_non_test_file_is_not_a_test(repo):
    os.symlink("../calc.py", repo / "tests" / "test_link.py")
    commit_all(repo, "link")
    head = _git(repo, "rev-parse", "HEAD")
    fix = fixes(repo, ledger_text(head, findings=["FINDING high security calc.py:9 | x | fixed tests/test_link.py mutation=failed"]))
    assert len(fix) == 1 and "not a test file" in fix[0], fix


@pytest.mark.parametrize("path", ["README.md", "calc.py", "tests/notes.md", "tests/data.json", "docs/test_plan.md"])
def test_a_fixed_medium_or_high_must_name_a_test_file_not_any_file(repo, path):
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "tests" / "notes.md").write_text("n\n")
    (repo / "tests" / "data.json").write_text("{}\n")
    (repo / "docs" / "test_plan.md").write_text("p\n")
    commit_all(repo, "files")
    head = _git(repo, "rev-parse", "HEAD")
    for sev in ("medium", "high"):
        fix = fixes(repo, ledger_text(head, findings=[f"FINDING {sev} security calc.py:9 | add skips zero | fixed {path} mutation=failed"]))
        assert len(fix) == 1 and "not a test file" in fix[0] and path in fix[0], fix


def test_a_low_fix_needs_neither_test_nor_mutation_but_a_given_mutation_must_be_well_formed(repo):
    head = _git(repo, "rev-parse", "HEAD")
    assert fixes(repo, ledger_text(head, findings=["FINDING low security calc.py:9 | nit | fixed README.md"])) == []
    fix = fixes(repo, ledger_text(head, findings=["FINDING low security calc.py:9 | nit | fixed tests/test_calc.py mutation=passed"]))
    assert len(fix) == 1 and fix[0].startswith("ledger line 7:") and "mutation=failed" in fix[0], fix


@pytest.mark.parametrize("name, ok", [
    ("tests/test_calc.py", True), ("test_calc.py", True), ("tests/test_calc.py::test_x", True), ("pkg/calc_test.go", True),
    ("src/calc.test.ts", True), ("web/calc.spec.js", True), ("__tests__/calc.js", True), ("spec/calc_spec.rb", True),
    ("tests/helpers/util.py", True), ("test.py", True), ("src/calc-test.js", True), ("src/test-calc.js", True),
    ("lib/calc_spec.rb", True), ("web/calc-spec.ts", True), ("Calc.Tests/CalcTests.cs", True), ("Calc.Tests/Helpers.cs", True), ("calc-tests/util.go", True), ("src/CalcTest.java", True),
    ("cypress/e2e/calc.cy.ts", True), ("e2e/calc.ts", True), ("src/calc.rs::tests::adds", True), ("src/calc.tests.ts", True),
    ("README.md", False), ("calc.py", False), ("tests/NOTES.md", False), ("tests/data.json", False), ("docs/test_plan.md", False),
    ("tests/__init__.py", False), ("tests/conftest.py", False), ("tests/a.svg", False), ("tests/a.jpeg", False),
    ("tests/report.html", False), ("src/calc.rs", False), ("src/calc.rs::adds", False), ("src/Contest.java", False),
    ("tests/data.csv", False), ("tests/data.jsonl", False), ("tests/q.sql", False), ("tests/fixtures/x.bin", False), ("tests/__snapshots__/a.snap", False), ("src/spec.py", False),
    ("src/contest.py", False), ("latest/calc.py", False), ("tests", False), ("", False),
])
def test_which_paths_count_as_a_test_file(name, ok):
    from office import preflight
    assert preflight._is_test_path(name) is ok


@pytest.mark.parametrize("lens", LENSES)
def test_every_lens_may_name_a_finding(repo, lens):
    head = _git(repo, "rev-parse", "HEAD")
    assert fixes(repo, ledger_text(head, findings=[f"FINDING low {lens} calc.py:9 | nit | fixed"])) == []


@pytest.mark.parametrize("sev", ["high", "medium"])
@pytest.mark.parametrize("location", ["calc.py:3", "./calc.py:3", "tests/../calc.py:3", "tests/test_calc.py:2",
                                      "calc.py:3-9", "@ABS@/calc.py:3"])
def test_a_blocker_marked_out_of_scope_must_be_outside_scope(repo, sev, location):
    head = _git(repo, "rev-parse", "HEAD")
    location = location.replace("@ABS@", str(repo.resolve()))
    fix = fixes(repo, ledger_text(head, findings=[f"FINDING {sev} security {location} | add skips zero | out-of-scope"]))
    assert len(fix) == 1 and "marked out-of-scope, but" in fix[0] and "is inside SCOPE" in fix[0], fix
    assert "dismissed <reason>" in fix[0] and "mutation=failed" in fix[0], fix


def _case_insensitive(wt: Path) -> bool:
    return (wt / "CALC.PY").exists()


@pytest.mark.parametrize("sev", ["high", "medium"])
def test_on_a_case_insensitive_checkout_another_casing_of_an_in_scope_file_is_in_scope(repo, sev):
    if not _case_insensitive(repo):
        pytest.skip("case-sensitive filesystem")
    head = _git(repo, "rev-parse", "HEAD")
    fix = fixes(repo, ledger_text(head, findings=[f"FINDING {sev} security CALC.PY:3 | x | out-of-scope"]))
    assert len(fix) == 1 and "is inside SCOPE" in fix[0], fix


def test_in_scope_fallback_judges_the_name_git_tracks(repo, monkeypatch):
    """Platform independent: the filesystem and git answers are forced."""
    from office import preflight
    monkeypatch.setattr(Path, "exists", lambda self: True)
    monkeypatch.setattr(preflight.os.path, "samefile", lambda a, b: True)
    tracked = {"calc.py": "calc.py\0README.md\0", ":(literal)Calc.PY": ""}

    def fake(wt, *args, **kw):
        return tracked.get(args[-1], "") if "--" in args else tracked["calc.py"]

    monkeypatch.setattr(preflight.paths, "git", fake)
    assert preflight._in_scope(repo, "Calc.PY", ["calc.py"]) is True      # another casing of the in-scope file
    assert preflight._in_scope(repo, "README.MD", ["calc.py"]) is False   # another casing of an out-of-scope file
    tracked[":(literal)Calc.py"] = "Calc.py\0"                             # git tracks this exact name: its own file
    assert preflight._in_scope(repo, "Calc.py", ["calc.py"]) is False
    monkeypatch.setattr(Path, "exists", lambda self: False)               # no such file on this filesystem
    assert preflight._in_scope(repo, "Calc.PY", ["calc.py"]) is False
    monkeypatch.setattr(Path, "exists", lambda self: True)
    monkeypatch.setattr(preflight.os.path, "samefile", lambda a, b: False)  # a different file that differs by case
    assert preflight._in_scope(repo, "Calc.PY", ["calc.py"]) is False


def test_on_a_case_sensitive_checkout_another_casing_is_another_file(repo):
    if _case_insensitive(repo):
        pytest.skip("case-insensitive filesystem")
    head = _git(repo, "rev-parse", "HEAD")
    assert fixes(repo, ledger_text(head, findings=["FINDING high security CALC.PY:3 | x | out-of-scope"])) == []


@pytest.mark.parametrize("location", ["README.md:1", "docs/guide.md:4", "../sibling/calc.py:3", "/etc/hosts:1", "src/calc.py:3"])
def test_a_blocker_whose_file_is_outside_scope_may_be_out_of_scope(repo, location):
    head = _git(repo, "rev-parse", "HEAD")
    for sev in ("high", "medium"):
        assert fixes(repo, ledger_text(head, findings=[f"FINDING {sev} security {location} | doc drift | out-of-scope"])) == []


def test_a_low_finding_may_be_out_of_scope_whatever_its_file(repo):
    head = _git(repo, "rev-parse", "HEAD")
    assert fixes(repo, ledger_text(head, findings=["FINDING low security calc.py:3 | nit | out-of-scope"])) == []


def test_a_blocker_may_be_dismissed_with_a_reason(repo):
    head = _git(repo, "rev-parse", "HEAD")
    assert fixes(repo, ledger_text(head, findings=["FINDING high security calc.py:3 | maybe | dismissed the call never happens"])) == []


# ------------------------------------------------------------------ stops

def test_contract_conflict_stops_with_the_accept_line_quoted(repo):
    head = _git(repo, "rev-parse", "HEAD")
    write_ledger(repo, ledger_text(head, findings=["FINDING high security calc.py:2 | add must reject ints | contract-conflict accept=2"]))
    stop, fix = verdict(repo)
    assert fix == [] and len(stop) == 1, (stop, fix)
    assert 'would break ACCEPT 2: "no CLI"' in stop[0] and "calc.py:2" in stop[0], stop


def test_contract_conflict_naming_no_accept_line_is_a_fix(repo):
    head = _git(repo, "rev-parse", "HEAD")
    fix = fixes(repo, ledger_text(head, findings=["FINDING high security calc.py:2 | x | contract-conflict accept=3"]))
    assert len(fix) == 1 and "accept=3" in fix[0] and "2 ACCEPT lines" in fix[0], fix


@pytest.mark.parametrize("sev, kind", [("medium", "stop"), ("high", "stop"), ("low", "fix")])
def test_round_three_with_an_open_finding(repo, sev, kind):
    head = _git(repo, "rev-parse", "HEAD")
    write_ledger(repo, ledger_text(head, rnd=3, findings=[f"FINDING {sev} security calc.py:9 | add skips zero | open"]))
    stop, fix = verdict(repo)
    if kind == "stop":
        assert fix == [] and len(stop) == 1 and "round 3 ended with a finding still open" in stop[0], (stop, fix)
        assert f"{sev} calc.py:9 add skips zero" in stop[0]
    else:
        assert stop == [] and len(fix) == 1, (stop, fix)


@pytest.mark.parametrize("rnd", [1, 2])
def test_an_open_blocker_before_round_three_is_a_fix_not_a_stop(repo, rnd):
    head = _git(repo, "rev-parse", "HEAD")
    write_ledger(repo, ledger_text(head, rnd=rnd, findings=["FINDING medium security calc.py:9 | x | open"]))
    stop, fix = verdict(repo)
    assert stop == [] and len(fix) == 1, (stop, fix)


def test_round_three_with_everything_resolved_is_ready(repo):
    head = _git(repo, "rev-parse", "HEAD")
    write_ledger(repo, ledger_text(head, rnd=3, findings=["FINDING high security calc.py:9 | x | fixed tests/test_calc.py mutation=failed"]))
    assert verdict(repo) == ([], [])


# ------------------------------------------------------------------ malformed lines

MALFORMED = [
    "# a comment",
    "NOTE all fine",
    "FINDING",
    "FINDING medium security calc.py:1",
    "FINDING medium security calc.py:1 | no disposition",
    "FINDING critical security calc.py:1 | s | open",
    "FINDING Medium security calc.py:1 | s | open",
    "FINDING medium security calc.py:1 and more | s | open",
    "FINDING medium security  | s | open",
    "FINDING medium security calc.py:1 |  | open",
    "FINDING medium security calc.py:1 | s | maybe",
    "FINDING medium security calc.py:1 | s | open now",
    "FINDING medium security calc.py:1 | s | fixed a.py b.py",
    "FINDING medium security calc.py:1 | s | fixed tests/test_calc.py mutation=passed",
    "FINDING medium security calc.py:1 | s | fixed tests/test_calc.py mutation=failed extra",
    "FINDING medium security calc.py:1 | s | fixed mutation=failed",
    "FINDING medium security calc.py:1 | s | rejected nope",
    "FINDING medium calc.py:1 | s | open",
    "FINDING medium crypto calc.py:1 | s | open",
    "FINDING medium security calc.py | s | open",
    "FINDING medium security calc.py:x | s | open",
    "FINDING medium security :3 | s | open",
    "FINDING medium security calc.py:1 | s | dismissed",
    "FINDING medium security calc.py:1 | s | contract-conflict",
    "FINDING medium security calc.py:1 | s | contract-conflict accept=0",
    "FINDING medium security calc.py:1 | s | contract-conflict accept=x",
    "COMMIT",
    "COMMIT nothex1",
    "COMMIT abc",
    "COMMIT abcdef1",
    "ROUND 0",
    "ROUND 4",
    "ROUND two",
    "ROUND ²",
    "LENS",
    "LENS security",
    "LENS security done",
    "LENS security reviewed extra",
    "LENS crypto reviewed",
    "lens security reviewed",
]


@pytest.mark.parametrize("bad", MALFORMED)
def test_a_malformed_line_is_a_fix_with_its_line_number_and_never_ready(repo, bad):
    head = _git(repo, "rev-parse", "HEAD")
    text = ledger_text(head) + bad + "\n"
    n = text.splitlines().index(bad) + 1
    fix = fixes(repo, text)
    assert any(f.startswith(f"ledger line {n}:") for f in fix), (bad, fix)


@pytest.mark.parametrize("line, why", [
    ("FINDING low calc.py:9 | nit | fixed", "lens must be one of"),
    ("FINDING low crypto calc.py:9 | nit | fixed", "lens must be one of"),
    ("FINDING low Security calc.py:9 | nit | fixed", "lens must be one of"),
    ("FINDING low security calc.py | nit | fixed", "location must be one word, file:line"),
    ("FINDING low security calc.py:x | nit | fixed", "location must be one word, file:line"),
    ("FINDING low security calc.py:9 | nit | rejected because", "disposition must be one of"),
    ("FINDING low security calc.py:9 | nit | dismissed", "`dismissed` needs a reason"),
    ("FINDING low security calc.py:9 | nit | fixed tests/test_calc.py mutation=passed", "mutation result must be"),
    ("FINDING low security calc.py:9 | nit | fixed mutation=failed", "names the test path before"),
])
def test_a_finding_line_is_rejected_for_its_own_reason_not_just_flagged_as_something(repo, line, why):
    head = _git(repo, "rev-parse", "HEAD")
    fix = fixes(repo, ledger_text(head) + line + "\n")
    assert len(fix) == 1 and fix[0].startswith("ledger line 7:") and why in fix[0], fix


@pytest.mark.parametrize("dup", ["COMMIT {head}", "ROUND 2", "LENS security reviewed", "LENS security skipped why"])
def test_a_repeated_header_or_lens_line_is_malformed(repo, dup):
    head = _git(repo, "rev-parse", "HEAD")
    text = ledger_text(head) + dup.format(head=head) + "\n"
    n = len(text.splitlines())
    assert any(f.startswith(f"ledger line {n}:") for f in fixes(repo, text))


def test_malformed_lines_do_not_hide_a_stop(repo):
    head = _git(repo, "rev-parse", "HEAD")
    text = ledger_text(head, findings=["junk", "FINDING high security calc.py:2 | s | contract-conflict accept=1"])
    write_ledger(repo, text)
    stop, fix = verdict(repo)
    assert len(stop) == 1 and any("line 7" in f for f in fix), (stop, fix)


def test_every_malformed_line_is_listed_not_only_the_first(repo):
    head = _git(repo, "rev-parse", "HEAD")
    fix = fixes(repo, ledger_text(head, findings=["junk one", "junk two"]))
    assert [f for f in fix if "unknown line" in f] and len([f for f in fix if "unknown line" in f]) == 2, fix


@pytest.mark.parametrize("text", ["", "\n\n", "   \n"])
def test_an_empty_ledger_is_never_ready(repo, text):
    fix = fixes(repo, text)
    assert any("no COMMIT line" in f for f in fix) and any("no ROUND line" in f for f in fix), fix
    assert sum("has no line" in f for f in fix) == 4, fix


def test_a_ledger_with_findings_but_no_header_is_never_ready(repo):
    fix = fixes(repo, "FINDING low security calc.py:1 | s | fixed\n")
    assert fix, "a header-less ledger must not be ready"


# ------------------------------------------------------------------ unsafe or oversized files

def test_a_symlinked_ledger_is_refused_not_read(repo, tmp_path):
    from office import briefs
    secret = tmp_path / "secret.txt"
    secret.write_text(ledger_text(_git(repo, "rev-parse", "HEAD")))
    os.symlink(secret, repo / briefs.LEDGER_FILE)
    stop, fix = verdict(repo)
    assert stop == [] and len(fix) == 1 and "regular untracked file" in fix[0], fix


def test_a_hard_linked_ledger_is_refused(repo, tmp_path):
    from office import briefs
    other = tmp_path / "other.txt"
    other.write_text(ledger_text(_git(repo, "rev-parse", "HEAD")))
    os.link(other, repo / briefs.LEDGER_FILE)
    stop, fix = verdict(repo)
    assert len(fix) == 1 and "regular untracked file" in fix[0], fix


def test_a_committed_ledger_is_refused(repo):
    from office import briefs
    write_ledger(repo)
    _git(repo, "add", briefs.LEDGER_FILE)
    _git(repo, "commit", "-qm", "oops")
    stop, fix = verdict(repo)
    assert len(fix) == 1 and "git rm --cached" in fix[0], fix


def test_an_oversized_ledger_is_refused_not_truncated(repo):
    from office import briefs
    head = _git(repo, "rev-parse", "HEAD")
    pad = "FINDING low security calc.py:1 | " + "x" * 200 + " | fixed\n"
    write_ledger(repo, ledger_text(head) + pad * (briefs.LEDGER_MAX_CHARS // len(pad) + 5))
    stop, fix = verdict(repo)
    assert len(fix) == 1 and "over" in fix[0] and "characters" in fix[0], fix


# ------------------------------------------------------------------ brief, skill, SKILL.md agree

RULES = ("OFFICE_SELF_REVIEW.md", "severity as found", "do not trigger a re-review", "fix-diff re-review",
         "3-round cap", "contract-conflict", "consumes", "ACCEPT line", "mutation=failed")


def _brief(tier: str) -> str:
    from office import briefs
    gear, risk = {"inline": ("direct", '{"blast_radius": "local"}'), "single": ("quick", '{"blast_radius": "local"}'),
                  "deep": ("full", '{"blast_radius": "production"}')}[tier]
    packet = {"task_id": "T1", "title": "x", "scope": ["a.py"], "plan_version": 1, "requirements_version": 1,
              "base_commit": "abc123"}
    return briefs.executor_brief(None, {"gear": gear, "risk_json": risk}, packet)


@pytest.mark.parametrize("tier", ["inline", "single", "deep"])
def test_brief_self_review_block_states_the_ledger_and_the_round_rules(tier):
    from office import briefs
    brief = _brief(tier)
    block = brief[brief.index("SELF-REVIEW before submitting"):brief.index("WHEN DONE run: office preflight")]
    for rule in RULES:
        assert rule in block, (tier, rule)
    for needle in ("COMMIT <full sha of HEAD>", "ROUND <1-3>", "LENS <security|edge-cases|platform|test-strength> reviewed",
                   "LENS <lens> skipped <reason>", "FINDING <high|medium|low> <security|edge-cases|platform|test-strength> <file:line> | <summary> | <disposition>",
                   "write it fresh every round", "after your last commit"):
        assert needle in block, (tier, needle)
    for disposition in briefs.LEDGER_DISPOSITIONS:
        assert f"`{disposition}" in block, (tier, disposition)
    assert "subagent" not in block.lower() or tier != "inline"


def test_fix_round_brief_carries_the_same_ledger_block():
    from office import briefs
    packet = {"task_id": "T1", "title": "x", "scope": ["a.py"], "plan_version": 1, "requirements_version": 1,
              "base_commit": "abc123", "fix_of": "R1"}

    class _Con:
        def execute(self, *a, **k):
            class _R:
                def fetchall(self_inner):
                    return []
            return _R()

    brief = briefs.executor_brief(_Con(), {"id": "r", "gear": "full", "risk_json": None}, packet)
    assert "FIX ROUND for revision R1" in brief
    for rule in RULES:
        assert rule in brief, rule


def test_office_submit_step_2_and_the_hub_state_the_same_rules_as_the_brief():
    skill = (ROOT / "skills/office-submit/SKILL.md").read_text()
    step = skill[skill.index("## 2. Adversarial self-review"):skill.index("## 3. Checks")]
    hub = (ROOT / "SKILL.md").read_text()
    line = hub[hub.index("- Executors simplify"):hub.index("- Before submitting a plan inline")]
    for name, text in (("office-submit step 2", step), ("SKILL.md executor line", line)):
        flat = " ".join(text.split()).lower()
        for rule in RULES:
            assert rule.lower() in flat, (name, rule)
        assert "preflight" in flat, name
    for disposition in ("fixed <test path> mutation=failed", "out-of-scope", "dismissed <reason>", "contract-conflict accept=<n>", "open"):
        assert disposition in step, disposition
    assert "FINDING <severity> <lens> <file:line> | <summary> | <disposition>" in " ".join(step.split())
    assert "outside SCOPE" in step


# ------------------------------------------------------------------ through the real preflight and submit

def _dispatch(env, plan=None):
    kw = {"plan": plan} if plan else {}
    approved_run(env, executor=[{}], code_reviewer=[{"reply": "VERDICT: PASS"}], **kw)
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    con = env.con()
    t = task_row(env, "T1")
    d = dict(con.execute("SELECT * FROM dispatches WHERE id=?", (t["current_dispatch_id"],)).fetchone())
    wenv = {"OFFICE_RUN_ID": d["run_id"], "OFFICE_DISPATCH_ID": d["id"], "OFFICE_TASK_ID": "T1",
            "OFFICE_ROLE": "executor", "OFFICE_JOBS": "manual"}
    wt = Path(d["worktree"])
    (wt / "calc.py").write_text(GOOD_ADD)
    _git(wt, "add", "calc.py")
    _git(wt, "-c", "user.email=t@e.test", "-c", "user.name=t", "commit", "-qm", "calc")  # the ledger names HEAD
    return wenv, wt, d


@pytest.mark.integration
def test_preflight_walks_missing_stale_open_then_ready_and_submit_consumes_the_ledger(env):
    from conftest import PLAN_ONE
    from office import briefs
    wenv, wt, d = _dispatch(env, plan=PLAN_ONE.replace("scope: calc.py", "scope: calc.py, tests/**"))
    (wt / "tests").mkdir()
    (wt / "tests" / "test_calc.py").write_text("def test_add():\n    pass\n")
    _git(wt, "add", "tests")
    _git(wt, "-c", "user.email=t@e.test", "-c", "user.name=t", "commit", "-qm", "test")
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 1 and f"fix: ledger: no {briefs.LEDGER_FILE}" in out, out
    head = _git(wt, "rev-parse", "HEAD")
    write_ledger(wt, ledger_text("0" * 40))
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 1 and "ledger: names commit" in out, out
    write_ledger(wt, ledger_text(head, findings=["FINDING medium security calc.py:1 | add drops negatives | open"]))
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 1 and "medium calc.py:1 add drops negatives is open" in out, out
    write_ledger(wt, ledger_text(head, findings=["FINDING medium security calc.py:1 | add drops negatives | fixed tests/test_calc.py mutation=failed"]))
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 0 and out.startswith("PREFLIGHT ready"), out
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code == 0, out
    assert "warning" not in out and briefs.LEDGER_FILE not in out, out
    assert not (wt / briefs.LEDGER_FILE).exists(), "submit consumes the ledger"
    rev = dict(env.con().execute("SELECT * FROM revisions WHERE task_id='T1'").fetchone())
    assert briefs.LEDGER_FILE not in _git(wt, "ls-tree", "-r", "--name-only", rev["commit_sha"])


@pytest.mark.integration
@pytest.mark.approved
def test_preflight_contract_conflict_is_a_stop_quoting_the_accept_line(env):
    wenv, wt, d = _dispatch(env)
    write_ledger(wt, ledger_text(_git(wt, "rev-parse", "HEAD"),
                                 findings=["FINDING high security calc.py:1 | add must be float | contract-conflict accept=1"]))
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 4 and out.startswith("PREFLIGHT stop") and 'ACCEPT 1: "calc.add(2, 3) == 5"' in out, out


@pytest.mark.integration
def test_a_ledger_inside_a_wide_scope_still_stays_out_of_the_revision(env):
    from conftest import PLAN_ONE
    from office import briefs
    wenv, wt, d = _dispatch(env, plan=PLAN_ONE.replace("scope: calc.py", "scope: *"))
    write_ledger(wt)
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 0, out
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code == 0 and "warning" not in out, out
    rev = dict(env.con().execute("SELECT * FROM revisions WHERE task_id='T1'").fetchone())
    assert _git(wt, "ls-tree", "-r", "--name-only", rev["commit_sha"]).split().count("calc.py") == 1
    assert briefs.LEDGER_FILE not in _git(wt, "ls-tree", "-r", "--name-only", rev["commit_sha"])
    assert not (wt / briefs.LEDGER_FILE).exists()


# ------------------------------------------------------------------ strengthened: lone header lines, tiers, bounds

@pytest.mark.parametrize("bad", ["COMMIT", "COMMIT zzzzzzz", "COMMIT abc", "COMMIT " + "a" * 41])
def test_a_bad_commit_as_the_only_commit_line_is_malformed_not_a_wildcard(repo, bad):
    text = "\n".join([bad, "ROUND 1"] + [f"LENS {x} reviewed" for x in LENSES]) + "\n"
    fix = fixes(repo, text)
    assert any(f.startswith("ledger line 1:") and "COMMIT needs the full sha of HEAD" in f for f in fix), fix
    assert any("no COMMIT line" in f for f in fix), fix


@pytest.mark.parametrize("bad", ["ROUND", "ROUND 0", "ROUND 4", "ROUND 99", "ROUND two", "ROUND \u00b2", "ROUND " + "9" * 5000])
def test_a_bad_round_as_the_only_round_line_is_malformed(repo, bad):
    head = _git(repo, "rev-parse", "HEAD")
    text = "\n".join([f"COMMIT {head}", bad] + [f"LENS {x} reviewed" for x in LENSES]
                     + ["FINDING medium security calc.py:1 | x | open"]) + "\n"
    write_ledger(repo, text)
    stop, fix = verdict(repo)
    assert stop == [], "an invalid round must not be read as the round-3 cap"
    assert any(f.startswith("ledger line 2:") and "ROUND must be 1-3" in f for f in fix), fix
    assert any("no ROUND line" in f for f in fix), fix


@pytest.mark.parametrize("bad, why", [("LENS security reviewed extra", "LENS needs"), ("LENS security done", "LENS needs"),
                                      ("LENS security skipped", "LENS needs"), ("LENS crypto reviewed", "LENS must be one of")])
def test_a_bad_lens_line_on_its_own_is_malformed(repo, bad, why):
    head = _git(repo, "rev-parse", "HEAD")
    text = f"COMMIT {head}\nROUND 1\n{bad}\n" + "".join(f"LENS {x} reviewed\n" for x in LENSES if x != "security")
    fix = fixes(repo, text)
    assert any(f.startswith("ledger line 3:") and why in f for f in fix), fix


def test_a_header_less_ledger_names_each_missing_part(repo):
    fix = fixes(repo, "FINDING low security calc.py:1 | s | fixed\n")
    assert any("no COMMIT line" in f for f in fix) and any("no ROUND line" in f for f in fix), fix
    assert not any("line 1:" in f for f in fix), "the FINDING itself is well formed"


def test_only_the_inline_tier_may_skip_a_lens(repo):
    head = _git(repo, "rev-parse", "HEAD")
    write_ledger(repo, ledger_text(head, skip={"platform": "pure python"}))
    assert verdict(repo, tier="inline") == ([], [])
    for tier in ("single", "deep"):
        stop, fix = verdict(repo, tier=tier)
        assert stop == [] and len(fix) == 1 and f"lens platform is skipped, but the {tier} tier" in fix[0], (tier, fix)


def test_a_huge_accept_number_is_malformed_not_a_crash(repo):
    head = _git(repo, "rev-parse", "HEAD")
    fix = fixes(repo, ledger_text(head, findings=["FINDING low security calc.py:1 | s | contract-conflict accept=" + "9" * 5000]))
    assert any(f.startswith("ledger line 7:") for f in fix), fix


def test_control_characters_and_length_never_reach_the_output_raw(repo):
    head = _git(repo, "rev-parse", "HEAD")
    nasty = "\x1b[2J\x1b]0;pwned\x07" + "A" * 5000
    fix = fixes(repo, ledger_text(head, findings=[f"FINDING high security {nasty} | s | open",
                                                  f"FINDING high security calc.py:2 | s | fixed {nasty}"]))
    assert len(fix) == 2
    for line in fix:
        assert "\x1b" not in line and "\x07" not in line and len(line) < 600, line


def test_bom_and_crlf_ledgers_parse(repo):
    head = _git(repo, "rev-parse", "HEAD")
    text = ledger_text(head, findings=["FINDING low security calc.py:1 | s | fixed"]).replace("\n", "\r\n")
    write_ledger(repo, "\ufeff" + text)
    assert verdict(repo) == ([], [])


def test_a_pytest_node_id_names_its_file(repo):
    head = _git(repo, "rev-parse", "HEAD")
    findings = ["FINDING high security calc.py:3 | race | fixed tests/test_calc.py::test_x mutation=failed"]
    assert fixes(repo, ledger_text(head, findings=findings)) == []


def test_a_test_path_that_is_a_symlink_out_of_the_worktree_is_refused(repo, tmp_path):
    outside = tmp_path / "outside_test.py"
    outside.write_text("x\n")
    os.symlink(outside, repo / "tests" / "link.py")
    commit_all(repo, "link")
    head = _git(repo, "rev-parse", "HEAD")
    fix = fixes(repo, ledger_text(head, findings=["FINDING high security calc.py:3 | race | fixed tests/link.py mutation=failed"]))
    assert len(fix) == 1 and "is not a file in this worktree" in fix[0], fix


def test_the_size_cap_is_exact(repo):
    from office import briefs
    head = _git(repo, "rev-parse", "HEAD")
    base = ledger_text(head)
    at_cap = base + "FINDING low security calc.py:1 | " + "x" * (briefs.LEDGER_MAX_CHARS - len(base) - len("FINDING low security calc.py:1 |  | fixed\n")) + " | fixed\n"
    assert len(at_cap) == briefs.LEDGER_MAX_CHARS
    write_ledger(repo, at_cap)
    assert verdict(repo) == ([], [])
    write_ledger(repo, at_cap + "\n#")
    stop, fix = verdict(repo)
    assert len(fix) == 1 and "over" in fix[0], fix
    write_ledger(repo, base + "FINDING low security calc.py:1 | " + "\u00e9" * 6000 + " | fixed\n")  # multibyte, under the cap in characters
    assert verdict(repo) == ([], [])


def test_a_ledger_in_a_staged_or_committed_state_is_a_fix_even_when_nothing_else_is_owed(tmp_path):
    from office import briefs
    wt = tmp_path / "t"
    wt.mkdir()
    _git(wt, "init", "-q")
    _git(wt, "-c", "user.email=t@e.test", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "base")
    write_ledger(wt, "x\n")
    _git(wt, "add", briefs.LEDGER_FILE)
    stop, fix = verdict(wt, task={"scope": [], "accept": []})
    assert len(fix) == 1 and "committed or staged" in fix[0], fix
    commit_all(wt)
    stop, fix = verdict(wt, task={"scope": [], "accept": []})
    assert len(fix) == 1 and "committed or staged" in fix[0], fix


# ------------------------------------------------------------------ submit side

def test_evidence_commit_consumes_the_ledger_only_when_the_transaction_succeeded(tmp_path):
    from office import submit
    ledger = tmp_path / "OFFICE_SELF_REVIEW.md"
    ledger.write_text("keep me\n")
    with pytest.raises(RuntimeError):
        with submit._evidence_commit(ledger):
            raise RuntimeError("submit failed")
    assert ledger.read_text() == "keep me\n", "a failed submit keeps the ledger for the retry"
    with submit._evidence_commit(ledger):
        pass
    assert not ledger.exists()


def test_a_directory_named_like_the_ledger_is_not_taken_for_it(repo):
    from office import submit
    d = repo / "OFFICE_SELF_REVIEW.md"
    d.mkdir()
    (d / "x").write_text("x\n")
    assert submit._executor_ledger(repo) is None


def test_executor_ledger_is_found_untracked_staged_or_ignored_but_not_when_head_tracks_it(repo):
    from office import briefs, submit
    path = write_ledger(repo)
    assert submit._executor_ledger(repo) == path
    _git(repo, "add", briefs.LEDGER_FILE)
    assert submit._executor_ledger(repo) == path
    _git(repo, "reset", "-q", briefs.LEDGER_FILE)
    (repo / ".gitignore").write_text("*.md\n")
    assert submit._executor_ledger(repo) == path
    _git(repo, "add", "-f", briefs.LEDGER_FILE)
    commit_all(repo)
    assert submit._executor_ledger(repo) is None


@pytest.mark.integration
def test_a_staged_ledger_under_a_wide_scope_stays_out_of_the_revision(env):
    from conftest import PLAN_ONE
    from office import briefs
    wenv, wt, d = _dispatch(env, plan=PLAN_ONE.replace("scope: calc.py", "scope: *"))
    path = write_ledger(wt)
    _git(wt, "add", "-f", briefs.LEDGER_FILE)  # staged but not committed
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code == 0, out
    rev = dict(env.con().execute("SELECT * FROM revisions WHERE task_id='T1'").fetchone())
    assert briefs.LEDGER_FILE not in _git(wt, "ls-tree", "-r", "--name-only", rev["commit_sha"])
    assert not path.exists()
