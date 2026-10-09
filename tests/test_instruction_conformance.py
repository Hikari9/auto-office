"""#427: active instructions must not contradict the manifesto or the runtime contract.

Active surface: root SKILL.md, protocol/*.md, skills/*/SKILL.md, docs/orchestrator-reference.md.
3.0 spokes that say so in their banner are historical and may describe superseded rules.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _active():
    files = [ROOT / "SKILL.md", ROOT / "docs/orchestrator-reference.md", ROOT / "README.md"]
    files += sorted((ROOT / "protocol").glob("*.md"))
    files += sorted((ROOT / "skills").glob("*/SKILL.md"))
    return files


def _sentences(text):
    return re.split(r"(?<=[.;])\s+|\n\s*\n|\n- ", text)


def test_no_active_text_reruns_plan_review_after_amendments():
    bad = re.compile(r"(amend\w*|revision)[^.\n]{0,60}(re-?review|rerun plan review|queue[sd]? plan review)", re.I)
    ok = re.compile(r"\b(never|not|no|without|nor|only|doesn.t|does not|skip)\b", re.I)
    for f in _active():
        for s in _sentences(f.read_text(encoding="utf-8")):
            if bad.search(s) and not ok.search(s) and "3.0" not in s and "v3.1" not in s:
                raise AssertionError(f"{f.relative_to(ROOT)}: {s.strip()[:160]}")


def test_no_active_text_says_a_waiver_approves_or_grants_landing():
    bad = re.compile(r"waiv\w*[^.\n]{0,50}\b(becomes?|counts? as|is|equals?|grants?|manufactures?)\b[^.\n]{0,20}\b(APPROVED|approval|landing authority)\b", re.I)
    neg = re.compile(r"\b(never|not|no|nor|isn.t|without|≠)\b", re.I)
    for f in _active():
        for s in _sentences(f.read_text(encoding="utf-8")):
            if bad.search(s) and not neg.search(s):
                raise AssertionError(f"{f.relative_to(ROOT)}: {s.strip()[:160]}")


def test_hub_states_the_locked_review_rules():
    hub = " ".join((ROOT / "SKILL.md").read_text(encoding="utf-8").split())
    for needle in (
        "reviews the initial plan only",
        "a waiver is never an approval",
        "is not landing authority",
        "Unknown is never low",
        "different producer and reviewer session",
        "Specialists come first",
        "never queues plan review",
        "MANIFESTO.md",
    ):
        assert needle in hub, needle


def test_readme_and_help_point_to_the_manifesto():
    from office import cli
    assert "MANIFESTO.md" in (ROOT / "README.md").read_text(encoding="utf-8")
    assert "MANIFESTO.md" in cli.PRIMARY


def test_superseded_3_0_material_is_marked_historical():
    for rel in ("references/OFFICE-SKILLS-V3-SPEC.md", "references/OFFICE-SKILLS-V3-LIFECYCLE-SPEC.md",
                "docs/v3-runtime-contracts.md", "docs/v3-acceptance.md"):
        assert "Historical, reference-only" in (ROOT / rel).read_text(encoding="utf-8")[:1500], rel
