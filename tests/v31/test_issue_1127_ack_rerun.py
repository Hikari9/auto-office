"""#1127: an ack-only rerun restacked onto a moved base, with the task's own change unchanged, owes no new ledger.

`preflight.review_changes` decides what the producer self-review must cover. Before #1127 it treated only an
identical tree as already reviewed, so a restack (new base, same patch) demanded a fresh OFFICE_SELF_REVIEW.md.
"""
from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

from office import preflight


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, name: str, text: str, msg: str) -> str:
    (repo / name).write_text(text)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def _repo(tmp_path: Path) -> tuple[Path, dict]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    shas = {"b1": _commit(repo, "calc.py", "def add(a, b):\n    return a - b\n", "base")}
    shas["c1"] = _commit(repo, "calc.py", "def add(a, b):\n    return a + b\n", "fix")  # the accepted work
    # A dependency moved the base: B2 adds an unrelated file; C2 replays the same patch on top of it.
    _git(repo, "checkout", "-q", "--detach", shas["b1"])
    shas["b2"] = _commit(repo, "other.py", "X = 1\n", "dependency")
    shas["c2"] = _commit(repo, "calc.py", "def add(a, b):\n    return a + b\n", "fix again")
    return repo, shas


def _con(shas: dict) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE revisions(id TEXT, tree_sha TEXT, commit_sha TEXT, base_commit TEXT)")
    con.execute("INSERT INTO revisions VALUES('R1', ?, ?, ?)", (shas["tree1"], shas["c1"], shas["b1"]))
    return con


TASK = {"id": "T1", "accepted_revision_id": "R1", "scope": ["calc.py"]}


def test_a_restacked_rerun_with_the_same_patch_owes_no_new_ledger(tmp_path):
    repo, shas = _repo(tmp_path)
    shas["tree1"] = _git(repo, "rev-parse", f"{shas['c1']}^{{tree}}")
    assert _git(repo, "rev-parse", f"{shas['c2']}^{{tree}}") != shas["tree1"]  # the tree did move
    assert preflight.review_changes(_con(shas), TASK, repo, shas["b2"], shas["c2"], []) == []


def test_a_rerun_that_changed_its_own_patch_still_owes_a_ledger(tmp_path):
    repo, shas = _repo(tmp_path)
    shas["tree1"] = _git(repo, "rev-parse", f"{shas['c1']}^{{tree}}")
    c3 = _commit(repo, "calc.py", "def add(a, b):\n    return b + a\n", "different fix")
    assert preflight.review_changes(_con(shas), TASK, repo, shas["b2"], c3, []) == ["calc.py"]


def test_a_whitespace_only_difference_is_still_a_different_patch(tmp_path):
    """Indentation is code in Python: patch identity must not ignore whitespace."""
    repo, shas = _repo(tmp_path)
    shas["tree1"] = _git(repo, "rev-parse", f"{shas['c1']}^{{tree}}")
    c3 = _commit(repo, "calc.py", "def add(a, b):\n        return a + b\n", "reindent")
    assert preflight.review_changes(_con(shas), TASK, repo, shas["b2"], c3, []) == ["calc.py"]
