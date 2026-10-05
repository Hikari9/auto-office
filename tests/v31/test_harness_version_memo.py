"""Tests for in-process memoization of harness_version."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from office import adapters


@pytest.fixture(autouse=True)
def clean_memo():
    adapters._RESOLVED_EXE.clear()
    adapters._VERSION_MEMO.clear()
    yield
    adapters._RESOLVED_EXE.clear()
    adapters._VERSION_MEMO.clear()


def _make_executable(path: Path, script_content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(script_content, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_harness_version_memo_n_calls_one_probe_and_mtime_change_reprobes(tmp_path, monkeypatch):
    """Proves the memo: N calls produce one probe, and a changed mtime produces a second probe."""
    monkeypatch.setattr(adapters.paths, "data_home", lambda: tmp_path)
    bin_dir = tmp_path / "bin"
    exe_path = bin_dir / "fakeharness"
    _make_executable(exe_path, "#!/bin/sh\necho 'fakeharness 1.2.3'\n")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")

    adapter = {"id": "fakeharness", "invocation": {"executable": "fakeharness"}}

    orig_run = subprocess.run
    orig_which = shutil.which
    orig_read_text = Path.read_text

    run_calls = []
    which_calls = []
    read_text_calls = []

    def spy_run(*args, **kwargs):
        run_calls.append(args)
        return orig_run(*args, **kwargs)

    def spy_which(cmd, *args, **kwargs):
        if cmd == "fakeharness":
            which_calls.append(cmd)
        return orig_which(cmd, *args, **kwargs)

    def spy_read_text(self, *args, **kwargs):
        if self.name == "harness-versions.json":
            read_text_calls.append(str(self))
        return orig_read_text(self, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy_run)
    monkeypatch.setattr(shutil, "which", spy_which)
    monkeypatch.setattr(Path, "read_text", spy_read_text)

    # 1. N calls produce exactly one probe
    n = 5
    for _ in range(n):
        v = adapters.harness_version(adapter)
        assert v == "1.2.3"

    assert len(run_calls) == 1, f"Expected 1 probe call, got {len(run_calls)}"
    assert len(which_calls) == 1, f"Expected 1 which call across {n} invocations, got {len(which_calls)}"
    # Repeated calls must do no file read of the on-disk cache
    assert len(read_text_calls) == 1, f"Expected at most 1 read of harness-versions.json, got {len(read_text_calls)}"

    # 2. A changed mtime produces a second probe
    # Update script and advance mtime
    new_mtime = exe_path.stat().st_mtime + 50
    exe_path.write_text("#!/bin/sh\necho 'fakeharness 2.0.0'\n", encoding="utf-8")
    os.utime(exe_path, (new_mtime, new_mtime))

    v2 = adapters.harness_version(adapter)
    assert v2 == "2.0.0"
    assert len(run_calls) == 2, f"Expected 2 probe calls after mtime change, got {len(run_calls)}"

    # Subsequent call with same new mtime also memoized
    v3 = adapters.harness_version(adapter)
    assert v3 == "2.0.0"
    assert len(run_calls) == 2


def test_harness_version_missing_executable(tmp_path, monkeypatch):
    """Missing executable returns None and repeated calls do no shutil.which or file read."""
    monkeypatch.setattr(adapters.paths, "data_home", lambda: tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))

    # Adapter with no invocation/executable
    assert adapters.harness_version({}) is None
    assert adapters.harness_version({"invocation": {}}) is None

    adapter = {"id": "missing", "invocation": {"executable": "nonexistent_harness_xyz"}}

    orig_which = shutil.which
    which_calls = []

    def spy_which(cmd, *args, **kwargs):
        if cmd == "nonexistent_harness_xyz":
            which_calls.append(cmd)
        return orig_which(cmd, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", spy_which)

    assert adapters.harness_version(adapter) is None
    assert adapters.harness_version(adapter) is None
    assert adapters.harness_version(adapter) is None

    assert len(which_calls) == 1


def test_harness_version_swapped_fake_binary_different_path(tmp_path, monkeypatch):
    """Tests that swap fake binaries between cases (via PATH or location) see the new version."""
    monkeypatch.setattr(adapters.paths, "data_home", lambda: tmp_path)
    bin_dir_a = tmp_path / "bin_a"
    bin_dir_b = tmp_path / "bin_b"

    _make_executable(bin_dir_a / "fake", "#!/bin/sh\necho 'fake 1.0.0'\n")
    _make_executable(bin_dir_b / "fake", "#!/bin/sh\necho 'fake 2.0.0'\n")

    adapter = {"id": "fake", "invocation": {"executable": "fake"}}

    # Case 1: PATH points to bin_a
    monkeypatch.setenv("PATH", f"{bin_dir_a}:{os.environ.get('PATH', '')}")
    assert adapters.harness_version(adapter) == "1.0.0"

    # Case 2: PATH points to bin_b (swapped binary location)
    monkeypatch.setenv("PATH", f"{bin_dir_b}:{os.environ.get('PATH', '')}")
    assert adapters.harness_version(adapter) == "2.0.0"


def test_harness_version_cross_process_cache(tmp_path, monkeypatch):
    """The on-disk cache stays the cross-process cache."""
    monkeypatch.setattr(adapters.paths, "data_home", lambda: tmp_path)
    bin_dir = tmp_path / "bin"
    exe = _make_executable(bin_dir / "tool", "#!/bin/sh\necho 'tool 3.1.4'\n")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")

    adapter = {"id": "tool", "invocation": {"executable": "tool"}}

    # Process 1 probes and populates on-disk cache
    assert adapters.harness_version(adapter) == "3.1.4"
    cache_path = tmp_path / "harness-versions.json"
    assert cache_path.is_file()
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    assert data["tool"]["version"] == "3.1.4"

    # Process 2 starts fresh (in-process memo cleared)
    adapters._RESOLVED_EXE.clear()
    adapters._VERSION_MEMO.clear()

    # In process 2, harness_version should load from disk cache without running subprocess
    with patch("subprocess.run") as mock_run:
        assert adapters.harness_version(adapter) == "3.1.4"
        mock_run.assert_not_called()

    # Subsequent call in process 2 uses in-process memo (no file read)
    with patch.object(Path, "read_text") as mock_read:
        assert adapters.harness_version(adapter) == "3.1.4"
        mock_read.assert_not_called()


def test_harness_version_probe_failure_returns_none(tmp_path, monkeypatch):
    """When the probe command fails, harness_version returns None."""
    monkeypatch.setattr(adapters.paths, "data_home", lambda: tmp_path)
    bin_dir = tmp_path / "bin"
    _make_executable(bin_dir / "crash", "#!/bin/sh\nexit 1\n")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")

    adapter = {"id": "crash", "invocation": {"executable": "crash"}}

    def fail_run(*args, **kwargs):
        raise subprocess.SubprocessError("crashed")

    monkeypatch.setattr(subprocess, "run", fail_run)
    assert adapters.harness_version(adapter) is None


def test_harness_version_ttl_expiration(tmp_path, monkeypatch):
    """When _VERSION_TTL_SECONDS expires, harness_version re-probes."""
    monkeypatch.setattr(adapters.paths, "data_home", lambda: tmp_path)
    bin_dir = tmp_path / "bin"
    exe = _make_executable(bin_dir / "timed", "#!/bin/sh\necho 'timed 1.0'\n")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")

    adapter = {"id": "timed", "invocation": {"executable": "timed"}}

    current_time = 1000000.0
    monkeypatch.setattr(time, "time", lambda: current_time)

    probe_count = 0
    orig_run = subprocess.run

    def counting_run(*args, **kwargs):
        nonlocal probe_count
        probe_count += 1
        return orig_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", counting_run)

    assert adapters.harness_version(adapter) == "1.0"
    assert probe_count == 1

    # Within TTL: memo hit
    current_time += 100
    assert adapters.harness_version(adapter) == "1.0"
    assert probe_count == 1

    # Past TTL: re-probe
    current_time += adapters._VERSION_TTL_SECONDS + 1
    assert adapters.harness_version(adapter) == "1.0"
    assert probe_count == 2
