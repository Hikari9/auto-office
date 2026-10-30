"""The Claude plugin ships the root auto-office skill, and its install check detects the CLI without blocking."""
import json
import os
import re
import subprocess
from pathlib import Path

from scripts.check_ecosystem import check_plugin_skills, frontmatter

ROOT = Path(__file__).resolve().parents[2]


def _hub():
    return (ROOT / "SKILL.md").read_text(encoding="utf-8")


def _install_check():
    text = _hub()
    return text[text.index("## Install check"):text.index("## First-run onboarding")]


def _saved_choice_write_command():
    return re.search(r"\(in one shell command: `([^`]+)`", _install_check()).group(1)


def test_claude_manifest_exposes_root_skill_and_skills_dir():
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))

    assert manifest["skills"] == ["./", "./skills/"]


def test_root_skill_frontmatter_is_named_auto_office():
    ok, _, fm, _ = frontmatter(ROOT / "SKILL.md")

    assert ok and fm["name"] == "auto-office"


def test_install_check_detects_cli_without_failing_skill_load():
    assert '!`command -v office || echo "office: not found"`' in _install_check()


def test_install_check_offers_the_three_choices():
    section = _install_check()

    for choice in ("Install the CLI (Recommended)", "Lean for this request", "Lean, don't ask again"):
        assert choice in section, choice


def test_install_check_names_the_saved_choice_path_and_reads_and_writes_it():
    section = _install_check()

    assert 'CFG="${XDG_CONFIG_HOME:-$HOME/.config}/auto-office/install-choice"' in section
    assert 'cat "$CFG"' in section
    assert "in one shell command, set `CFG=" in section
    assert "prints `lean`" in section
    assert "(a trailing newline is fine)" in section
    assert 'CFG="${XDG_CONFIG_HOME:-$HOME/.config}/auto-office/install-choice"; mkdir -p "$(dirname "$CFG")" && printf lean >| "$CFG"' in section
    assert "the choice was not saved" in section
    assert "continue this request as **Lean for this request**" in section


def test_install_check_asks_once_before_installing():
    section = _install_check()
    not_found = section[section.index("- **Not found:**"):section.index("- **Install:**")]

    assert "ask once" in not_found
    assert "uv tool install auto-office" not in not_found


def test_install_check_installs_from_pypi_then_onboards_and_stops_on_failure():
    section = _install_check()

    assert "uv tool install auto-office" in section
    assert "office install --no-hooks" in section
    assert "office doctor" in section
    assert "Stop at the first failure" in section


def test_install_check_hands_off_to_the_lean_skill():
    section = _install_check()

    assert "auto-office:auto-office-lean" in section
    assert "skills/auto-office-lean/SKILL.md" in section
    assert "Either Lean choice: hand off" in section


def test_install_check_asks_before_repairing_a_failed_doctor():
    section = _install_check()
    found = section[section.index("- **Found:**"):section.index("- **Not found:**")]

    assert "If doctor fails, ask once, natively" in found
    assert "**Repair the install**" in found
    assert "repair only on yes" in found
    assert found.index("ask once, natively") < found.index("office install --no-hooks")


def test_install_check_checks_path_before_running_office_install():
    section = _install_check()
    install = section[section.index("- **Install:**"):section.index("- **Hand off:**")]

    assert "uv tool update-shell" in install
    assert install.index("command -v office") < install.index("`office install --no-hooks`")


def test_install_check_stops_on_uv_failure_before_checking_path():
    section = _install_check()
    install = section[section.index("- **Install:**"):section.index("- **Hand off:**")]
    uv_failure = install.index("If it fails, stop")
    path_check = install.index("`command -v office`")

    assert install.index("`uv tool install auto-office`") < uv_failure < path_check
    assert "exit status" in install[:uv_failure]
    assert "lean hand-off" in install[uv_failure:path_check]
    assert install.index("`uv tool update-shell`") > path_check


def test_saved_choice_write_command_saves_lean_under_xdg_config(tmp_path):
    env = {"PATH": os.environ["PATH"], "XDG_CONFIG_HOME": str(tmp_path / "cfg")}

    subprocess.run(["sh", "-c", _saved_choice_write_command()], env=env, check=True)

    assert (tmp_path / "cfg" / "auto-office" / "install-choice").read_text(encoding="utf-8") == "lean"


def test_saved_choice_write_failure_exits_nonzero(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("", encoding="utf-8")
    env = {"PATH": os.environ["PATH"], "XDG_CONFIG_HOME": str(blocker)}

    result = subprocess.run(["sh", "-c", _saved_choice_write_command()], env=env, capture_output=True)

    assert result.returncode != 0


def test_install_check_no_longer_stops_to_ask_approval():
    section = _install_check().lower()

    assert "stop and ask" not in section
    assert "approve installing" not in section


def test_install_check_keeps_stale_upgrade_guidance():
    section = _install_check()

    assert "install: STALE" in section
    assert "uv tool upgrade auto-office" in section
    assert "--force --reinstall" in section


def test_repository_plugin_skills_field_passes_ecosystem_check():
    assert check_plugin_skills(ROOT) == []


def _write_manifest(root, skills):
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "x", "skills": skills}), encoding="utf-8")


def test_ecosystem_check_accepts_relative_skill_directories(tmp_path):
    (tmp_path / "skills").mkdir()
    _write_manifest(tmp_path, ["./", "./skills/"])

    assert check_plugin_skills(tmp_path) == []


def test_ecosystem_check_rejects_paths_outside_the_plugin(tmp_path):
    (tmp_path / "plugin").mkdir()
    (tmp_path / "outside").mkdir()
    (tmp_path / "plugin" / "linked").symlink_to(tmp_path / "outside", target_is_directory=True)
    _write_manifest(tmp_path / "plugin", ["../outside", "./../outside", "./linked"])

    errors = check_plugin_skills(tmp_path / "plugin")

    assert errors == [
        ".claude-plugin/plugin.json: skills entry '../outside' must be a ./ directory inside the plugin",
        ".claude-plugin/plugin.json: skills entry './../outside' must be a ./ directory inside the plugin",
        ".claude-plugin/plugin.json: skills entry './linked' must be a ./ directory inside the plugin",
    ]


def test_ecosystem_check_rejects_absolute_and_file_entries(tmp_path):
    (tmp_path / "SKILL.md").write_text("---\n", encoding="utf-8")
    _write_manifest(tmp_path, [str(tmp_path), "./SKILL.md"])

    assert len(check_plugin_skills(tmp_path)) == 2


def test_ecosystem_check_reports_null_byte_entries_without_crashing(tmp_path):
    _write_manifest(tmp_path, ["./\x00"])

    assert check_plugin_skills(tmp_path) == [
        ".claude-plugin/plugin.json: skills entry './\\x00' must be a ./ directory inside the plugin",
    ]


def test_ecosystem_check_rejects_non_list_skills(tmp_path):
    _write_manifest(tmp_path, "./")

    assert check_plugin_skills(tmp_path) == [".claude-plugin/plugin.json: skills must be a list of paths"]
