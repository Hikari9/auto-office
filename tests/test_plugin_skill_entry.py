"""The Claude plugin ships the root auto-office skill, and its install check detects the CLI without blocking."""
import json
import os
import re
import subprocess
from pathlib import Path

from scripts.check_ecosystem import check_plugin_skills, frontmatter

ROOT = Path(__file__).resolve().parents[1]


def _hub():
    return (ROOT / "SKILL.md").read_text(encoding="utf-8")


def _install_check():
    text = _hub()
    return text[text.index("## Install check"):text.index("## First-run onboarding")]


def _saved_choice_write_command():
    return re.search(r"\(in one shell command: `([^`]+)`", _install_check()).group(1)


def _saved_choice_read_command():
    set_cfg = re.search(r"set `(CFG=[^`]+)`", _install_check()).group(1)
    return f'{set_cfg}; cat "$CFG"'


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

    assert "ask once per session" in not_found
    assert not_found.index('cat "$CFG"') < not_found.index("ask once per session")
    assert "uv tool install git+https://github.com/Hikari9/auto-office" not in not_found


def test_install_check_installs_from_github_then_onboards_and_stops_on_failure():
    section = _install_check()

    assert "`uv tool install git+https://github.com/Hikari9/auto-office`" in section
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
    assert "**Lean for this request** (hand off now)" in found
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

    assert install.index("`uv tool install git+https://github.com/Hikari9/auto-office`") < uv_failure < path_check
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


def test_saved_choice_read_prints_lean_when_saved(tmp_path):
    cfg_dir = tmp_path / "cfg" / "auto-office"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "install-choice").write_text("lean\n", encoding="utf-8")
    env = {"PATH": os.environ["PATH"], "XDG_CONFIG_HOME": str(tmp_path / "cfg")}

    result = subprocess.run(["sh", "-c", _saved_choice_read_command()], env=env, capture_output=True, text=True, check=True)

    assert result.stdout.strip() == "lean"


def test_saved_choice_read_prints_nothing_when_absent(tmp_path):
    env = {"PATH": os.environ["PATH"], "XDG_CONFIG_HOME": str(tmp_path / "cfg")}

    result = subprocess.run(["sh", "-c", _saved_choice_read_command()], env=env, capture_output=True, text=True)

    assert result.returncode != 0
    assert result.stdout == ""


def test_saved_choice_falls_back_to_home_config_when_xdg_unset(tmp_path):
    cfg_dir = tmp_path / ".config" / "auto-office"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "install-choice").write_text("lean", encoding="utf-8")
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}

    result = subprocess.run(["sh", "-c", _saved_choice_read_command()], env=env, capture_output=True, text=True, check=True)

    assert result.stdout.strip() == "lean"


def test_install_check_no_longer_stops_to_ask_approval():
    section = _install_check().lower()

    assert "stop and ask" not in section
    assert "approve installing" not in section
    assert "only after the user picks **install the cli**, run `uv tool install git+https://github.com/hikari9/auto-office`" in section


def test_install_check_explains_resetting_the_saved_choice():
    section = _install_check()
    not_found = section[section.index("- **Not found:**"):section.index("- **Install:**")]

    assert 'Only if the user asks to be asked again, run `rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/auto-office/install-choice"`' in not_found


def test_onboarding_doc_explains_resetting_the_saved_choice():
    doc = (ROOT / "docs" / "onboarding.md").read_text(encoding="utf-8")

    assert "to ask again, delete that file" in doc


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
    (tmp_path / "plugin" / "skills").mkdir()
    (tmp_path / "plugin" / "linked").symlink_to(tmp_path / "outside", target_is_directory=True)
    _write_manifest(tmp_path / "plugin", ["./", "./skills/", "../outside", "./../outside", "./linked"])

    errors = check_plugin_skills(tmp_path / "plugin")

    assert errors == [
        ".claude-plugin/plugin.json: skills entry '../outside' must be a ./ directory inside the plugin",
        ".claude-plugin/plugin.json: skills entry './../outside' must be a ./ directory inside the plugin",
        ".claude-plugin/plugin.json: skills entry './linked' must be a ./ directory inside the plugin",
    ]


def test_ecosystem_check_rejects_absolute_and_file_entries(tmp_path):
    (tmp_path / "SKILL.md").write_text("---\n", encoding="utf-8")
    (tmp_path / "skills").mkdir()
    _write_manifest(tmp_path, ["./", "./skills/", str(tmp_path), "./SKILL.md"])

    assert check_plugin_skills(tmp_path) == [
        f".claude-plugin/plugin.json: skills entry {str(tmp_path)!r} must be a ./ directory inside the plugin",
        ".claude-plugin/plugin.json: skills entry './SKILL.md' must be a ./ directory inside the plugin",
    ]


def test_ecosystem_check_reports_null_byte_entries_without_crashing(tmp_path):
    (tmp_path / "skills").mkdir()
    _write_manifest(tmp_path, ["./", "./skills/", "./\x00"])

    assert check_plugin_skills(tmp_path) == [
        ".claude-plugin/plugin.json: skills entry './\\x00' must be a ./ directory inside the plugin",
    ]


def test_ecosystem_check_rejects_non_list_skills(tmp_path):
    _write_manifest(tmp_path, "./")

    assert check_plugin_skills(tmp_path) == [".claude-plugin/plugin.json: skills must be a list of paths"]


# The only bare-name installs allowed are the README's two "never" warnings, matched by path and exact preceding text.
ALLOWED_WARNINGS = {("README.md", "unrelated project, so never `"), ("README.md", "never `uv tool install auto-office` or `")}
INSTALL = re.compile(r"(?:uv tool install|uv pip install|pipx install|pip install)\b[^`\n)]*")
BARE_REQUIREMENT = re.compile(r"(?<![\w/@.+-])['\"]?auto-office(?:\[[\w,]+\])?['\"]?(?![\w/-]|\.\w)")


def _pypi_installs(text: str) -> list[tuple[int, str]]:
    """Install commands that resolve the bare PyPI name: an `auto-office` requirement and no `git+` or `@` source."""
    found = []
    for command in INSTALL.finditer(text):
        span = command.group(0)
        if "git+" in span or " @ " in span:
            continue
        if BARE_REQUIREMENT.search(span):
            found.append((command.start(), span.strip()))
    return found


def test_nothing_installs_or_checks_the_unrelated_pypi_auto_office():
    """`auto-office` on PyPI is an unrelated project: no install command or update check may point at it (#924)."""
    roots = [ROOT / "SKILL.md", ROOT / "README.md", ROOT / "AGENTS.md", ROOT / "CLAUDE.md"]
    for folder, pattern in (("docs", "*.md"), ("skills", "*.md"), ("scripts", "*"), ("src", "*.py"), ("config", "*")):
        roots += [p for p in (ROOT / folder).rglob(pattern) if p.is_file()]
    offenders = []
    for path in roots:
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8", errors="ignore")
        offenders += [f"{rel}: {span}" for start, span in _pypi_installs(text)
                      if not any(rel == name and text[:start].endswith(before) for name, before in ALLOWED_WARNINGS)]
        if "pypi.org/pypi/auto-office" in text:
            offenders.append(f"{rel}: pypi.org/pypi/auto-office")
    assert not offenders


def test_the_pypi_install_guard_catches_every_bare_shape():
    for bare in ("run `uv tool install auto-office` and", "`uv tool install --force auto-office` for a release",
                 "uv tool install 'auto-office[visual]'", "pipx install auto-office.", "uv pip install -U auto-office",
                 "uv tool install --python 3.12 auto-office", "uv tool install --python=3.12 auto-office",
                 'uv tool install --python "3.12" auto-office', "uv tool install --with playwright 'auto-office[visual]'",
                 "uv tool install --reinstall-package auto-office auto-office", "pip install auto-office==0.0.3"):
        assert _pypi_installs(bare), bare
    for sourced in ('uv tool install --force --reinstall-package auto-office "auto-office[visual] @ <checkout>"',
                    "uv tool install git+https://github.com/Hikari9/auto-office",
                    "uv tool install 'auto-office[visual] @ git+https://github.com/Hikari9/auto-office'",
                    "uv tool install --editable <path/to/auto-office checkout>"):
        assert not _pypi_installs(sourced), sourced


def test_update_check_reads_the_release_from_github_main(monkeypatch):
    import io
    from office import self_update

    seen = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout):
        seen.append(request.full_url)
        return Response(b'"""Doc."""\n__version__ = "9.8.7"\n')

    monkeypatch.setattr(self_update.urllib.request, "urlopen", fake_urlopen)
    assert self_update._latest_release() == "9.8.7"
    assert seen == [self_update.RELEASE_VERSION_URL]
    assert seen[0].startswith("https://raw.githubusercontent.com/Hikari9/auto-office/main/")
