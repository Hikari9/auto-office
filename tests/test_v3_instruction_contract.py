"""Contract tests for the v3 active instruction surface (T1).

These tests do not assert on exact prose. A sentence may be reworded freely as long as the
underlying behavior it commits an agent to does not regress. Each test targets one of the three
defect classes named in `docs/plans/v3-final-merge.md`'s T1 section: a surviving prohibition on
planner-user interaction, an unconditional review escalation, and a documented routing filter whose
input source is left unstated.
"""
import re
import unittest
from pathlib import Path

from scripts.check_ecosystem import ROOT

PROTOCOL_DIR = ROOT / 'protocol'
SKILLS_DIR = ROOT / 'skills'
REFERENCES_DIR = ROOT / 'references'


def _instruction_files():
    """The active instruction surface named in the T1 brief: SKILL.md, protocol/*.md,
    skills/*/SKILL.md, and the named reference specs (not every reference file — quota-probe.md
    and MIGRATION-V2.md are out of T1's scope and untouched)."""
    files = [ROOT / 'SKILL.md']
    files += sorted(PROTOCOL_DIR.glob('*.md'))
    files += sorted(SKILLS_DIR.glob('*/SKILL.md'))
    files += [
        REFERENCES_DIR / 'OFFICE-SKILLS-V3-SPEC.md',
        REFERENCES_DIR / 'OFFICE-SKILLS-V3-LIFECYCLE-SPEC.md',
        REFERENCES_DIR / 'IMPLEMENTATION-NOTES.md',
    ]
    files += sorted(REFERENCES_DIR.glob('why-*.md'))
    return [f for f in files if f.exists()]


def _paragraphs(text):
    """Split on blank lines; a paragraph is the unit a reader/agent would take a sentence's
    context from. Table rows are kept as individual paragraphs (split on newline) since a
    markdown table has no blank lines between rows but each row is its own semantic unit."""
    chunks = []
    for block in re.split(r'\n\s*\n', text):
        if '|' in block and block.count('\n') > 1:
            chunks.extend(line for line in block.split('\n') if line.strip())
        else:
            chunks.append(block)
    return chunks


SUPERSESSION_MARKERS = re.compile(
    r'supersed|overturn|overturned|older rule|void|no longer|does not describe a gap'
    r'|conflict register|closed as a non-goal',
    re.IGNORECASE,
)

# A live directive telling an agent the planner must not talk to the user.
PLANNER_PROHIBITION = re.compile(
    r"planner[^.\n]*(?:never talk|does not talk|doesn'?t talk|is not present|"
    r"not allowed to talk|silent(?:ly)? (?:regarding|about) the user)",
    re.IGNORECASE,
)

# A live directive telling an agent the orchestrator must run the interview / freeze intent
# before planning starts.
ORCHESTRATOR_PREFREEZE = re.compile(
    r"orchestrator (?:conducts|owns) this interview|"
    r"freeze the five[^.\n]*orchestrator|"
    r"frozen by the orchestrator",
    re.IGNORECASE,
)


class TestNoPlannerUserProhibitionSurvives(unittest.TestCase):
    """issue-35#decision-1 / issue-77 overturned issue-47's rule that the planner never talks to
    the user. A paragraph may still *describe* that overturned rule for historical context, but
    only if it is clearly marked as superseded/void in the same paragraph; otherwise an agent
    reading it would come away believing the prohibition is still live."""

    def test_no_live_planner_prohibition(self):
        offenders = []
        for path in _instruction_files():
            text = path.read_text(encoding='utf-8')
            for para in _paragraphs(text):
                if PLANNER_PROHIBITION.search(para) and not SUPERSESSION_MARKERS.search(para):
                    offenders.append(f'{path.relative_to(ROOT)}: {para.strip()[:200]}')
        self.assertEqual(offenders, [], 'live planner-user prohibition found:\n' + '\n'.join(offenders))

    def test_no_live_orchestrator_prefreeze_ownership(self):
        offenders = []
        for path in _instruction_files():
            text = path.read_text(encoding='utf-8')
            for para in _paragraphs(text):
                if ORCHESTRATOR_PREFREEZE.search(para) and not SUPERSESSION_MARKERS.search(para):
                    offenders.append(f'{path.relative_to(ROOT)}: {para.strip()[:200]}')
        self.assertEqual(offenders, [], 'orchestrator pre-freeze ownership found:\n' + '\n'.join(offenders))

    def test_planner_role_affirmatively_talks_to_the_user(self):
        text = (PROTOCOL_DIR / 'roles-and-authority.md').read_text(encoding='utf-8')
        m = re.search(r'## Planner\n(.*?)(?=\n## )', text, re.S)
        self.assertIsNotNone(m, 'protocol/roles-and-authority.md must have a Planner section')
        self.assertRegex(m.group(1), r'(directly with|talks? directly to) the user')


class TestTrackingIssuePrecedesPlanning(unittest.TestCase):
    """The v2 tracking guarantee is restored without moving the v3 planner's intent freeze."""

    def test_tracking_issue_is_required_before_planning_spoke(self):
        text = (ROOT / 'SKILL.md').read_text(encoding='utf-8')
        issue_pos = text.index('file-issue')
        planning_pos = text.index('check-spoke --state-dir <state_dir> --spoke auto-planning')
        self.assertLess(issue_pos, planning_pos)
        self.assertRegex(text[issue_pos:planning_pos], r'create or reuse exactly one tracking GitHub issue')
        self.assertRegex(text[issue_pos:planning_pos], r'family-update .*--issue')

    def test_tracking_issue_is_not_waiting_for_routine_approval(self):
        text = (ROOT / 'SKILL.md').read_text(encoding='utf-8')
        section = text[text.index('file-issue'):text.index('## Fixed lifecycle')]
        self.assertRegex(section, r'Do not ask for a draft or routine approval')


class TestNoUnconditionalReviewEscalation(unittest.TestCase):
    """issue-35#decision-4 / issue-77 §4-5,7: a final/integration adversary is triggered by the
    integration boundary (dependent/merging multi-executor landings), never by executor count
    alone and never as a default second pass over a single producer's work."""

    UNCONDITIONAL_ESCALATION = re.compile(
        r'(always require|mandatory) (?:an? )?(?:independent|second|final) review|'
        r'every landing (?:requires|receives) (?:an? )?(?:independent|final) review|'
        r'second review(?:er)? (?:is|for) (?:always|every)',
        re.IGNORECASE,
    )

    def test_no_unconditional_escalation_phrase(self):
        """A negated occurrence ('receives no mandatory second review') is the correct, conditional
        phrasing and must not itself be flagged — only an assertion of the phrase is a defect."""
        negation = re.compile(r'\b(?:no|never|not)\s+\w*\s*$', re.IGNORECASE)
        offenders = []
        for path in _instruction_files():
            text = path.read_text(encoding='utf-8')
            for para in _paragraphs(text):
                for m in self.UNCONDITIONAL_ESCALATION.finditer(para):
                    prefix = para[max(0, m.start() - 15):m.start()]
                    if not negation.search(prefix):
                        offenders.append(f'{path.relative_to(ROOT)}: {para.strip()[:200]}')
        self.assertEqual(offenders, [], 'unconditional review escalation found:\n' + '\n'.join(offenders))

    def test_integration_review_trigger_is_explicitly_conditional(self):
        """Wherever a file describes integration/final review, that same file must also name the
        actual trigger condition (dependent or merging landings from multiple executors) — the
        behavioral guard is that a real trigger condition is named somewhere nearby, not that one
        specific sentence restates it."""
        found_any = False
        offenders = []
        for path in _instruction_files():
            text = path.read_text(encoding='utf-8')
            if re.search(r'integration[_ ]adversary', text, re.IGNORECASE):
                found_any = True
                if not re.search(r'dependent|merging', text, re.IGNORECASE):
                    offenders.append(f'{path.relative_to(ROOT)}: mentions integration review but never says what triggers it')
        self.assertTrue(found_any, 'no instruction file documents integration_adversary at all')
        self.assertEqual(offenders, [], 'integration review trigger under-qualified:\n' + '\n'.join(offenders))

    def test_executor_owns_disposition_with_exceptional_consultation(self):
        """issue-35#decision-3: executor disposition ownership is exceptional-consultation, not a
        mandatory approval chain."""
        text = (PROTOCOL_DIR / 'roles-and-authority.md').read_text(encoding='utf-8')
        self.assertRegex(text, re.compile(r'exceptional', re.IGNORECASE))
        self.assertRegex(text, r'not a mandatory approval chain|not a routine')


class TestRoutingFilterInputsAreStated(unittest.TestCase):
    """Amendment v3: the filter order must say where each filter's input comes from, or an
    orchestrator can satisfy a trust/floor/reward gate by typing a value into its own request."""

    FILTER_ORDER = re.compile(
        r'hard exclusions.{0,40}adapter (?:validity(?:/| and )trust|trust)', re.IGNORECASE | re.DOTALL,
    )

    def test_every_filter_order_occurrence_has_derived_language_nearby(self):
        offenders = []
        checked = 0
        for path in _instruction_files():
            text = path.read_text(encoding='utf-8')
            for m in self.FILTER_ORDER.finditer(text):
                checked += 1
                window = text[m.start():m.start() + 2500]
                if not re.search(r'derived|never caller-supplied|route\(\) ignores', window, re.IGNORECASE):
                    offenders.append(f'{path.relative_to(ROOT)}: filter order at char {m.start()} has no derived-input language nearby')
        self.assertGreater(checked, 0, 'no instruction file documents the router filter order')
        self.assertEqual(offenders, [], '\n'.join(offenders))

    def test_adapter_trust_promotion_never_described_as_evidence_derived_alone(self):
        """Amendments v5/v6: a query may lower adapter trust and may never raise it. A document
        that says trust is 'derived from recorded evidence' without qualifying that promotion is
        an explicit recorded act reintroduces exactly the defect class five review rounds closed."""
        offenders = []
        for path in _instruction_files():
            text = path.read_text(encoding='utf-8')
            for para in _paragraphs(text):
                if re.search(r'adapter trust', para, re.IGNORECASE) and re.search(r'derived from recorded evidence', para, re.IGNORECASE):
                    qualified = re.search(
                        r'demotion only|never rais|only ever falls|falls? automatically|'
                        r'down(?:ward)? only|explicit.{0,30}(?:act|record)',
                        para, re.IGNORECASE,
                    )
                    if not qualified:
                        offenders.append(f'{path.relative_to(ROOT)}: {para.strip()[:200]}')
        self.assertEqual(offenders, [], 'unqualified evidence-derived trust claim:\n' + '\n'.join(offenders))

    def test_no_automatic_proven_from_dispatch_count(self):
        """A document must not claim that meeting a dispatch-count/task-shape bar is what makes an
        adapter proven — that bar is advisory-only; only a recorded trust act raises trust."""
        offenders = []
        pattern = re.compile(
            r'\bproven\b[^.\n]{0,120}(?:dispatch|task shape)|'
            r'(?:dispatch|task shape)[^.\n]{0,120}\bproven\b',
            re.IGNORECASE,
        )
        for path in _instruction_files():
            text = path.read_text(encoding='utf-8')
            for para in _paragraphs(text):
                if pattern.search(para):
                    qualified = re.search(
                        r'advisory|explicit|never (?:automatic|comput|infer)|not.{0,20}comput|'
                        r'consult before recording|recorded.{0,20}act',
                        para, re.IGNORECASE,
                    )
                    if not qualified:
                        offenders.append(f'{path.relative_to(ROOT)}: {para.strip()[:200]}')
        self.assertEqual(offenders, [], 'automatic proven-from-dispatch-count claim:\n' + '\n'.join(offenders))

    def test_override_path_is_documented_with_attribution_and_hard_stop(self):
        text = (PROTOCOL_DIR / 'routing.md').read_text(encoding='utf-8')
        self.assertRegex(text, re.compile(r'RecordedOverride|recorded[- ]override', re.IGNORECASE))
        self.assertRegex(text, r'attribution')
        self.assertRegex(text, r'hard stop|override_not_authorized')

    def test_trust_conformance_suite_named_as_normative(self):
        """Amendment v6: tests/test_trust_conformance.py is the normative artifact for the trust
        properties, not any prose/SQL example in a document."""
        text = (PROTOCOL_DIR / 'routing.md').read_text(encoding='utf-8')
        self.assertIn('test_trust_conformance.py', text)


class TestNoApprovalHookEnforcementReintroduced(unittest.TestCase):
    """issue-93's amendment withdrew the approval-hook-enforcement requirement. T1 must not
    reintroduce it as a live requirement anywhere in the instruction surface."""

    REINTRODUCTION = re.compile(
        r'(?:PreToolUse|approval hook)[^.\n]{0,80}(?:must (?:block|enforce|intercept)|enforces? approval)|'
        r'mechanical(?:ly)? enforc\w* (?:the )?approval',
        re.IGNORECASE,
    )

    def test_no_reintroduced_enforcement_requirement(self):
        offenders = []
        for path in _instruction_files():
            text = path.read_text(encoding='utf-8')
            for para in _paragraphs(text):
                if self.REINTRODUCTION.search(para) and not SUPERSESSION_MARKERS.search(para):
                    offenders.append(f'{path.relative_to(ROOT)}: {para.strip()[:200]}')
        self.assertEqual(offenders, [], 'reintroduced approval-hook enforcement requirement:\n' + '\n'.join(offenders))


class TestFamilyAmendmentAndCompactionConceptsDefined(unittest.TestCase):
    """issue-35#decisions 5-7 / issue-77: amendment ownership, sticky focus, and conditional
    compaction must exist somewhere in the active instruction surface — this was previously
    missing behavior, not a contradiction to resolve."""

    def test_families_and_amendments_protocol_file_exists_and_covers_the_three_kinds(self):
        path = PROTOCOL_DIR / 'families-and-amendments.md'
        self.assertTrue(path.exists(), 'protocol/families-and-amendments.md must exist')
        text = path.read_text(encoding='utf-8')
        self.assertRegex(text, r'[Rr]outing-only')
        self.assertRegex(text, r'[Rr]equirements delta')
        self.assertRegex(text, r'[Pp]lan-contract delta|plan_contract')
        self.assertRegex(text, re.compile(r'wakes?', re.IGNORECASE))

    def test_sticky_focus_defined(self):
        text = (PROTOCOL_DIR / 'families-and-amendments.md').read_text(encoding='utf-8')
        self.assertRegex(text, re.compile(r'sticky', re.IGNORECASE))
        self.assertRegex(text, r'unqualified command')
        self.assertRegex(text, r'ambiguous')

    def test_conditional_compaction_defined(self):
        text = (PROTOCOL_DIR / 'families-and-amendments.md').read_text(encoding='utf-8')
        self.assertRegex(text, re.compile(r'compact', re.IGNORECASE))
        self.assertRegex(text, re.compile(r'checkpoint', re.IGNORECASE))
        self.assertRegex(text, re.compile(r'conditional|not mandatory', re.IGNORECASE))


class TestReviewTierVocabularyMatchesT0Schema(unittest.TestCase):
    """The review-tier vocabulary T1 documents must match the enum T0 pinned in
    schemas/review-result.schema.json / docs/v3-runtime-contracts.md §2.6, not a reinvented one."""

    def test_review_mode_enum_present_in_verification_review_protocol(self):
        text = (PROTOCOL_DIR / 'verification-review.md').read_text(encoding='utf-8')
        for token in ('independent_adversary', 'labeled-inline', 'integration_adversary'):
            self.assertIn(token, text, f'missing review_mode value: {token}')

    def test_disposition_owner_enum_present(self):
        text = (PROTOCOL_DIR / 'verification-review.md').read_text(encoding='utf-8')
        self.assertRegex(text, r'disposition_owner')
        for token in ('executor', 'planner', 'orchestrator'):
            self.assertIn(token, text)


if __name__ == '__main__':
    unittest.main()
