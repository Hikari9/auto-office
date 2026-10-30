"""The Claude plugin ships the root auto-office skill, and its install check detects the CLI without blocking."""
import json
from pathlib import Path

from scripts.check_ecosystem import check_plugin_skills, frontmatter

ROOT = Path(__file__).resolve().parents[2]


def _hub():
    return (ROOT / "SKILL.md").read_text(encoding="utf-8")


def _install_check():
    text = _hub()
    return text[text.index("## Install check"):text.index("## First-run onboarding")]


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
    assert "mkdir -p" in section
    assert "exactly `lean`" in section


def test_install_check_asks_once_before_installing():
    section = _install_check()

    assert "ask once" in section
    assert section.index("Install the CLI (Recommended)") < section.index("`uv tool install auto-office`")


def test_install_check_installs_from_pypi_then_onboards_and_stops_on_failure():
    section = _install_check()

    assert "uv tool install auto-office" in section
    assert "office install --no-hooks" in section
    assert "office doctor" in section
    assert "Stop at the first failure" in section


def test_install_check_hands_off_to_the_lean_skill():
    assert "auto-office:auto-office-lean" in _install_check()


def test_install_check_no_longer_stops_to_ask_approval():
    section = _install_check().lower()

    assert "stop and ask" not in section
    assert "approve installing" not in section


def test_install_check_keeps_stale_upgrade_guidance():
    section = _install_check()

    assert "install: STALE" in section
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
    _write_manifest(tmp_path / "plugin", ["./../outside", "./linked"])

    errors = check_plugin_skills(tmp_path / "plugin")

    assert errors == [
        ".claude-plugin/plugin.json: skills entry './../outside' must be a ./ directory inside the plugin",
        ".claude-plugin/plugin.json: skills entry './linked' must be a ./ directory inside the plugin",
    ]


def test_ecosystem_check_rejects_absolute_and_file_entries(tmp_path):
    (tmp_path / "SKILL.md").write_text("---\n", encoding="utf-8")
    _write_manifest(tmp_path, [str(tmp_path), "./SKILL.md"])

    assert len(check_plugin_skills(tmp_path)) == 2


def test_ecosystem_check_rejects_non_list_skills(tmp_path):
    _write_manifest(tmp_path, "./")

    assert check_plugin_skills(tmp_path) == [".claude-plugin/plugin.json: skills must be a list of paths"]
