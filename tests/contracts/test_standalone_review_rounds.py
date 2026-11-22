import pytest
from scripts.standalone_review_rounds import record, ReviewContractError

S = 'a' * 40

def case(ledger, verdict='RECHECK', **kw):
    return record(ledger, verdict=verdict, revision=kw.pop('revision', S),
                  reviewer_session=kw.pop('reviewer_session', 'fresh-reviewer'),
                  invoker_session=kw.pop('invoker_session', 'coordinator'),
                  producer_session=kw.pop('producer_session', 'coder'),
                  fix_owner_session=kw.pop('fix_owner_session', None),
                  read_only=kw.pop('read_only', True),
                  evidence=kw.pop('evidence', 'diff + findings'), **kw)

@pytest.mark.parametrize('invoker,fix_owner', [
    ('orchestrator', 'worker-2'), ('executor', 'executor'), ('orchestrator', 'designated-third-owner')
])
def test_remediation_owner_can_differ_from_invoker(invoker, fix_owner):
    ledger = case({}, invoker_session=invoker, fix_owner_session=fix_owner,
                  producer_session='producer')
    assert ledger['reviews'][0]['invoker_session'] == invoker
    assert ledger['reviews'][0]['fix_owner_session'] == fix_owner
    assert ledger['remaining'] == 2


def test_failed_launch_does_not_consume_and_3_rounds_cap_without_false_approval():
    ledger = case({}, verdict='UNAVAILABLE', evidence='CLI executable missing')
    assert len(ledger['unavailable']) == 1
    assert not ledger.get('reviews')
    for n in range(3):
        ledger = case(ledger, reviewer_session='fresh-reviewer-'+str(n))
        assert len(ledger['reviews']) == n + 1
    assert ledger['status'] == 'blocked-exhausted'
    assert ledger['remaining'] == 0
    with pytest.raises(ReviewContractError, match='exhausted'):
        case(ledger, verdict='APPROVED', reviewer_session='fresh-new-reviewer')


def test_intake_gap_requires_authoritative_decision():
    ledger = case({}, verdict='INTAKE_GAP', evidence='missing approval')
    assert ledger['status'] == 'owner-decision-needed'
    with pytest.raises(ReviewContractError, match='owner decision'):
        case(ledger, verdict='APPROVED')
    ledger = case(ledger, verdict='APPROVED', decision_resolved=True)
    assert ledger['status'] == 'approved'
    assert ledger['remaining'] == 1


def test_invalid_independence_and_permissions_fail_closed():
    for override in ({'reviewer_session': 'coordinator'}, {'reviewer_session': 'coder'},
                     {'fix_owner_session': 'fresh-reviewer'}, {'read_only': False},
                     {'evidence': ''}, {'revision': 'mutable-branch'}):
        with pytest.raises(ReviewContractError):
            case({}, **override)


def test_invoker_and_fix_owner_changes_cannot_reset_round_counter():
    ledger = {}
    for idx, (invoker, owner) in enumerate([
        ('orchestrator', 'first-delegate'),
        ('orchestrator', 'second-delegate'),
        ('executor', 'executor'),
    ]):
        ledger = case(ledger, reviewer_session=f'reviewer-{idx}',
                      invoker_session=invoker, fix_owner_session=owner)
        assert ledger['reviews'][-1]['round'] == idx + 1
    assert ledger['status'] == 'blocked-exhausted'
    with pytest.raises(ReviewContractError, match='exhausted'):
        case(ledger, verdict='APPROVED')


def test_no_review_is_recorded_after_approval():
    approved = case({}, verdict='APPROVED')
    assert approved['status'] == 'approved'
    with pytest.raises(ReviewContractError, match='already approved'):
        case(approved, reviewer_session='fresh-reviewer-2')


@pytest.mark.parametrize('field', ['reviewer_session', 'invoker_session', 'producer_session'])
@pytest.mark.parametrize('blank', ['', '   '])
def test_blank_identity_fails_closed(field, blank):
    with pytest.raises(ReviewContractError, match='missing independently identifiable sessions'):
        case({}, **{field: blank})


def test_session_identity_comparison_ignores_case_and_padding():
    with pytest.raises(ReviewContractError, match='distinct session'):
        case({}, reviewer_session=' Coordinator ', invoker_session='coordinator')


@pytest.mark.parametrize('override', [{'revision': None}, {'evidence': None}])
def test_non_string_revision_or_evidence_is_a_contract_error(override):
    with pytest.raises(ReviewContractError, match='must be strings'):
        case({}, **override)


def test_malformed_previous_entry_is_a_contract_error():
    with pytest.raises(ReviewContractError, match='invalid previous review ledger'):
        case({'reviews': ['not-a-review']})


def test_malformed_unavailable_list_is_a_contract_error():
    with pytest.raises(ReviewContractError, match='invalid previous review ledger'):
        case({'reviews': [], 'unavailable': None}, verdict='UNAVAILABLE', evidence='no CLI')


def test_ledger_stores_the_callers_spelling_and_compares_case_insensitively():
    ledger = case({}, reviewer_session='  Rev-ABC ', invoker_session='coordinator')
    assert ledger['reviews'][0]['reviewer_session'] == 'Rev-ABC'
    with pytest.raises(ReviewContractError, match='distinct session'):
        case({}, reviewer_session='COORDINATOR', invoker_session='coordinator')


def test_fix_owner_must_be_a_string_and_blank_means_no_owner():
    with pytest.raises(ReviewContractError, match='ASCII'):
        case({}, fix_owner_session=5)
    ledger = case({}, fix_owner_session='   ')
    assert ledger['reviews'][0]['fix_owner_session'] is None


def test_fix_owner_identity_is_compared_normalized():
    with pytest.raises(ReviewContractError, match='distinct session'):
        case({}, reviewer_session='Fixer', fix_owner_session=' fixer ')


@pytest.mark.parametrize('field', ['reviewer_session', 'invoker_session', 'producer_session', 'fix_owner_session'])
@pytest.mark.parametrize('lookalike', ['coordinator​', 'coordinаtor'])
def test_non_ascii_identities_fail_closed(field, lookalike):
    overrides = {field: lookalike}
    if field == 'reviewer_session':
        overrides['fix_owner_session'] = None
    with pytest.raises(ReviewContractError, match='ASCII'):
        case({}, **overrides)
