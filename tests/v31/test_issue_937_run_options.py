"""#937: confirmed live run option amendments persist, reject unsafe changes and affect PR jobs."""
import io
import json
import sys
from pathlib import Path

import pytest

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


def _pr_jobs(env):
    con = env.con()
    try:
        return con.execute("SELECT count(*) FROM outbox WHERE kind IN ('pr', 'pr_sync')").fetchone()[0]
    finally:
        con.close()


def _enable(env):
    return env.office('amend', 'options', '--prs', '--quote', 'User approved enabling PRs', '--confirm')


def _ready(env):
    env.trust()
    env.script(executor=[{'write': {'calc.py': GOOD_ADD}, 'submit': True}],
               convergence_reviewer=[{'reply': 'VERDICT: APPROVED\nNEXT proceed'}])


def test_worker_and_short_quotes_cannot_amend(env, monkeypatch):
    github(env, monkeypatch)
    _ready(env)
    start_inline(env, extra=('--no-prs',))
    for quote in ('', '   ', 'x', '  x \t'):
        code, out = env.office('amend', 'options', '--prs', '--quote', quote, '--confirm')
        assert code == 4 and 'user-authorization-required' in out
    for worker_env in ({'OFFICE_DISPATCH_ID': 'worker'}, {'OFFICE_ROLE': 'reviewer'}):
        code, out = env.office('amend', 'options', '--prs', '--quote', 'yes', '--confirm', env=worker_env)
        assert code == 4 and 'worker-not-authorized' in out, out
    assert not _prs(env)['enabled'] and not _events(env)


def test_status_persistently_shows_option_amendment(env, monkeypatch):
    github(env, monkeypatch)
    _ready(env)
    start_inline(env, extra=('--no-prs',))
    assert _enable(env)[0] == 0
    for _ in range(2):
        code, out = env.office('status')
        assert code == 0 and 'run option amendment: task PRs on' in out, out


def test_stale_detection_cannot_overwrite_confirmed_disable(env, monkeypatch):
    github(env, monkeypatch, repo_view_failures=1)
    env.trust()
    start_inline(env)
    from office import db, prs, state
    con = env.con()
    try:
        run_id = con.execute('SELECT id FROM runs').fetchone()[0]
        run = state.get_run(con, run_id)
        assert run['landing']['prs']['transient']
        disabled = {'enabled': False, 'amended_at': 'now', 'amended_by': 'user'}

        def detect(con, run):
            with db.transaction(con):
                landing = dict(state.get_run(con, run_id)['landing'])
                landing['prs'] = disabled
                state.update_run(con, run_id, landing=landing)
            return {'enabled': True}

        monkeypatch.setattr(prs, '_detect', detect)
        assert prs.settings(con, run) == disabled
        assert state.get_run(con, run_id)['landing']['prs'] == disabled
    finally:
        con.close()


def test_submit_uses_current_pr_setting_with_stale_run(env, monkeypatch):
    github(env, monkeypatch)
    _ready(env)
    start_inline(env, extra=('--no-prs',))
    env.office('approve', 'plan', '--quote', 'go', check=0)
    env.office('dispatch', 'T1', env={'OFFICE_WORKER_LAUNCHER': 'external'}, check=0)
    from office import state, submit, prs
    con = env.con()
    try:
        d = dict(con.execute("SELECT * FROM dispatches WHERE role='executor'").fetchone())
        stale = state.get_run(con, d['run_id'])
        assert _enable(env)[0] == 0
        calls = []
        monkeypatch.setattr(prs, 'advance_branch', lambda *args: calls.append(args))
        submit.submit_revision(con, stale, d, Path(d['worktree']))
        assert calls
        assert con.execute("SELECT count(*) FROM outbox WHERE kind='pr_sync'").fetchone()[0] >= 1
    finally:
        con.close()


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
    before_prs = {k: _prs(env)[k] for k in ('repo', 'base_branch', 'merge_method')}
    code, out = env.office('amend', 'options', '--no-prs', '--quote', 'Keep it local', '--confirm')
    assert code == 0 and 'disabled' in out, out
    kept = ('repo', 'base_branch', 'merge_method')
    assert {k: _prs(env)[k] for k in kept} == before_prs and before_prs['base_branch'] == 'main'
    code, out = env.office('status')
    assert code == 0 and 'run option amendment: task PRs off' in out, out
    env.office('approve', 'plan', '--quote', 'go', check=0)
    env.office('dispatch', 'T1', check=0)
    assert _pr_jobs(env) == 0
    assert _prs(env)['enabled'] is False
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
    assert code == 4 and 'prs-in-use' in out and 'already has a recorded PR' in out, out
    assert _prs(env) == before and not _events(env)


def test_disable_during_live_executor_is_refused(env, monkeypatch):
    github(env, monkeypatch)
    env.trust()
    start_inline(env)
    env.office('approve', 'plan', '--quote', 'go', check=0)
    env.office('dispatch', 'T1', env={'OFFICE_WORKER_LAUNCHER': 'external'}, check=0)
    code, out = env.office('amend', 'options', '--no-prs', '--quote', 'Stop PRs', '--confirm')
    assert code == 4 and 'prs-in-use' in out and 'still live' in out, out
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


def test_route_amend_rejects_option_flags(env):
    env.trust()
    start_inline(env, extra=('--no-prs',))
    for flag in ('--prs', '--no-prs', '--confirm'):
        code, out = env.office('amend', 'route', 'T1', '--as', 'claude/sonnet@high', '--quote', 'yes', flag)
        assert code == 2 and 'option-scope-required' in out, out
    assert not _events(env) and not _prs(env)['enabled']


def test_pending_pr_job_blocks_disabling_before_pr_recorded(env, monkeypatch):
    """An outbox job may already have made a remote PR Office has not observed."""
    github(env, monkeypatch)
    env.trust()
    start_inline(env)
    from office import db, state
    con = env.con()
    try:
        with db.transaction(con):
            run = state.get_run(con, con.execute('SELECT id FROM runs').fetchone()[0])
            state.enqueue(con, run, 'pr_sync', {'task_id': 'T1', 'event': 'revision', 'ref': 'unknown'},
                          dedup_key='test-pending-pr-job')
    finally:
        con.close()
    before = _prs(env)
    code, out = env.office('amend', 'options', '--no-prs', '--quote', 'Stop PRs', '--confirm')
    assert code == 4 and 'prs-in-use' in out and 'PR job' in out, out
    assert _prs(env) == before and not _events(env)


def test_pr_sync_queue_uses_current_mode_not_stale_run_snapshot(env, monkeypatch):
    """A worker may have read run state before the user enabled PRs (#937)."""
    github(env, monkeypatch)
    env.trust()
    start_inline(env, extra=('--no-prs',))
    from office import db, prs, state
    con = env.con()
    try:
        run_id = con.execute('SELECT id FROM runs').fetchone()[0]
        stale_off = state.get_run(con, run_id)
        assert not prs.enabled(stale_off)
        assert _enable(env)[0] == 0
        with db.transaction(con):
            prs.queue(con, stale_off, 'T1', 'verdict', 'G-stale')
        rows = con.execute("SELECT payload_json FROM outbox WHERE kind='pr_sync'").fetchall()
        assert len(rows) == 1 and json.loads(rows[0][0])['ref'] == 'G-stale'
    finally:
        con.close()


def test_confirmed_disable_pins_off_after_transient_github_failure(env, monkeypatch):
    """A temporary gh error cannot later override explicit --no-prs (#479)."""
    github(env, monkeypatch, repo_view_failures=2)
    env.trust()
    start_inline(env)
    assert _prs(env)['enabled'] is False and _prs(env)['transient'] is True
    code, out = env.office('amend', 'options', '--no-prs', '--quote', 'Keep PRs off', '--confirm')
    assert code == 0 and 'disabled' in out, out
    assert _prs(env)['enabled'] is False and 'transient' not in _prs(env) and _prs(env)['amended_at']
    from office import prs, state
    con = env.con()
    try:
        run = state.get_run(con, con.execute('SELECT id FROM runs').fetchone()[0])
        assert prs.settings(con, run)['enabled'] is False
    finally:
        con.close()
    assert len(_events(env)) == 1


class _Tty(io.StringIO):
    def isatty(self):
        return True


@pytest.mark.parametrize('flags', [
    ('--contract',), ('--requirements',), ('--no-review',), ('--reason', 'why'),
    ('--add-accept', 'calc.add(0, 0) == 0'), ('--drop-accept', 'calc.add(2, 3) == 5'),
    ('--add-check', 'true'), ('--drop-check', 'true'), ('--set', 'depends=none'),
    ('--add-criterion', 'x'), ('--drop-criterion', 'x'), ('--add-non-goal', 'x'), ('--drop-non-goal', 'x'),
    ('--redirect', 'P1'), ('--root-cause', 'x'), ('--requirement', 'x'), ('--reviewer', 'same'),
])
def test_options_amend_refuses_plan_flags_it_would_drop(env, flags):
    env.trust()
    start_inline(env, extra=('--no-prs',))
    code, out = env.office('amend', 'options', '--prs', *flags, '--quote', 'yes', '--confirm')
    assert code == 4 and 'immutable-run-option' in out, out
    assert _prs(env)['enabled'] is False and not _events(env)


def test_options_amend_mode_flag_is_required_and_exclusive(env):
    env.trust()
    start_inline(env, extra=('--no-prs',))
    code, out = env.office('amend', 'options', '--quote', 'yes', '--confirm')
    assert code == 2 and 'missing-run-option' in out, out
    code, out = env.office('amend', 'options', '--prs', '--no-prs', '--quote', 'yes', '--confirm')
    assert code == 2 and 'not allowed with' in out, out
    assert _prs(env)['enabled'] is False and not _events(env)


def test_interactive_option_change_needs_an_explicit_yes(env, monkeypatch):
    github(env, monkeypatch)
    _ready(env)
    start_inline(env)
    monkeypatch.setattr(sys, 'stdin', _Tty())
    for answer in ('n', ''):
        monkeypatch.setattr('builtins.input', lambda prompt='', answer=answer: answer)
        code, out = env.office('amend', 'options', '--no-prs', '--quote', 'Keep it local')
        assert code == 4 and 'confirmation-declined' in out, out
        assert _prs(env)['enabled'] is True and not _events(env)
    monkeypatch.setattr('builtins.input', lambda prompt='': 'y')
    code, out = env.office('amend', 'options', '--no-prs', '--quote', 'Keep it local')
    assert code == 0 and 'disabled' in out, out
    assert _prs(env)['enabled'] is False and len(_events(env)) == 1


def test_run_closing_mid_confirm_refuses_option_change(env, monkeypatch):
    # A closed run is not active, so the only reachable terminal check is the one inside the transaction.
    github(env, monkeypatch)
    _ready(env)
    start_inline(env)
    from office import db, prs, state
    real_settings = prs.settings

    def settle_then_close(con, run):
        seen = real_settings(con, run)
        with db.transaction(con):
            state.update_run(con, run['id'], phase='closed')
        return seen

    monkeypatch.setattr(prs, 'settings', settle_then_close)
    code, out = env.office('amend', 'options', '--no-prs', '--quote', 'Keep it local', '--confirm')
    assert code == 4 and 'run-terminal' in out, out
    assert _prs(env)['enabled'] is True and not _events(env)


def test_option_change_refused_when_pr_settings_move_mid_confirm(env, monkeypatch):
    github(env, monkeypatch)
    _ready(env)
    start_inline(env)
    from office import db, prs, state
    real_settings = prs.settings

    def settle_then_move(con, run):
        seen = real_settings(con, run)
        with db.transaction(con):
            landing = dict(state.get_run(con, run['id'])['landing'])
            landing['prs'] = {**seen, 'reason': 'moved mid-confirm'}
            state.update_run(con, run['id'], landing=landing)
        return seen

    monkeypatch.setattr(prs, 'settings', settle_then_move)
    code, out = env.office('amend', 'options', '--no-prs', '--quote', 'Keep it local', '--confirm')
    assert code == 4 and 'run-options-stale' in out, out
    assert _prs(env)['reason'] == 'moved mid-confirm' and not _events(env)


@pytest.mark.parametrize('status', ['done', 'failed'])
def test_pr_job_with_unrecorded_outcome_blocks_disable(env, monkeypatch, status):
    github(env, monkeypatch)
    _ready(env)
    start_inline(env)
    from office import db
    from office.util import now_iso
    con = env.con()
    try:
        run_id = con.execute('SELECT id FROM runs').fetchone()[0]
        with db.transaction(con):
            con.execute("INSERT INTO outbox(id, run_id, kind, dedup_key, payload_json, office_version, status, error, "
                        "created_at) VALUES(?, ?, 'pr_sync', ?, '{\"task_id\": \"T1\"}', "
                        "'test', ?, 'gh pr create failed', ?)", (status, run_id, status, status, now_iso()))
    finally:
        con.close()
    code, out = env.office('amend', 'options', '--no-prs', '--quote', 'Keep it local', '--confirm')
    assert code == 4 and 'prs-in-use' in out and 'may already have pushed' in out, out
    assert _prs(env)['enabled'] is True and not _events(env)
