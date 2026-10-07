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

    # Adapter with no invocation/executable or non-dict
    assert adapters.harness_version(None) is None
    assert adapters.harness_version({}) is None
    assert adapters.harness_version({"invocation": {}}) is None

    adapter = {"id": "missing", "invocation": {"executable": "nonexistent_harness_xyz"}}

    orig_which = shutil.which
    orig_read_text = Path.read_text
    which_calls = []
    read_text_calls = []

    def spy_which(cmd, *args, **kwargs):
        if cmd == "nonexistent_harness_xyz":
            which_calls.append(cmd)
        return orig_which(cmd, *args, **kwargs)

    def spy_read_text(self, *args, **kwargs):
        if self.name == "harness-versions.json":
            read_text_calls.append(str(self))
        return orig_read_text(self, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", spy_which)
    monkeypatch.setattr(Path, "read_text", spy_read_text)

    assert adapters.harness_version(adapter) is None
    assert adapters.harness_version(adapter) is None
    assert adapters.harness_version(adapter) is None

    assert len(which_calls) == 1
    assert len(read_text_calls) == 0


def test_harness_version_swapped_fake_binary_different_path(tmp_path, monkeypatch):
    """Tests that swap fake binaries between cases (via PATH or location) see the new version."""
    monkeypatch.setattr(adapters.paths, "data_home", lambda: tmp_path)
    bin_dir_a = tmp_path / "bin_a"
    bin_dir_b = tmp_path / "bin_b"

    bin_a = _make_executable(bin_dir_a / "fake", "#!/bin/sh\necho 'fake 1.0.0'\n")
    bin_b = _make_executable(bin_dir_b / "fake", "#!/bin/sh\necho 'fake 2.0.0'\n")
    # Synchronize timestamps to specifically test path discrimination (not mtime discrimination)
    st = bin_a.stat()
    os.utime(bin_b, ns=(st.st_atime_ns, st.st_mtime_ns))

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
    """When the probe command fails, harness_version returns None and repeated calls do not re-run."""
    monkeypatch.setattr(adapters.paths, "data_home", lambda: tmp_path)
    bin_dir = tmp_path / "bin"
    _make_executable(bin_dir / "crash", "#!/bin/sh\nexit 1\n")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")

    adapter = {"id": "crash", "invocation": {"executable": "crash"}}

    call_count = 0

    def fail_run(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise subprocess.SubprocessError("crashed")

    monkeypatch.setattr(subprocess, "run", fail_run)
    assert adapters.harness_version(adapter) is None
    assert adapters.harness_version(adapter) is None
    assert adapters.harness_version(adapter) is None
    assert call_count == 1, f"Expected 1 probe failure call, got {call_count}"


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
    which_count = 0
    read_text_count = 0
    orig_run = subprocess.run
    orig_which = shutil.which
    orig_read_text = Path.read_text

    def counting_run(*args, **kwargs):
        nonlocal probe_count
        probe_count += 1
        return orig_run(*args, **kwargs)

    def counting_which(cmd, *args, **kwargs):
        nonlocal which_count
        if cmd == "timed":
            which_count += 1
        return orig_which(cmd, *args, **kwargs)

    def counting_read_text(self, *args, **kwargs):
        nonlocal read_text_count
        if self.name == "harness-versions.json":
            read_text_count += 1
        return orig_read_text(self, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", counting_run)
    monkeypatch.setattr(shutil, "which", counting_which)
    monkeypatch.setattr(Path, "read_text", counting_read_text)

    assert adapters.harness_version(adapter) == "1.0"
    assert probe_count == 1
    assert which_count == 1
    assert read_text_count == 1

    # Within TTL: in-process memo hit (no probe, no which, no file read)
    current_time += 100
    assert adapters.harness_version(adapter) == "1.0"
    assert probe_count == 1
    assert which_count == 1, "In-process memo hit should not call shutil.which"
    assert read_text_count == 1, "In-process memo hit should not read harness-versions.json"

    # Past TTL: re-probe and re-checks which and disk cache
    current_time += adapters._VERSION_TTL_SECONDS + 1
    assert adapters.harness_version(adapter) == "1.0"
    assert probe_count == 2
    assert which_count == 2, "Expired TTL should re-call shutil.which"
    assert read_text_count == 2


def test_harness_version_disk_cache_corrupt_or_non_dict(tmp_path, monkeypatch):
    """Corrupt or non-dict harness-versions.json (e.g. null, []) does not crash harness_version."""
    monkeypatch.setattr(adapters.paths, "data_home", lambda: tmp_path)
    bin_dir = tmp_path / "bin"
    exe = _make_executable(bin_dir / "safe", "#!/bin/sh\necho 'safe 1.0'\n")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")

    adapter = {"id": "safe", "invocation": {"executable": "safe"}}
    cache_path = tmp_path / "harness-versions.json"

    # null in cache file
    cache_path.write_text("null", encoding="utf-8")
    assert adapters.harness_version(adapter) == "1.0"

    # [] in cache file
    adapters._RESOLVED_EXE.clear()
    adapters._VERSION_MEMO.clear()
    cache_path.write_text("[]", encoding="utf-8")
    assert adapters.harness_version(adapter) == "1.0"

    # entry with null 'at' timestamp in cache file
    adapters._RESOLVED_EXE.clear()
    adapters._VERSION_MEMO.clear()
    cache_path.write_text(json.dumps({"safe": {"version": "1.0", "at": None}}), encoding="utf-8")
    assert adapters.harness_version(adapter) == "1.0"


def test_harness_version_different_command_same_exe(tmp_path, monkeypatch):
    """Adapters with different version commands for the same executable probe separately and memoize."""
    monkeypatch.setattr(adapters.paths, "data_home", lambda: tmp_path)
    bin_dir = tmp_path / "bin"
    script = (
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--v1\" ]; then echo '1.0.0'; else echo '2.0.0'; fi\n"
    )
    _make_executable(bin_dir / "multicmd", script)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")

    adapter_a = {
        "id": "multicmd_a",
        "invocation": {"executable": "multicmd"},
        "version_fingerprint": {"command": ["multicmd", "--v1"]},
    }
    adapter_b = {
        "id": "multicmd_b",
        "invocation": {"executable": "multicmd"},
        "version_fingerprint": {"command": ["multicmd", "--v2"]},
    }

    orig_run = subprocess.run
    run_counts: dict[str, int] = {}

    def counting_run(cmd, *args, **kwargs):
        arg = cmd[1] if len(cmd) > 1 else ""
        run_counts[arg] = run_counts.get(arg, 0) + 1
        return orig_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", counting_run)

    # Initial probe for each
    assert adapters.harness_version(adapter_a) == "1.0.0"
    assert adapters.harness_version(adapter_b) == "2.0.0"
    assert run_counts == {"--v1": 1, "--v2": 1}

    # Repeated calls hit in-process memo without probing
    assert adapters.harness_version(adapter_a) == "1.0.0"
    assert adapters.harness_version(adapter_b) == "2.0.0"
    assert run_counts == {"--v1": 1, "--v2": 1}


def test_harness_version_nonzero_exit_with_version_output(tmp_path, monkeypatch):
    """When a version command emits a usable version on stdout or stderr but exits nonzero,
    the output is parsed, returned, and memoized (both in-process and on-disk)."""
    monkeypatch.setattr(adapters.paths, "data_home", lambda: tmp_path)
    bin_dir = tmp_path / "bin"
    # Case 1: command exits 1 with version on stderr
    _make_executable(bin_dir / "errprobe_stderr", "#!/bin/sh\necho 'error 503.1: offline' >&2\nexit 1\n")
    # Case 2: command exits 2 with version on stdout
    _make_executable(bin_dir / "errprobe_stdout", "#!/bin/sh\necho 'tool v2.4.9 (experimental)'\nexit 2\n")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")

    orig_run = subprocess.run
    run_counts: dict[str, int] = {}

    def counting_run(cmd, *args, **kwargs):
        exe_name = Path(cmd[0]).name
        run_counts[exe_name] = run_counts.get(exe_name, 0) + 1
        return orig_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", counting_run)

    adapter_stderr = {"id": "errprobe_stderr", "invocation": {"executable": "errprobe_stderr"}}
    adapter_stdout = {"id": "errprobe_stdout", "invocation": {"executable": "errprobe_stdout"}}

    # Probe returns parsed version despite non-zero exit
    assert adapters.harness_version(adapter_stderr) == "503.1"
    assert adapters.harness_version(adapter_stdout) == "2.4.9"
    assert run_counts == {"errprobe_stderr": 1, "errprobe_stdout": 1}

    # Second calls within process hit in-process memo (no subprocess probe)
    assert adapters.harness_version(adapter_stderr) == "503.1"
    assert adapters.harness_version(adapter_stdout) == "2.4.9"
    assert run_counts == {"errprobe_stderr": 1, "errprobe_stdout": 1}

    # Verify on-disk cache populated
    cache_path = tmp_path / "harness-versions.json"
    assert cache_path.is_file()
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    assert data["errprobe_stderr"]["version"] == "503.1"
    assert data["errprobe_stdout"]["version"] == "2.4.9"

    # Cross-process: clear in-process memo, verify loaded from disk cache without probe
    adapters._RESOLVED_EXE.clear()
    adapters._VERSION_MEMO.clear()
    assert adapters.harness_version(adapter_stderr) == "503.1"
    assert adapters.harness_version(adapter_stdout) == "2.4.9"
    assert run_counts == {"errprobe_stderr": 1, "errprobe_stdout": 1}
