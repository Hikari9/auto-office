"""The suite never creates or writes the real repository's `.office/`.

tests/conftest.py checks it around every test. These tests prove the check can fail: a detector
that cannot see a write guards nothing."""
from __future__ import annotations

import importlib.util
import os
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


def test_suite_leaves_the_real_office_dir_alone(env):
    """An Office run in the fixture repository writes its `.office/` there, never here."""
    from conftest import PLAN_ONE, start_inline
    before = Path(ROOT / ".office").exists()
    start_inline(env, plan=PLAN_ONE, gear="direct+review")
    env.office("status", check=0)
    assert (env.repo / ".office").exists()
    assert Path(ROOT / ".office").exists() == before


def test_a_patched_pathlib_is_no_false_alarm(guard, tmp_path, monkeypatch):
    """The check runs while a test's own monkeypatches may still be active (test_self_review_ledger
    patches Path.exists to True): an absent `.office/` stays absent."""
    monkeypatch.setattr(Path, "exists", lambda self: True)
    monkeypatch.setattr(Path, "is_dir", lambda self: True)
    assert guard.office_dir_snapshot(tmp_path) is None
