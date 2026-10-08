"""Run-first workbench browser contract: the real fixture Office server.

The historic Workstation browser suite intentionally opens fixture root (classic).
The new view is exercised with ?workbench=1; production root defaults to it.
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
    page.wait_for_url('**/\u003fclassic=1*')
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