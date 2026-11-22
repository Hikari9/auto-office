"""#908: preflight accepts a low `fixed` without a test path, and doc-only tasks can mark
medium/high doc fixes as verified by independent review without naming a test file."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from conftest import GOOD_ADD


def _git(wt: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(wt), *args], check=True, capture_output=True, text=True).stdout.strip()


def ledger_text(head: str, *, rnd: int = 1, findings=()) -> str:
    lines = [
        f"COMMIT {head}",
        f"ROUND {rnd}",
        "LENS security reviewed",
        "LENS edge-cases reviewed",
        "LENS platform reviewed",
        "LENS test-strength reviewed",
    ]
    return "\n".join(lines + list(findings)) + "\n"


def write_ledger(wt: Path, text: str) -> Path:
    from office import briefs
    path = wt / briefs.LEDGER_FILE
    path.write_text(text)
    return path


def commit_all(wt: Path, msg: str = "work") -> None:
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", msg)


@pytest.fixture
def repo(tmp_path):
    """A standard code repo fixture with calc.py and tests/test_calc.py."""
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


@pytest.fixture
def doc_repo(tmp_path):
    """A repo for a doc-only task with docs/guide.md."""
    wt = tmp_path / "doc_wt"
    wt.mkdir()
    _git(wt, "init", "-q")
    _git(wt, "config", "user.email", "t@example.test")
    _git(wt, "config", "user.name", "t")
    (wt / "README.md").write_text("readme\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "base")
    (wt / "docs").mkdir()
    (wt / "docs" / "guide.md").write_text("# Guide\nSome doc content.\n")
    commit_all(wt, "docs")
    return wt


def verdict(wt: Path, task, tier="deep"):
    from office import preflight
    base = _git(wt, "rev-list", "--max-parents=0", "HEAD")
    changed = [f for f in _git(wt, "diff", "--name-only", "-z", base, "HEAD").split("\0") if f]
    return preflight.ledger_verdict(wt, task, changed, _git(wt, "rev-parse", "HEAD"), tier)


def fixes(wt: Path, text: str, task, **kw) -> list[str]:
    write_ledger(wt, text)
    stop, fix = verdict(wt, task=task, **kw)
    assert not stop, stop
    return fix


# ------------------------------------------------------------------ Low finding tests

@pytest.mark.parametrize("note", [
    "typo",
    "renamed variable to count",
    "clarified the docstring with more details",
    "removed redundant helper",
    "README.md",
    "fixed the comment indentation",
])
def test_low_fixed_with_note_passes_preflight(repo, note):
    head = _git(repo, "rev-parse", "HEAD")
    task = {"scope": ["calc.py", "tests/**"], "accept": ["calc.add(2, 3) == 5"]}
    finding = f"FINDING low security calc.py:9 | cosmetic | fixed {note}"
    text = ledger_text(head, findings=[finding])
    assert fixes(repo, text, task=task) == []


@pytest.mark.parametrize("lens", ["security", "edge-cases", "platform", "test-strength"])
def test_low_fixed_across_all_lenses(repo, lens):
    head = _git(repo, "rev-parse", "HEAD")
    task = {"scope": ["calc.py", "tests/**"], "accept": ["calc.add(2, 3) == 5"]}
    finding = f"FINDING low {lens} calc.py:9 | nit | fixed cleaned up {lens} detail"
    text = ledger_text(head, findings=[finding])
    assert fixes(repo, text, task=task) == []


def test_low_fixed_empty_passes(repo):
    head = _git(repo, "rev-parse", "HEAD")
    task = {"scope": ["calc.py", "tests/**"], "accept": ["calc.add(2, 3) == 5"]}
    finding = "FINDING low security calc.py:9 | nit | fixed"
    text = ledger_text(head, findings=[finding])
    assert fixes(repo, text, task=task) == []


def test_low_fixed_with_test_path_and_mutation_passes(repo):
    head = _git(repo, "rev-parse", "HEAD")
    task = {"scope": ["calc.py", "tests/**"], "accept": ["calc.add(2, 3) == 5"]}
    finding = "FINDING low security calc.py:9 | nit | fixed tests/test_calc.py mutation=failed"
    text = ledger_text(head, findings=[finding])
    assert fixes(repo, text, task=task) == []


def test_low_fixed_mutation_passed_is_rejected(repo):
    head = _git(repo, "rev-parse", "HEAD")
    task = {"scope": ["calc.py", "tests/**"], "accept": ["calc.add(2, 3) == 5"]}
    finding = "FINDING low security calc.py:9 | nit | fixed tests/test_calc.py mutation=passed"
    text = ledger_text(head, findings=[finding])
    fix = fixes(repo, text, task=task)
    assert len(fix) == 1 and "mutation=failed" in fix[0], fix


def test_low_fixed_mutation_failed_without_test_is_rejected(repo):
    head = _git(repo, "rev-parse", "HEAD")
    task = {"scope": ["calc.py", "tests/**"], "accept": ["calc.add(2, 3) == 5"]}
    finding = "FINDING low security calc.py:9 | nit | fixed mutation=failed"
    text = ledger_text(head, findings=[finding])
    fix = fixes(repo, text, task=task)
    assert len(fix) == 1 and "names the test path before mutation=failed" in fix[0], fix


# ------------------------------------------------------------------ Doc-only task tests

@pytest.mark.parametrize("sev", ["medium", "high"])
@pytest.mark.parametrize("disp", [
    "fixed verified by independent review",
    "fixed Verified by independent review",
    "fixed verified by independent reviewer",
    "fixed verified by independent review.",
    "fixed  verified   by   independent   review",
])
def test_doc_only_task_allows_independent_review_for_medium_and_high(doc_repo, sev, disp):
    head = _git(doc_repo, "rev-parse", "HEAD")
    task = {"scope": ["docs/**"], "accept": ["guide updated"]}
    finding = f"FINDING {sev} edge-cases docs/guide.md:2 | missing caveat | {disp}"
    text = ledger_text(head, findings=[finding])
    assert fixes(doc_repo, text, task=task) == []


def test_doc_only_task_with_readme_scope(tmp_path):
    wt = tmp_path / "readme_wt"
    wt.mkdir()
    _git(wt, "init", "-q")
    _git(wt, "config", "user.email", "t@example.test")
    _git(wt, "config", "user.name", "t")
    (wt / "README.md").write_text("initial\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "base")
    (wt / "README.md").write_text("updated readme\n")
    commit_all(wt, "update readme")
    head = _git(wt, "rev-parse", "HEAD")

    task = {"scope": ["README.md"], "accept": ["readme updated"]}
    finding = "FINDING medium edge-cases README.md:1 | unclear quickstart | fixed verified by independent review"
    text = ledger_text(head, findings=[finding])
    assert fixes(wt, text, task=task) == []


def test_doc_only_task_without_independent_review_requires_test_or_review(doc_repo):
    head = _git(doc_repo, "rev-parse", "HEAD")
    task = {"scope": ["docs/**"], "accept": ["guide updated"]}
    finding = "FINDING medium edge-cases docs/guide.md:2 | missing caveat | fixed"
    text = ledger_text(head, findings=[finding])
    fix = fixes(doc_repo, text, task=task)
    assert len(fix) == 1 and "without a test path" in fix[0], fix


def test_doc_only_task_finding_on_code_file_cannot_use_independent_review(doc_repo):
    head = _git(doc_repo, "rev-parse", "HEAD")
    task = {"scope": ["docs/**"], "accept": ["guide updated"]}
    finding = "FINDING medium edge-cases src/calc.py:2 | code defect | fixed verified by independent review"
    text = ledger_text(head, findings=[finding])
    fix = fixes(doc_repo, text, task=task)
    assert len(fix) == 1 and "only doc fixes can be verified by independent review" in fix[0], fix


# ------------------------------------------------------------------ Code task tests

@pytest.mark.parametrize("sev", ["medium", "high"])
def test_code_task_rejects_independent_review(repo, sev):
    head = _git(repo, "rev-parse", "HEAD")
    task = {"scope": ["calc.py", "tests/**"], "accept": ["calc.add(2, 3) == 5"]}
    finding = f"FINDING {sev} security calc.py:9 | add skips zero | fixed verified by independent review"
    text = ledger_text(head, findings=[finding])
    fix = fixes(repo, text, task=task)
    assert len(fix) == 1 and "code tasks require a test path" in fix[0] and "calc.py:9" in fix[0], fix


def test_code_task_touching_both_code_and_docs_rejects_independent_review(repo):
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "guide.md").write_text("doc\n")
    commit_all(repo, "add doc")
    head = _git(repo, "rev-parse", "HEAD")
    task = {"scope": ["calc.py", "docs/**"], "accept": ["both"]}
    finding = "FINDING medium edge-cases docs/guide.md:1 | typo | fixed verified by independent review"
    text = ledger_text(head, findings=[finding])
    fix = fixes(repo, text, task=task)
    assert len(fix) == 1 and "code tasks require a test path" in fix[0], fix


def test_code_task_with_real_test_path_passes(repo):
    head = _git(repo, "rev-parse", "HEAD")
    task = {"scope": ["calc.py", "tests/**"], "accept": ["calc.add(2, 3) == 5"]}
    finding = "FINDING medium security calc.py:9 | add skips zero | fixed tests/test_calc.py mutation=failed"
    text = ledger_text(head, findings=[finding])
    assert fixes(repo, text, task=task) == []
