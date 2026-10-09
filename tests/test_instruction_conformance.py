"""#427: active instructions must not contradict the manifesto or the runtime contract.

Active surface: root SKILL.md, protocol/*.md, skills/*/SKILL.md, docs/orchestrator-reference.md, README.md.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

NEG = re.compile(r"\b(never|not|no|nor|without|isn.t|doesn.t|cannot|can.t)\b|≠", re.I)
# A negation within this many characters before the match flips it into a legitimate statement.
WINDOW = 40

RERUN = [
    re.compile(r"(amend\w*|revis\w*)\W+(?:\w+\W+){0,8}?(re-?review\w*|plan review (?:runs|reruns|is queued|happens) again|reruns? plan review|queues? plan review)", re.I),
    re.compile(r"(re-?review\w*|reruns? plan review|plan review (?:runs|reruns) again)\W+(?:\w+\W+){0,8}?(after|following|on)\W+(?:\w+\W+){0,3}?(amend\w*|revis\w*)", re.I),
]
WAIVER = [
    re.compile(r"waiv\w*\W+(?:\w+\W+){0,6}?(becomes?|counts? as|is|equals?|grants?|manufactures?|serves? as)\W+(?:\w+\W+){0,3}?(APPROVED|approval|landing authority)", re.I),
    re.compile(r"waiv\w*\W+(?:\w+\W+){0,6}?(?<!office )(approves?|authori[sz]es?)\b", re.I),
]


def _hits(patterns, sentence):
    for p in patterns:
        for m in p.finditer(sentence):
            if not NEG.search(sentence[max(0, m.start() - WINDOW):m.end()]):
                return True
    return False


def _active():
    files = [ROOT / "SKILL.md", ROOT / "docs/orchestrator-reference.md", ROOT / "README.md"]
    files += sorted((ROOT / "protocol").glob("*.md"))
    files += sorted((ROOT / "skills").glob("*/SKILL.md"))
    return files


def _sentences(text):
    return re.split(r"(?<=[.;])\s+|\n\s*\n|\n- ", text)


def _scan(patterns):
    for f in _active():
        for s in _sentences(f.read_text(encoding="utf-8")):
            if _hits(patterns, s) and "v3.1" not in s and "3.0" not in s:
                raise AssertionError(f"{f.relative_to(ROOT)}: {s.strip()[:160]}")


@pytest.mark.parametrize("sentence", [
    "After every amendment, plan review runs again.",
    "Re-review the plan after each amendment.",
    "Each amendment queues plan review.",
    "Re-review after amendment; not optional.",
    "Amendments reopen plan review and rerun plan review.",
])
def test_rerun_lint_catches_bad_sentences(sentence):
    assert _hits(RERUN, sentence), sentence


@pytest.mark.parametrize("sentence", [
    "Plan review reviews the initial plan only: once it closes, no amendment is reviewed again.",
    "It never queues plan review.",
    "Ordinary amendments are yours and are not re-reviewed.",
])
def test_rerun_lint_allows_legitimate_sentences(sentence):
    assert not _hits(RERUN, sentence), sentence


@pytest.mark.parametrize("sentence", [
    "A waiver counts as approval.",
    "Waiving the cap approves the work.",
    "A waiver becomes APPROVED.",
    "The waiver is landing authority.",
])
def test_waiver_lint_catches_bad_sentences(sentence):
    assert _hits(WAIVER, sentence), sentence


@pytest.mark.parametrize("sentence", [
    "A waiver is never an approval.",
    "The verdict stays RECHECK and a waiver is not landing authority.",
    "The round-cap waiver never becomes APPROVED.",
])
def test_waiver_lint_allows_legitimate_sentences(sentence):
    assert not _hits(WAIVER, sentence), sentence


def test_no_active_text_reruns_plan_review_after_amendments():
    _scan(RERUN)


def test_no_active_text_says_a_waiver_approves_or_grants_landing():
    _scan(WAIVER)


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
        "Runs started before #423 record it as degraded and non-independent",
    ):
        assert needle in hub, needle


def test_readme_and_help_point_to_the_manifesto():
    from office import cli
    assert "MANIFESTO.md" in (ROOT / "README.md").read_text(encoding="utf-8")
    assert "MANIFESTO.md" in cli.PRIMARY


def test_superseded_material_is_marked_historical_or_scoped():
    for rel in ("references/OFFICE-SKILLS-V3-SPEC.md", "references/OFFICE-SKILLS-V3-LIFECYCLE-SPEC.md",
                "docs/v3-runtime-contracts.md", "docs/v3-acceptance.md"):
        assert "Historical, reference-only" in (ROOT / rel).read_text(encoding="utf-8")[:1500], rel
    for rel in ("protocol/verification-review.md", "protocol/lifecycle.md", "protocol/families-and-amendments.md"):
        head = (ROOT / rel).read_text(encoding="utf-8")[:900]
        assert "MANIFESTO.md" in head or "3.0" in head and "3.1+" in head, rel


def test_hub_linked_docs_ship_in_the_wheel():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for rel in ("docs/orchestrator-reference.md", "docs/review-convergence.md"):
        assert f'"{rel}" = "office/_resources/{rel}"' in text, rel
