"""Run-first workbench browser contract: the real fixture Office server.

The historic Workstation browser suite intentionally opens fixture root (classic).
The workbench is opt-in (?workbench=1 or a stored preference) until visual sign-off;
classic is the default.
"""
from __future__ import annotations

import pytest


def open_workbench(page, web_url, width=1440, height=900):
    page.set_viewport_size({"width": width, "height": height})
    page.goto(web_url + "?workbench=1")
    page.wait_for_function("() => window.officeStore && window.officeStore.state")
    page.wait_for_selector('[data-testid="run-workbench"]')


def test_run_sidebar_is_backed_by_office_snapshot(page, web_url):
    errors = []
    page.on("pageerror", lambda err: errors.append(str(err)))
    open_workbench(page, web_url)
    assert page.locator('[data-testid="wb-run"]').count() >= 1
    value = page.locator('.ww-run.selected').get_attribute('data-run-id')
    assert value
    assert page.evaluate('(id) => Boolean(window.officeStore.state.entities.runs[id])', value)
    page.locator('[data-testid="wb-tab-tasks"]').click()
    assert page.locator('.ww-inspector-scroll').is_visible()
    page.locator('[data-testid="wb-tab-agents"]').click()
    page.locator('[data-testid="wb-tab-changes"]').click()
    page.locator('[data-testid="wb-tab-activity"]').click()
    assert errors == []


def test_issue_inbox_and_supporting_views_remain_reachable(page, web_url):
    open_workbench(page, web_url)
    for key, heading in [('issues','Issue Inbox'),('agents','Agents'),('allocation','Allocation'),('settings','Settings')]:
        page.locator(f'[data-testid="wb-nav-{key}"]').click()
        assert page.locator('#ww-work h1').first.inner_text() == heading
    page.keyboard.press('Control+k')
    assert page.locator('#ww-command').is_visible()
    page.locator('#ww-palette-input').fill('Issue Inbox')
    page.keyboard.press('Enter')
    assert page.locator('#ww-work h1').first.inner_text() == 'Issue Inbox'
    assert page.locator('#ww-command').is_hidden()


def test_read_only_run_cannot_show_a_live_composer(page, web_url):
    open_workbench(page, web_url)
    runs = page.evaluate("Object.values(window.officeStore.state.entities.runs)")
    without = next((r for r in runs if not r.get('owner') or r['owner'].get('kind') != 'session'), None)
    if without is None:
        pytest.skip('This fixture has no run without an orchestrator binding')
    key = without['id']
    page.locator('#ww-search').fill(without['run_id'])
    page.locator(f'[data-testid="wb-run"][data-run-id="{key}"]').click()
    assert page.locator('#ww-composer-input').is_disabled()
    assert page.locator('[data-testid="wb-send"]').is_disabled()


def test_mobile_sidebar_and_legacy_controls(page, web_url):
    open_workbench(page, web_url, width=390, height=844)
    assert 'ww-inspector-hidden' in (page.locator('#ww').get_attribute('class') or '')
    assert page.locator('#ww-overlay').is_hidden()
    page.locator('#ww-inspector-toggle').click()
    assert page.locator('#ww-inspector').is_visible()
    assert page.locator('#ww-overlay').is_visible()
    page.locator('#ww-overlay').click(position={'x':10,'y':400})
    assert 'ww-inspector-hidden' in (page.locator('#ww').get_attribute('class') or '')
    page.locator('#ww-mobile-menu').click()
    assert 'ww-sidebar-open' in (page.locator('#ww').get_attribute('class') or '')
    page.locator('#ww-overlay').click(position={'x':370,'y':400})
    assert 'ww-sidebar-open' not in (page.locator('#ww').get_attribute('class') or '')
    page.locator('#ww-mobile-menu').click()
    page.get_by_role('button', name='Classic controls').click()
    page.wait_for_function("() => new URLSearchParams(location.search).get('classic') === '1'")
    page.wait_for_selector('[data-testid="issue-table"]')
    assert page.locator('#ww').count() == 0


def test_activity_is_recorded_only_from_office(page, web_url):
    open_workbench(page, web_url)
    run = page.locator('.ww-run.selected').get_attribute('data-run-id')
    run_id = page.evaluate('(id) => window.officeStore.state.entities.runs[id].run_id', run)
    payload = page.request.get(web_url + 'api/runs/' + run_id + '/activity?limit=80').json()
    page.wait_for_selector('#ww-feed')
    if payload.get('items'):
        page.wait_for_selector('[data-testid="wb-event"]')
        seqs = page.locator('[data-testid="wb-event"]').evaluate_all(
            '(els) => els.map(el => Number(el.dataset.seq))')
        assert seqs == [e['seq'] for e in reversed(payload['items'])]
    else:
        assert page.locator('[data-testid="wb-event"]').count() == 0
    assert 'Provider transcripts are not synthesized' in page.locator('#ww-feed').inner_text()


def runs_by_repo(page):
    return page.evaluate("""() => Object.values(window.officeStore.state.entities.runs).map(
        r => ({id: r.id, run_id: r.run_id, repo: (r.repo && r.repo.key) || null}))""")


def other_repo_run(page):
    runs = runs_by_repo(page)
    selected = page.locator('.ww-run.selected').get_attribute('data-run-id')
    current = next(r['repo'] for r in runs if r['id'] == selected)
    return next((r for r in runs if r['repo'] != current), None)


def pick_run(page, run):
    # Search narrows to the run; clearing the search must leave its repository expanded.
    page.locator('#ww-search').fill(run['run_id'])
    page.locator(f'[data-testid="wb-run"][data-run-id="{run["id"]}"]').click()
    page.locator('#ww-search').fill('')


def test_classic_is_default_and_workbench_is_opt_in(page, web_url):
    page.goto(web_url)
    page.wait_for_selector('[data-testid="issue-table"]')
    assert page.locator('#ww').count() == 0
    # stored preference opts in; ?classic=1 still wins; ?workbench=0 clears it
    page.evaluate("localStorage.setItem('office-workbench-mode', 'workbench')")
    page.goto(web_url)
    page.wait_for_selector('[data-testid="run-workbench"]')
    page.goto(web_url + '?classic=1')
    page.wait_for_selector('[data-testid="issue-table"]')
    assert page.locator('#ww').count() == 0
    page.goto(web_url + '?workbench=0')
    page.wait_for_selector('[data-testid="issue-table"]')
    assert page.evaluate("localStorage.getItem('office-workbench-mode')") is None


def test_settings_toggle_stores_the_default_interface(page, web_url):
    open_workbench(page, web_url)
    page.locator('[data-testid="wb-nav-settings"]').click()
    assert 'opt-in' in page.locator('#ww-work').inner_text()
    page.locator('[data-testid="wb-mode-toggle"]').click()
    assert page.evaluate("localStorage.getItem('office-workbench-mode')") == 'workbench'
    page.locator('[data-testid="wb-mode-toggle"]').click()
    assert page.evaluate("localStorage.getItem('office-workbench-mode')") is None


def test_selected_run_repo_is_expanded_and_visible(page, web_url):
    open_workbench(page, web_url)
    other = other_repo_run(page)
    if other is None:
        pytest.skip('This fixture has a single repository')
    pick_run(page, other)
    page.wait_for_selector(f'.ww-run.selected[data-run-id="{other["id"]}"]')


def test_deep_link_history_and_not_found(page, web_url):
    open_workbench(page, web_url)
    other = other_repo_run(page)
    first = page.locator('.ww-run.selected').get_attribute('data-run-id')
    if other is None:
        pytest.skip('This fixture has a single repository')
    pick_run(page, other)
    page.wait_for_selector(f'.ww-run.selected[data-run-id="{other["id"]}"]')
    frag = page.evaluate("new URLSearchParams(location.hash.slice(1)).toString()")
    assert 'repo=' in frag and 'run=' in frag
    page.go_back()
    page.wait_for_selector(f'.ww-run.selected[data-run-id="{first}"]')
    page.go_forward()
    page.wait_for_selector(f'.ww-run.selected[data-run-id="{other["id"]}"]')
    # reload restores from the link; localStorage keeps the last run without a hash
    page.reload()
    page.wait_for_selector(f'.ww-run.selected[data-run-id="{other["id"]}"]')
    page.goto(web_url + '?workbench=1')
    page.wait_for_selector(f'.ww-run.selected[data-run-id="{other["id"]}"]')
    # an unknown run shows a notice and does not fall back to another run
    page.goto(web_url + '?workbench=1#repo=nope&run=run:does-not-exist')
    page.wait_for_selector('[data-testid="wb-run-not-found"]')
    assert page.locator('.ww-run.selected').count() == 0


def test_filter_and_copy_link(page, web_url):
    open_workbench(page, web_url)
    page.locator('#ww-search').fill('zzz-filter')
    page.reload()
    page.wait_for_selector('[data-testid="run-workbench"]')
    assert page.locator('#ww-search').input_value() == 'zzz-filter'
    page.locator('#ww-search').fill('')
    assert page.locator('[data-testid="wb-copy-link"]').count() == 1


def test_issue_with_several_runs_requires_an_explicit_choice(page, web_url):
    open_workbench(page, web_url)
    # clone a run on the same issue so one issue has two runs
    page.evaluate("""() => {
        const s = window.officeStore.state.entities;
        const run = Object.values(s.runs).find(r => r.issue && r.issue.ref && s.issues[r.issue.ref]);
        const twin = {...run, id: run.id + ':twin', run_id: run.run_id + '-twin', liveness: 'resumable'};
        s.runs[twin.id] = twin;
        const issue = s.issues[run.issue.ref];
        if (!(issue.runs || []).includes(run.id)) issue.runs = [...(issue.runs || []), run.id];
        issue.runs = [...issue.runs, twin.id];
    }""")
    page.locator('[data-testid="wb-nav-issues"]').click()
    choice = page.locator('[data-testid="wb-run-choice"]').first
    choice.wait_for()
    assert choice.locator('[data-testid="wb-open-run"]').count() >= 2
