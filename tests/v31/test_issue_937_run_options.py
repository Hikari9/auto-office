"""#937: confirmed live run option amendments persist, reject unsafe changes and affect PR jobs."""
import json

from conftest import GOOD_ADD, start_inline
from test_task_prs import github, gh


def _events(env):
    con = env.con()
    try:
        return [json.loads(r[0]) for r in con.execute("SELECT payload_json FROM events WHERE kind='run.options_amended'")]
    finally:
        con.close()


def _prs(env):
    con = env.con()
    try:
        return json.loads(con.execute('SELECT landing_json FROM runs').fetchone()[0])['prs']
    finally:
        con.close()


def _enable(env):
    return env.office('amend', 'options', '--prs', '--quote', 'User approved enabling PRs', '--confirm')


def _ready(env):
    env.trust()
    env.script(executor=[{'write': {'calc.py': GOOD_ADD}, 'submit': True}],
               convergence_reviewer=[{'reply': 'VERDICT: APPROVED\nNEXT proceed'}])


def test_quote_and_confirmation_required_and_audited(env, monkeypatch):
    github(env, monkeypatch)
    _ready(env)
    start_inline(env, extra=('--no-prs',))
    for flags, reason in [(('--prs',), 'user-authorization-required'),
                          (('--prs', '--quote', 'User said yes'), 'confirmation-required')]:
        code, out = env.office('amend', 'options', *flags)
        assert code == 4 and reason in out, out
        assert _prs(env)['enabled'] is False and _events(env) == []
    code, out = _enable(env)
    assert code == 0 and 'enabled' in out, out
    assert _prs(env)['enabled'] and _prs(env)['base_branch'] == 'main'
    history = _events(env)
    assert len(history) == 1 and history[0]['before']['enabled'] is False
    assert history[0]['after']['enabled'] is True
    assert history[0]['option'] == 'prs.enabled' and history[0]['confirmed'] is True
    assert history[0]['quote'] == 'User approved enabling PRs'
    code, data = env.ojson('inspect', 'run')
    assert data['data']['run']['landing']['prs']['enabled'] is True
    assert _enable(env)[0] == 0 and len(_events(env)) == 1


def test_enable_mid_run_creates_pr_for_subsequent_dispatch(env, monkeypatch):
    github(env, monkeypatch)
    _ready(env)
    start_inline(env, extra=('--no-prs',))
    env.office('approve', 'plan', '--quote', 'go', check=0)
    _enable(env)
    env.office('dispatch', 'T1', check=0)
    assert len(gh(env)['prs']) == 1


def test_enable_after_acceptance_backfills_ready_pr(env, monkeypatch):
    github(env, monkeypatch)
    _ready(env)
    start_inline(env, extra=('--no-prs',))
    env.office('approve', 'plan', '--quote', 'go', check=0)
    env.office('dispatch', 'T1', check=0)
    assert not (env.tmp / 'gh.json').exists() or not gh(env)['prs']
    code, out = _enable(env)
    assert code == 0 and 'queued 2' in out, out
    assert len(gh(env)['prs']) == 1 and not gh(env)['prs'][0]['draft']
    assert any('accepted' in c for c in gh(env)['prs'][0]['comments'])


def test_disable_before_dispatch_stops_future_prs(env, monkeypatch):
    github(env, monkeypatch)
    _ready(env)
    start_inline(env)
    assert _prs(env)['enabled'] is True
    code, out = env.office('amend', 'options', '--no-prs', '--quote', 'Keep it local', '--confirm')
    assert code == 0 and 'disabled' in out, out
    env.office('approve', 'plan', '--quote', 'go', check=0)
    env.office('dispatch', 'T1', check=0)
    assert not (env.tmp / 'gh.json').exists() or not gh(env)['prs'] and _prs(env)['enabled'] is False
    assert _events(env)[0]['before']['enabled'] is True


def test_disable_after_pr_is_refused_and_atomic(env, monkeypatch):
    github(env, monkeypatch)
    _ready(env)
    start_inline(env)
    env.office('approve', 'plan', '--quote', 'go', check=0)
    env.office('dispatch', 'T1', check=0)
    assert gh(env)['prs']
    before = _prs(env)
    code, out = env.office('amend', 'options', '--no-prs', '--quote', 'Stop PRs', '--confirm')
    assert code == 4 and 'prs-in-use' in out, out
    assert _prs(env) == before and not _events(env)


def test_disable_during_live_executor_is_refused(env, monkeypatch):
    github(env, monkeypatch)
    env.trust()
    start_inline(env)
    env.office('approve', 'plan', '--quote', 'go', check=0)
    env.office('dispatch', 'T1', env={'OFFICE_WORKER_LAUNCHER': 'external'}, check=0)
    code, out = env.office('amend', 'options', '--no-prs', '--quote', 'Stop PRs', '--confirm')
    assert code == 4 and 'prs-in-use' in out, out
    assert _prs(env)['enabled'] is True


def test_unavailable_and_immutable_options_fail_closed(env):
    env.trust()
    start_inline(env, extra=('--no-prs',))
    before = _prs(env)
    code, out = _enable(env)
    assert code == 4 and 'prs-unavailable' in out, out
    code, out = env.office('amend', 'options', '--gear', 'XL', '--confirm', '--quote', 'change gear')
    assert code == 4 and 'immutable-run-option' in out, out
    assert _prs(env) == before and not _events(env)


def test_wrong_amend_scope_rejects_option_flags(env):
    env.trust()
    start_inline(env, extra=('--no-prs',))
    code, out = env.office('amend', 'plan', '--prs', '--quote', 'yes', '--confirm', '--', 'note')
    assert code == 2 and 'option-scope-required' in out, out
    assert not _events(env)
