import json
import os
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def _probe(tmp_path, direct_url, *, mismatched=False):
    skill = (ROOT / "SKILL.md").read_text()
    found = skill.split("- **Found:**", 1)[1].split("\n- **Not found:**", 1)[0]
    command = re.search(r"`(test [^`]+)`", found).group(1)
    metadata = tmp_path / "auto_office-9999.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Metadata-Version: 2.1\nName: auto-office\nVersion: 9999\n")
    if direct_url is not None:
        (metadata / "direct_url.json").write_text(json.dumps(direct_url))
    tool_bin = tmp_path / "tools" / "auto-office" / "bin"
    tool_bin.mkdir(parents=True)
    (tool_bin / "python").symlink_to(sys.executable)
    (tool_bin / "office").write_text("#!/bin/sh\n")
    (tool_bin / "office").chmod(0o755)
    executable_bin = tmp_path / "bin"
    executable_bin.mkdir()
    (executable_bin / "office").symlink_to(tool_bin / "office" if not mismatched else sys.executable)
    (executable_bin / "uv").write_text(f'#!/bin/sh\nprintf "%s\\n" "{tmp_path / "tools"}"\n')
    (executable_bin / "uv").chmod(0o755)
    env = {**os.environ, "PYTHONPATH": str(tmp_path), "PATH": f"{executable_bin}:{os.environ['PATH']}"}
    return subprocess.run(command, shell=True, env=env, capture_output=True, text=True)


def test_onboarding_says_missing_cli_asks_once_per_session():
    assert "asks once per session" in (ROOT / "docs/onboarding.md").read_text()


def test_probe_identifies_editable_checkout_without_consulting_upstream(tmp_path):
    source = {"url": "file:///checkout-ahead", "dir_info": {"editable": True}}
    result = _probe(tmp_path, source)
    assert result.returncode == 0 and json.loads(result.stdout) == source


def test_probe_identifies_local_source_without_git(tmp_path):
    source = {"url": "file:///source-without-git", "dir_info": {}}
    result = _probe(tmp_path, source)
    assert result.returncode == 0 and json.loads(result.stdout) == source


def test_probe_requires_pypi_confirmation_for_index_install(tmp_path):
    result = _probe(tmp_path, None)
    assert result.returncode == 0 and result.stdout.strip() == "index install (confirm PyPI)"


def test_probe_refuses_a_different_office_executable(tmp_path):
    result = _probe(tmp_path, None, mismatched=True)
    assert result.returncode != 0 and not result.stdout


def test_archive_is_not_called_a_checkout(tmp_path):
    source = {"url": "file:///auto-office.whl", "archive_info": {}}
    result = _probe(tmp_path, source)
    assert result.returncode == 0 and json.loads(result.stdout) == source
    found = (ROOT / "SKILL.md").read_text().split("- **Found:**", 1)[1].split("\n- **Not found:**", 1)[0]
    assert "with `dir_info` identifies a source directory" in found
    assert "An archive, failed command" in found
