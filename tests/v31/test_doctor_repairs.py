"""Doctor repairs must restore pinned code and preserve unrelated user config."""
from __future__ import annotations

import json
import subprocess

import yaml

from office import config_repairs, legacy


def test_packaged_install_restores_exact_legacy_commit(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    runtime = source / "scripts" / "office_runtime.py"
    runtime.parent.mkdir()
    runtime.write_text("# original pinned runtime\n")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=test", "-c", "user.email=test@test",
                    "commit", "-qm", "pinned"], check=True)
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    runtime.write_text("# later checkout code must not be retained\n")
    monkeypatch.setattr(legacy, "SOURCE_ROOT", tmp_path / "venv")
    monkeypatch.setattr(legacy, "install_source", lambda: source)
    monkeypatch.setattr(legacy.paths, "runtimes_dir", lambda: tmp_path / "retained")
    assert legacy.retained_runtime(commit, materialize=False) is None
    target = legacy.retained_runtime(commit)
    assert target is not None
    assert (target / "scripts" / "office_runtime.py").read_text() == "# original pinned runtime\n"
    assert legacy.retained_runtime(commit, materialize=False) == target
    assert runtime.read_text() == "# later checkout code must not be retained\n"


def test_missing_install_source_leaves_runtime_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(legacy, "SOURCE_ROOT", tmp_path / "venv")
    monkeypatch.setattr(legacy, "install_source", lambda: None)
    monkeypatch.setattr(legacy.paths, "runtimes_dir", lambda: tmp_path / "retained")
    assert legacy.retained_runtime("a" * 40) is None
    assert not (tmp_path / "retained").exists()


def test_gemini_retires_only_obsolete_office_entries(env, monkeypatch):
    monkeypatch.setenv("HOME", str(env.home))
    path = env.home / ".gemini/config/hooks.json"
    path.parent.mkdir(parents=True)
    keep = {"hooks": {"PreToolUse": [{"hooks": [{"command": "context-mode hook antigravity-cli pretooluse"}]}]},
            "herdr": {"PreInvocation": [{"command": "echo other"}]}, "Stop": "echo user",
            "notes": "a reference to --office-managed is not a hook"}
    data = {**keep, "SessionEnd": "/Users/example/Git/office-skills/.office/hooks/session_end.sh",
            "office-skills": {"SessionEnd": [{"hooks": []}]}}
    original = json.dumps(data, indent=2) + "\n"
    path.write_text(original)
    assert config_repairs.gemini_legacy(False)[1] == 1
    assert path.read_text() == original
    assert config_repairs.gemini_legacy(True)[1] == 0
    assert json.loads(path.read_text()) == keep
    backups = list(path.parent.glob("hooks.json.bak.*"))
    assert len(backups) == 1 and backups[0].read_text() == original
    assert config_repairs.gemini_legacy(True)[1] == 0
    assert list(path.parent.glob("hooks.json.bak.*")) == backups


def test_hermes_repairs_scalars_preserving_yaml_and_approval(env, monkeypatch):
    monkeypatch.setenv("HOME", str(env.home))
    path = env.home / ".hermes/profiles/default/config.yaml"
    path.parent.mkdir(parents=True)
    prefix = "# user comment\nmodel: 'keep quoted'\nhooks_auto_accept: false\nhooks:\n"
    original = prefix + ("  on_session_end: '/path with spaces/cleanup.sh' # keep inline comment\n"
                         "  on_session_finalize: |\n"
                         "    /path/cleanup.sh\n"
                         "  post_llm_call: [{command: echo user, timeout: 7}]\n"
                         "other: {quoted: 'keep me'}\n")
    path.write_text(original)
    assert config_repairs.hermes_hooks(False)[1] == 2
    assert path.read_text() == original
    lines, count = config_repairs.hermes_hooks(True)
    assert count == 0 and "repaired 2" in lines[0]
    data = yaml.safe_load(path.read_text())
    assert data["hooks"]["on_session_end"] == [{"command": "/path with spaces/cleanup.sh"}]
    assert data["hooks"]["on_session_finalize"] == [{"command": "/path/cleanup.sh\n"}]
    assert data["hooks"]["post_llm_call"] == [{"command": "echo user", "timeout": 7}]
    assert data["hooks_auto_accept"] is False
    assert path.read_text().startswith(prefix)
    assert "# keep inline comment" in path.read_text()
    assert "other: {quoted: 'keep me'}\n" in path.read_text()
    backups = list(path.parent.glob("config.yaml.bak.*"))
    assert len(backups) == 1 and backups[0].read_text() == original
    assert config_repairs.hermes_hooks(True) == ([], 0)
    assert list(path.parent.glob("config.yaml.bak.*")) == backups


def test_hermes_shared_alias_and_empty_scalar_are_not_rewritten(env, monkeypatch):
    monkeypatch.setenv("HOME", str(env.home))
    path = env.home / ".hermes/config.yaml"
    path.parent.mkdir(parents=True)
    original = 'shared: &cleanup echo cleanup\nhooks:\n  on_session_end: *cleanup\n  on_session_finalize: ""\n'
    path.write_text(original)
    assert config_repairs.hermes_hooks(True)[1] == 2
    assert path.read_text() == original
    assert not list(path.parent.glob("config.yaml.bak.*"))


def test_hermes_shared_mapping_and_reserved_sections_are_preserved(env, monkeypatch):
    monkeypatch.setenv("HOME", str(env.home))
    path = env.home / ".hermes/config.yaml"
    path.parent.mkdir(parents=True)
    original = 'shared: &settings\n  on_session_end: echo cleanup\nhooks: *settings\n'
    path.write_text(original)
    assert config_repairs.hermes_hooks(True)[1] == 1
    assert path.read_text() == original
    original = 'hooks:\n  output_spill: "user setting"\n  outbound: "another setting"\n'
    path.write_text(original)
    assert config_repairs.hermes_hooks(True) == ([], 0)
    assert path.read_text() == original


def test_doctor_fix_repairs_and_next_doctor_is_clean(env, monkeypatch):
    monkeypatch.setenv("HOME", str(env.home))
    path = env.home / ".hermes/profiles/default/config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("hooks:\n  on_session_end: echo cleanup\n")
    code, out = env.office("doctor")
    assert code == 1 and "bare string" in out
    code, out = env.office("doctor", "--fix")
    assert code == 0 and "repaired 1" in out, out
    code, out = env.office("doctor")
    assert code == 0 and "bare string" not in out, out
