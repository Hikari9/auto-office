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
