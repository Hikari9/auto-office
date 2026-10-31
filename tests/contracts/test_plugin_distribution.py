"""#926 public plugin inventory and compatibility contract."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.build_plugin_distribution import check, file_map, retired_reference_errors
from scripts.standalone_review_rounds import MAX_SUBSTANTIVE_ROUNDS, record

ROOT = Path(__file__).resolve().parents[2]
CFG = json.loads((ROOT / "plugin-distribution.json").read_text())


def test_curated_installed_package_has_exact_visible_skills():
    assert check(ROOT) == []
    dist = ROOT / "plugin-dist" / "skills"
    visible = {p.parent.name for p in dist.glob("*/SKILL.md")}
    assert visible == set(CFG["public_skills"])
    assert len(visible) == 16
    assert not visible.intersection(CFG["retired_skills"])


def test_original_full_skill_is_the_only_canonical_root():
    assert (ROOT / "SKILL.md").read_bytes() == (
        ROOT / "plugin-dist/skills/auto-office/SKILL.md"
    ).read_bytes()
    text = (ROOT / "SKILL.md").read_text()
    assert "name: auto-office" in text
    assert "Original full Auto Office" in text
    assert "## Start" in text and "## Execute" in text
    assert "auto-office-lean" in (ROOT / "plugin-dist/skills/auto-office-lean/SKILL.md").read_text()
    assert "Lean, don\'t ask again" in text
    assert "only after the user picks **Install the CLI**" in text


def test_all_retired_skills_and_primitive_manifest_paths_remain():
    for name in CFG["retired_skills"]:
        assert (ROOT / "skills" / name / "SKILL.md").is_file()
        assert not (ROOT / "plugin-dist/skills" / name).exists()
    tsv = (ROOT / "config/cli-runtimes.tsv").read_text()
    for name, slug in [("opencode", "opencode-cli"), ("pi", "pi-cli")]:
        assert f"{name}\tskills/{slug}/SKILL.md\t" in tsv
        assert (ROOT / "plugin-dist/skills" / slug / "agents/openai.yaml").is_file()


def test_plugin_marketplaces_point_at_isolated_package():
    claude = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
    codex = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text())
    assert claude["plugins"][0]["source"] == "./plugin-dist"
    assert codex["plugins"][0]["source"]["path"] == "./plugin-dist"
    assert (ROOT / "plugin-dist/.claude-plugin/plugin.json").is_file()
    assert (ROOT / "plugin-dist/plugin.json").is_file()


def test_standalone_review_independence_delegation_and_bounded_rounds():
    text = (ROOT / "skills/auto-review/SKILL.md").read_text()
    for phrase in [
        "fresh", "read-only", "Herdr", "CLI session", "immutable",
        "APPROVED", "RECHECK", "INTAKE_GAP", "initial", "round 3",
        "need NOT personally fix", "delegate", "other owner", "UNAVAILABLE",
        "no hidden waiver", "NOT another Office lifecycle",
    ]:
        assert phrase.lower() in text.lower(), phrase


def test_installed_resources_byte_match_source():
    desired = file_map(ROOT)
    assert desired
    for name in ("scripts/build_plugin_distribution.py", "MANIFESTO.md", "adapters/seed/pi.yaml"):
        assert (ROOT / "plugin-dist" / name).read_bytes() == desired[name]


def test_generated_distribution_preserves_executable_support_scripts():
    for path in ("scripts/validate.sh", "scripts/office_spawn.sh", "scripts/office_runtime.py"):
        source = ROOT / path
        shipped = ROOT / "plugin-dist" / path
        assert bool(source.stat().st_mode & 0o100) == bool(shipped.stat().st_mode & 0o100)


def test_review_rounds_account_for_unavailable_and_owner_delegation():
    """Drive the round guard for each delegation shape; the SKILL cap must match the code."""
    assert MAX_SUBSTANTIVE_ROUNDS == 3
    sha = "b" * 40
    for invoker, fixer in [
        ("orchestrator", "separate-executor"),
        ("executor", "executor"),
        ("orchestrator", "assigned-other-owner"),
    ]:
        ledger = record({}, verdict="UNAVAILABLE", revision=sha, reviewer_session="r-down",
                        invoker_session=invoker, producer_session="producer", evidence="no CLI")
        assert not ledger.get("reviews")  # an unavailable launch consumes no round
        for n in range(1, MAX_SUBSTANTIVE_ROUNDS + 1):
            ledger = record(ledger, verdict="RECHECK", revision=sha, reviewer_session=f"r{n}",
                            invoker_session=invoker, producer_session="producer",
                            fix_owner_session=fixer, read_only=True, evidence="findings")
            assert ledger["reviews"][-1]["round"] == n
        with pytest.raises(ValueError, match="exhausted"):
            record(ledger, verdict="RECHECK", revision=sha, reviewer_session="r-extra",
                   invoker_session=invoker, producer_session="producer", read_only=True,
                   evidence="again")


def test_skill_text_states_the_round_and_standards_rules():
    """Two normative rules must stay in the skill: the cap wording and the trusted-base standards."""
    skill = (ROOT / "skills/auto-review/SKILL.md").read_text()
    assert "at most three substantive rounds" in skill.lower()
    assert "never from the reviewed revision" in skill
    assert "never interpolated into a shell string" in skill


def _fixture_root(root: Path, public=None, retired=None) -> Path:
    """Minimal source tree that file_map accepts: the real config, plus stub skill and support files."""
    root.mkdir(parents=True, exist_ok=True)
    real = json.loads((ROOT / "plugin-distribution.json").read_text())
    public = list(real["public_skills"] if public is None else public)
    retired = list(real["retired_skills"] if retired is None else retired)
    (root / "plugin-distribution.json").write_text(json.dumps(
        {"public_skills": public, "retired_skills": retired, "support_roots": [], "support_files": []}))
    for name in ("AGENTS.md", "CLAUDE.md", "CONTEXT.md", "MANIFESTO.md", "README.md", "plugin.json"):
        (root / name).write_text("x\n")
    (root / ".claude-plugin").mkdir()
    (root / ".claude-plugin/plugin.json").write_text("{}\n")
    (root / "SKILL.md").write_text("---\nname: auto-office\ndescription: x\n---\n")
    for name in dict.fromkeys(public + retired):
        if name == "auto-office":
            continue
        (root / "skills" / name).mkdir(parents=True, exist_ok=True)
        (root / "skills" / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: x\n---\n")
    for name in ("adapters", "catalog", "config", "docs", "protocol", "references", "schemas", "scripts"):
        (root / name).mkdir(exist_ok=True)
    return root


def test_fixture_source_tree_builds(tmp_path):
    root = _fixture_root(tmp_path / "ok")
    assert "skills/auto-office/SKILL.md" in file_map(root)


def test_file_symlink_inside_a_skill_is_rejected(tmp_path):
    root = _fixture_root(tmp_path / "linked")
    (root / "skills/agy-cli/notes.md").symlink_to(root / "README.md")
    with pytest.raises(ValueError, match="symlinks are not allowed"):
        file_map(root)


def test_symlinked_skill_directory_is_rejected(tmp_path):
    root = _fixture_root(tmp_path / "linked-dir")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("---\nname: agy-cli\ndescription: x\n---\n")
    skill = root / "skills/agy-cli"
    for child in skill.iterdir():
        child.unlink()
    skill.rmdir()
    skill.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks are not allowed"):
        file_map(root)


def test_config_path_traversal_is_rejected(tmp_path):
    root = _fixture_root(tmp_path / "traversal")
    cfg = json.loads((root / "plugin-distribution.json").read_text())
    cfg["support_files"] = ["../../escape.txt"]
    (root / "plugin-distribution.json").write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match="escapes the repository"):
        file_map(root)


def test_invalid_skill_name_in_config_is_rejected(tmp_path):
    root = _fixture_root(tmp_path / "badname")
    cfg = json.loads((root / "plugin-distribution.json").read_text())
    cfg["public_skills"][0] = "../auto-office"
    (root / "plugin-distribution.json").write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match="invalid skill name"):
        file_map(root)


def test_check_reports_build_errors_as_fail_lines_not_tracebacks(tmp_path):
    root = _fixture_root(tmp_path / "check-err")
    (root / "skills/agy-cli/notes.md").symlink_to(root / "README.md")
    errors = check(root)
    assert errors and errors[0].startswith("plugin-dist: symlinks are not allowed")


def test_builder_rejects_a_public_list_that_is_not_exactly_sixteen(tmp_path):
    root = _fixture_root(tmp_path / "short", public=["auto-office", "agy-cli"])
    with pytest.raises(ValueError, match="exactly 16"):
        file_map(root)


def test_retired_reference_forms_are_flagged(tmp_path):
    dist = tmp_path / "dist"
    forms = {
        "backtick": "Run `auto-planning` first.",
        "relative-path": "See ../auto-loop/SKILL.md.",
        "repo-path": "Read skills/auto-adapter/SKILL.md.",
        "slash-command": "Invoke /auto-loop to continue.",
        "plugin-qualified": "Use auto-office:office-submit for submit.",
        "plain-prose": "Then the auto-planning skill decides sizes.",
    }
    for label, text in forms.items():
        (dist / "skills" / label).mkdir(parents=True, exist_ok=True)
        (dist / "skills" / label / "SKILL.md").write_text(text + "\n")
    (dist / "skills/nested/references").mkdir(parents=True)
    (dist / "skills/nested/references/guide.md").write_text("Old: auto-verification.\n")
    retired = ["auto-planning", "auto-loop", "auto-adapter", "office-submit", "auto-verification"]
    errors = retired_reference_errors(dist, retired)
    flagged = {line.split(": references retired skill")[0] for line in errors}
    assert {f"skills/{label}/SKILL.md" for label in forms} <= flagged
    assert "skills/nested/references/guide.md" in flagged


def test_retired_reference_check_is_wired_into_check(tmp_path):
    root = _fixture_root(tmp_path / "wired")
    (root / ".agents/plugins").mkdir(parents=True)
    (root / ".claude-plugin/marketplace.json").write_text(
        json.dumps({"plugins": [{"source": "./plugin-dist"}]}))
    (root / ".agents/plugins/marketplace.json").write_text(
        json.dumps({"plugins": [{"source": {"path": "./plugin-dist"}}]}))
    (root / "plugin-dist").mkdir()
    file_map_result = file_map(root)
    for path, content in file_map_result.items():
        target = root / "plugin-dist" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    (root / "plugin-dist/skills/agy-cli/SKILL.md").write_text(
        "---\nname: agy-cli\ndescription: x\n---\nRun `auto-planning` first.\n")
    errors = check(root)
    assert any("references retired skill auto-planning" in e for e in errors), errors


def test_opt_in_only_skill_descriptions():
    for name in ("auto-office", "auto-office-lean", "auto-intake", "auto-review", "auto-takeover"):
        text = (ROOT / "plugin-dist/skills" / name / "SKILL.md").read_text()
        description = text.split("\n---\n", 1)[0].lower()
        if name == "auto-takeover":
            assert "only an explicit user request" in description
        if name != "auto-review":
            assert "disable-model-invocation: true" in description
        else:
            assert "disable-model-invocation" not in description  # Agents must invoke /auto-review explicitly
        assert "explicit" in description
        if name == "auto-office-lean":
            assert "never for ordinary engineering requests" in description
