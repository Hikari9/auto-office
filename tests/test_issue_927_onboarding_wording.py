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
    metadata.mkdir(parents=True, exist_ok=True)
    (metadata / "METADATA").write_text("Metadata-Version: 2.1\nName: auto-office\nVersion: 9999\n")
    if direct_url is not None:
        content = direct_url if isinstance(direct_url, str) else json.dumps(direct_url)
        (metadata / "direct_url.json").write_text(content)
    tool_bin = tmp_path / "tools" / "auto-office" / "bin"
    tool_bin.mkdir(parents=True, exist_ok=True)
    (tool_bin / "python").symlink_to(sys.executable)
    (tool_bin / "office").write_text("#!/bin/sh\n")
    (tool_bin / "office").chmod(0o755)
    executable_bin = tmp_path / "bin"
    executable_bin.mkdir(parents=True, exist_ok=True)
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
    found = (ROOT / "SKILL.md").read_text().split("- **Found:**", 1)[1].split("\n- **Not found:**", 1)[0]
    assert '`uv tool install --force --editable "<checkout path>"` for an editable install' in found


def test_probe_identifies_local_source_without_git(tmp_path):
    source = {"url": "file:///source-without-git", "dir_info": {}}
    result = _probe(tmp_path, source)
    assert result.returncode == 0 and json.loads(result.stdout) == source
    found = (ROOT / "SKILL.md").read_text().split("- **Found:**", 1)[1].split("\n- **Not found:**", 1)[0]
    assert '`uv tool install --force --reinstall "<checkout path>"` for a non-editable directory install' in found


def test_probe_flags_an_index_install_as_not_auto_office(tmp_path):
    # Auto Office is not on PyPI (#979): an index install is the unrelated `auto-office` project.
    result = _probe(tmp_path, None)
    assert result.returncode == 0 and result.stdout.strip() == "index install (not Auto Office)"
    found = (ROOT / "SKILL.md").read_text().split("- **Found:**", 1)[1].split("\n- **Not found:**", 1)[0]
    assert "the PyPI `auto-office` is an unrelated project, so tell the user to reinstall from GitHub" in found


def test_probe_identifies_a_github_install(tmp_path):
    source = {"url": "https://github.com/Hikari9/auto-office", "vcs_info": {"vcs": "git", "commit_id": "0" * 40}}
    result = _probe(tmp_path, source)
    assert result.returncode == 0 and json.loads(result.stdout) == source
    found = (ROOT / "SKILL.md").read_text().split("- **Found:**", 1)[1].split("\n- **Not found:**", 1)[0]
    assert "a `vcs_info` URL for github.com/Hikari9/auto-office is a GitHub install" in found


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


def test_probe_reports_vcs_info_rather_than_checkout(tmp_path):
    source = {"url": "https://github.com/example/auto-office.git", "vcs_info": {"vcs": "git", "commit_id": "abcdef123"}}
    result = _probe(tmp_path, source)
    assert result.returncode == 0 and json.loads(result.stdout) == source
    assert "dir_info" not in json.loads(result.stdout)
    found = (ROOT / "SKILL.md").read_text().split("- **Found:**", 1)[1].split("\n- **Not found:**", 1)[0]
    assert "unclear origin is not proof" in found


def test_probe_reports_empty_or_malformed_metadata_as_unclear(tmp_path):
    result_empty = _probe(tmp_path / "empty", "")
    assert result_empty.returncode == 0 and result_empty.stdout.strip() == "unclear origin"
    result_malformed = _probe(tmp_path / "malformed", "{malformed json")
    assert result_malformed.returncode == 0 and result_malformed.stdout.strip() == "unclear origin"
