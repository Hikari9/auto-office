"""The suite never creates or writes the real repository's `.office/`.

tests/conftest.py checks it around every test. These tests prove the check can fail: a detector
that cannot see a write guards nothing."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def guard():
    spec = importlib.util.spec_from_file_location("office_tests_root_conftest", ROOT / "tests" / "conftest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_guard_watches_the_real_repository(guard):
    assert guard.REPO_ROOT == ROOT


def test_snapshot_sees_a_created_and_a_written_office_dir(guard, tmp_path):
    assert guard.office_dir_snapshot(tmp_path) is None
    (tmp_path / ".office").mkdir()
    empty = guard.office_dir_snapshot(tmp_path)
    assert empty is not None
    plan = tmp_path / ".office" / "plans" / "r1" / "PLAN.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("a\n")
    created = guard.office_dir_snapshot(tmp_path)
    assert created != empty
    plan.write_text("bb\n")
    assert guard.office_dir_snapshot(tmp_path) != created
    plan.unlink()
    assert guard.office_dir_snapshot(tmp_path) != created


def test_a_touch_without_a_size_change_is_still_a_write(guard, tmp_path):
    hook = tmp_path / ".office" / "sessions" / "claude-x.json"
    hook.parent.mkdir(parents=True)
    hook.write_text("a")
    before = guard.office_dir_snapshot(tmp_path)
    os.utime(hook, ns=(1, 1))
    assert guard.office_dir_snapshot(tmp_path) != before


def test_a_patched_pathlib_is_no_false_alarm(guard, tmp_path, monkeypatch):
    """The check runs while a test's own monkeypatches may still be active (test_self_review_ledger
    patches Path.exists to True): an absent `.office/` stays absent."""
    monkeypatch.setattr(Path, "exists", lambda self: True)
    monkeypatch.setattr(Path, "is_dir", lambda self: True)
    assert guard.office_dir_snapshot(tmp_path) is None


def test_check_fails_the_test_that_created_or_wrote_the_dir(guard, tmp_path):
    before = guard.office_dir_snapshot(tmp_path)
    guard.check_office_dir(tmp_path, before, "t::quiet")  # nothing changed: no failure
    (tmp_path / ".office").mkdir()
    with pytest.raises(pytest.fail.Exception, match=r"t::made created the real repository's .*\.office"):
        guard.check_office_dir(tmp_path, before, "t::made")
    before = guard.office_dir_snapshot(tmp_path)
    (tmp_path / ".office" / "x").write_text("a")
    with pytest.raises(pytest.fail.Exception, match=r"t::wrote wrote the real repository's"):
        guard.check_office_dir(tmp_path, before, "t::wrote")


def test_the_autouse_fixture_is_wired_to_the_check(tmp_path):
    """A copy of the suite's conftest in a scratch repository: a test that writes that repository's
    `.office/` errors at teardown naming itself, and one that does not passes. Never touches the real repo."""
    scratch = tmp_path / "repo"
    (scratch / "tests").mkdir(parents=True)
    (scratch / "tests" / "conftest.py").write_text((ROOT / "tests" / "conftest.py").read_text())
    (scratch / "tests" / "test_leak.py").write_text(
        "from pathlib import Path\n"
        "ROOT = Path(__file__).resolve().parents[1]\n"
        "def test_leaks():\n    (ROOT / '.office').mkdir()\n    (ROOT / '.office' / 'plan').write_text('x')\n"
        "def test_quiet():\n    pass\n")
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:xdist", str(scratch / "tests")],
                          capture_output=True, text=True, cwd=tmp_path)
    out = proc.stdout + proc.stderr
    assert "2 passed" in out and "1 error" in out, out  # the leaking test itself passed; its teardown errors
    assert "test_leak.py::test_leaks created the real repository's" in out, out


def test_every_test_starts_outside_the_repository_and_its_primary_checkout():
    """A command that falls back to the working directory finds no repository, instead of this one
    (or the primary checkout a linked worktree belongs to)."""
    from office import paths
    cwd = Path.cwd().resolve()
    assert ROOT not in (cwd, *cwd.parents)
    assert paths.repo_identity(cwd) is None


def test_the_suite_parses_yaml_with_libyaml_when_it_can():
    """tests/conftest.py swaps in the C loader for the session (the suite is several times slower without it)."""
    import yaml
    if not getattr(yaml, "__with_libyaml__", False):
        pytest.skip("PyYAML built without libyaml")
    assert yaml.safe_load.__name__ == "<lambda>"
