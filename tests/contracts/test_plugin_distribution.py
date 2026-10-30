"""#926 public plugin inventory and compatibility contract."""
from __future__ import annotations

import json
from pathlib import Path

from scripts.build_plugin_distribution import check, file_map

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
    assert "auto-switch to auto-office-lean" in text


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
    """Contract scenarios: reviewer unavailability never uses a round; any fixer is allowed."""
    skill = (ROOT / "skills/auto-review/SKILL.md").read_text().lower()
    scenarios = [
        ("orchestrator invoker", "separate executor", 3),
        ("executor invoker", "same executor", 3),
        ("orchestrator invoker", "specifically assigned other owner", 3),
    ]
    for invoker, fixer, max_rounds in scenarios:
        assert invoker and fixer
        assert "delegate" in skill and "fix inline" in skill and "other owner" in skill
        assert "changing fixer, caller or review surface never resets the cap" in skill
        assert "initial review is round 1" in skill
        assert "never round 4" in skill
        assert "consumes no substantive round" in skill
        assert max_rounds == 3


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
        else:
            assert "explicit" in description
        if name == "auto-office-lean":
            assert "never for ordinary engineering requests" in description
