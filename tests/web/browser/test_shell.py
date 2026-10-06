"""The Workstation shell in a real browser: rails, freshness, banners, the live store, keyboard, layout."""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager

import pytest

from office.web import server


@contextmanager
def serving(svc, port: int = 0, poll: bool = True):
    httpd = server.make_server(svc, "127.0.0.1", port)
    if poll:
        svc.run_poller(interval=0.2)
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/", httpd
    finally:
        svc.close()
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture
def fx(tmp_path, monkeypatch):
    """A seeded small fixture service, not yet serving."""
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp_path / "user.yaml"))
    return server.build_fixture("small", home=tmp_path / "fx", seed_receipts=True).start()


def set_github(url: str, svc, repo: str, state: str) -> dict:
    req = urllib.request.Request(url + "api/fixture/github", method="POST",
                                 data=json.dumps({"repo": repo, "state": state}).encode(),
                                 headers={"Content-Type": "application/json", "X-Office-Token": svc.token})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def ready(page, url):
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url)
    page.wait_for_selector("[data-testid=issue-row]", timeout=10000)
    return errors


def test_shell_layout_rails_and_freshness_without_external_requests(page, fx):
    with serving(fx) as (url, _):
        requests = []
        page.on("request", lambda r: requests.append(r.url))
        page.set_viewport_size({"width": 1440, "height": 900})
        errors = ready(page, url)
        surfaces = page.locator(".product-rail button")
        assert surfaces.all_text_contents() == ["Issues", "Agents", "Allocation", "Settings"]
        assert surfaces.nth(0).get_attribute("aria-current") == "page"
        assert page.locator("[data-testid=repo-all]").get_attribute("aria-pressed") == "true"
        not_ready = page.locator('[data-testid="repo-synth-org-0/not-ready"]')
        assert not_ready.locator("[data-testid=mark-local]").text_content().startswith("No checkout")
        assert not_ready.locator("[data-testid=mark-github]").text_content().startswith("GitHub")
        archived = page.locator('[data-testid="repo-synth-org-1/archived-repo"] [data-testid=mark-github]')
        assert archived.text_content().startswith("Archived")
        assert page.locator('[data-testid="repo-synth-org-2/issues-disabled"] [data-testid=mark-github]') \
            .text_content().startswith("Issues off")
        # Office and GitHub freshness are two indicators, each with an age.
        assert page.text_content("[data-testid=office-freshness]") == "live"
        assert page.text_content("[data-testid=github-freshness]") == "fresh"
        assert "ago" in page.text_content("[data-testid=office-age]")
        assert "ago" in page.text_content("[data-testid=github-age]")
        assert page.locator("[data-testid=kpi-cpu]").text_content().endswith("CPU telemetry unavailable")
        page.click("text=Agents")
        assert page.locator("[data-testid=surface-other]").is_visible()
        assert not page.locator("[data-testid=surface-issues]").is_visible()
        page.click(".product-rail >> text=Issues")
        assert page.locator("[data-testid=surface-issues]").is_visible()
        assert errors == []
        assert requests and all(r.startswith(url) for r in requests), [r for r in requests if not r.startswith(url)]
        assert any(r.endswith("/static/store.js") for r in requests)


def test_delta_keeps_focus_typed_text_selection_and_scroll(page, fx):
    with serving(fx) as (url, _):
        page.set_viewport_size({"width": 1440, "height": 900})
        ready(page, url)
        rows = page.locator("[data-testid=issue-row]")
        target = rows.nth(4)
        target_id = target.get_attribute("data-id")
        target.click()
        assert target.get_attribute("aria-selected") == "true"
        page.eval_on_selector("[data-testid=issue-table]", "el => { el.scrollTop = 120; }")
        page.fill("[data-testid=issue-search]", "synth")
        page.eval_on_selector("[data-testid=issue-search]", "el => { el.focus(); el.setSelectionRange(1, 3); }")
        before = page.evaluate("({rev: window.officeStore.state.rev, ...window.officeStore.stats})")
        set_github(url, fx, "synth-org-1/repo-01", "rate_limited")
        page.wait_for_selector("[data-testid=banner-rate-limited]", timeout=10000)
        after = page.evaluate("({rev: window.officeStore.state.rev, ...window.officeStore.stats})")
        assert after["applied"] > before["applied"] and after["rev"] > before["rev"]
        assert after["resyncs"] == before["resyncs"] and after["snapshots"] == before["snapshots"]
        focus = page.evaluate("""() => { const a = document.activeElement;
            return {id: a.id, value: a.value, start: a.selectionStart, end: a.selectionEnd}; }""")
        assert focus == {"id": "issue-search", "value": "synth", "start": 1, "end": 3}
        assert page.eval_on_selector("[data-testid=issue-table]", "el => el.scrollTop") == 120
        selected = page.locator('[data-testid=issue-row][aria-selected="true"]')
        assert selected.count() == 1 and selected.get_attribute("data-id") == target_id
        assert page.locator("[data-testid=inspector]").is_visible()


def test_focused_inspector_control_survives_a_delta(page, fx):
    with serving(fx) as (url, _):
        page.set_viewport_size({"width": 1440, "height": 900})
        ready(page, url)
        page.fill("[data-testid=issue-search]", "repo-00 #3")
        page.locator("[data-testid=issue-row]").first.click()
        select = page.locator("[data-testid=auth-start]")
        select.select_option("merge")
        select.focus()
        rev = page.evaluate("window.officeStore.state.rev")
        set_github(url, fx, "synth-org-1/repo-01", "revoked")
        page.wait_for_selector("[data-testid=banner-revoked]", timeout=10000)
        assert page.evaluate("window.officeStore.state.rev") > rev
        assert page.evaluate("document.activeElement.dataset.testid") == "auth-start"
        assert page.input_value("[data-testid=auth-start]") == "merge"
        assert "--end-state merge" in page.text_content("[data-testid=start-command]")


def test_store_ignores_duplicates_and_resyncs_on_gap_and_epoch(page, fx):
    with serving(fx) as (url, _):
        ready(page, url)
        out = page.evaluate("""async () => {
            const s = window.officeStore, st = s.state, rev = st.rev, epoch = st.epoch;
            const empty = {upserts: {}, removes: {}, scalars: {}};
            const dup = s.delta({epoch, rev, base_rev: rev - 1, ...empty});
            const snaps = s.stats.snapshots;
            const gap = s.delta({epoch, rev: rev + 5, base_rev: rev + 4, ...empty});
            await new Promise((r) => { const t = setInterval(() => { if (s.stats.snapshots > snaps) { clearInterval(t); r(); } }, 20); });
            const snaps2 = s.stats.snapshots;
            const other = s.delta({epoch: "other", rev: s.state.rev + 1, base_rev: s.state.rev, ...empty});
            await new Promise((r) => { const t = setInterval(() => { if (s.stats.snapshots > snaps2) { clearInterval(t); r(); } }, 20); });
            const { applyDelta } = await import("/static/store.js");
            const local = {epoch: "e", rev: 3, entities: {runs: {a: 1, b: 2}}, scalars: {}, freshness: {}};
            const applied = applyDelta(local, {epoch: "e", rev: 4, base_rev: 3, upserts: {runs: {c: 3}},
                                               removes: {runs: ["a"]}, scalars: {freshness: {x: 1}, host: 2}});
            return {dup, gap, other, applied, local, epochAfter: s.state.epoch, stats: s.stats};
        }""")
        assert out["dup"] == "duplicate" and out["gap"] == "resync" and out["other"] == "resync"
        assert out["stats"]["duplicates"] == 1 and out["stats"]["resyncs"] == 2
        assert out["epochAfter"] == fx.epoch
        assert out["applied"] == "applied"
        assert out["local"] == {"epoch": "e", "rev": 4, "entities": {"runs": {"b": 2, "c": 3}},
                                "scalars": {"host": 2}, "freshness": {"x": 1}}


def test_reconnects_after_restart_and_resyncs_to_the_new_epoch(page, tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp_path / "user.yaml"))
    first = server.build_fixture("small", home=tmp_path / "a").start()
    with serving(first) as (url, httpd):
        port = httpd.server_address[1]
        ready(page, url)
        old_epoch = page.evaluate("window.officeStore.state.epoch")
    page.wait_for_selector("[data-testid=banner-disconnected]", timeout=10000)
    assert page.get_attribute("[data-testid=stream-indicator]", "data-state") in ("reconnecting", "disconnected")
    second = server.build_fixture("small", home=tmp_path / "b").start()
    with serving(second, port=port):
        page.wait_for_function("(e) => window.officeStore.state && window.officeStore.state.epoch === e", arg=second.epoch,
                               timeout=30000)
        page.wait_for_selector("[data-testid=banner-disconnected]", state="detached", timeout=10000)
        assert page.get_attribute("[data-testid=stream-indicator]", "data-state") == "live"
        assert old_epoch != second.epoch
        assert page.text_content("[data-testid=rev]") == f"{second.epoch}:{second.rev}"


def test_stale_office_and_github_banners_and_reset_time(page, fx):
    with serving(fx, poll=False) as (url, _):
        ready(page, url)
        set_github(url, fx, "synth-org-0/repo-00", "rate_limited")
        page.wait_for_selector("[data-testid=banner-rate-limited]", timeout=10000)
        text = page.text_content("[data-testid=banner-rate-limited]")
        assert "synth-org-0/repo-00" in text and "resets at" in text and "(in 15m)" in text
        assert page.get_attribute("[data-testid=github-indicator]", "data-state") == "rate_limited"
        fx.last_ok -= fx.stale_after + 5
        fx._rebuild(reuse_entities=True)
        page.wait_for_selector("[data-testid=banner-office-stale]", timeout=10000)
        assert page.text_content("[data-testid=office-freshness]") == "stale"
        set_github(url, fx, "synth-org-0/repo-00", "fresh")
        page.wait_for_selector("[data-testid=banner-rate-limited]", state="detached", timeout=10000)


def test_fixture_control_is_refused_outside_fixture_mode(fx):
    fx.fixture = None
    with serving(fx, poll=False) as (url, _):
        with pytest.raises(urllib.error.HTTPError) as err:
            set_github(url, fx, "synth-org-0/repo-00", "revoked")
        assert err.value.code == 403 and json.loads(err.value.read())["reason"] == "fixture-only"


def test_static_modules_are_served_only_by_name(fx):
    with serving(fx, poll=False) as (url, _):
        with urllib.request.urlopen(url + "static/store.js", timeout=5) as resp:
            assert resp.headers["Content-Type"].startswith("text/javascript")
        for bad in ("static/../api.py", "static/index.html", "static/%2e%2e/server.py", "static/missing.js"):
            with pytest.raises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(url + bad, timeout=5)
            assert err.value.code == 404


def test_keyboard_reaches_rails_rows_and_actions_with_visible_focus(page, fx):
    with serving(fx) as (url, _):
        page.set_viewport_size({"width": 1440, "height": 900})
        ready(page, url)
        page.keyboard.press("Tab")
        assert page.evaluate("document.activeElement.dataset.surface") == "issues"
        outline = page.evaluate("getComputedStyle(document.activeElement).outlineStyle")
        assert outline not in ("", "none")
        page.keyboard.press("Tab")
        page.keyboard.press("Tab")
        page.keyboard.press("Tab")
        page.keyboard.press("Tab")
        assert page.evaluate("document.activeElement.id") == "repo-search"
        page.keyboard.press("Tab")
        assert page.evaluate("document.activeElement.dataset.testid") == "repo-all"
        page.keyboard.press("Tab")
        page.keyboard.press("Enter")  # a repository button is operable by keyboard
        assert page.evaluate("document.activeElement.getAttribute('aria-pressed')") == "true"
        page.click("[data-testid=repo-all]")
        page.locator("[data-testid=issue-row]").first.focus()
        first = page.evaluate("document.activeElement.dataset.id")
        page.keyboard.press("ArrowDown")
        second = page.evaluate("document.activeElement.dataset.id")
        assert second and second != first
        page.keyboard.press("Enter")
        assert page.locator(f'[data-testid=issue-row][data-id="{second}"]').get_attribute("aria-selected") == "true"
        assert page.evaluate("document.getElementById('inspector').contains(document.activeElement)")
        page.keyboard.press("Escape")
        assert not page.locator("[data-testid=inspector]").is_visible()
        assert page.evaluate("document.activeElement.dataset.id") == second
        page.keyboard.press("Tab")  # the row's own action is next in the tab order
        assert page.evaluate("document.activeElement.dataset.key") == f"row-action:{second}"
        labels = page.eval_on_selector_all("button", "els => els.filter(b => !b.textContent.trim() && !b.getAttribute('aria-label')).length")
        assert labels == 0


def test_status_is_not_color_alone(page, fx):
    with serving(fx) as (url, _):
        ready(page, url)
        for testid in ("office-indicator", "github-indicator"):
            assert page.text_content(f"[data-testid={testid}] .state")
        receipts = page.locator("[data-testid=receipt]")
        texts = receipts.all_text_contents()
        assert any("result unknown" in t for t in texts) and any("failed" in t for t in texts)
        glyphs = page.eval_on_selector_all("[data-testid=receipt]", "els => els.map(e => getComputedStyle(e, '::before').content)")
        assert len(set(glyphs)) >= 4


def test_layout_at_1100_scrolls_columns_and_falls_back_below_900(page, fx):
    with serving(fx) as (url, _):
        page.set_viewport_size({"width": 1100, "height": 800})
        ready(page, url)
        table = page.locator("[data-testid=issue-table]")
        assert page.locator("[role=columnheader]").count() == 12
        dims = table.evaluate("el => ({sw: el.scrollWidth, cw: el.clientWidth})")
        assert dims["sw"] > dims["cw"]  # internal horizontal scroll, columns kept
        row = page.locator("[data-testid=issue-row]").first
        assert row.evaluate("el => getComputedStyle(el).display") == "grid"
        assert page.evaluate("document.documentElement.scrollWidth") <= 1100
        page.set_viewport_size({"width": 800, "height": 900})
        time.sleep(0.2)
        assert page.evaluate("document.documentElement.scrollWidth") <= 800
        assert page.locator(".product-rail").evaluate("el => getComputedStyle(el).flexDirection") == "row"
        assert page.locator("[data-testid=issue-row]").first.is_visible()


def test_reduced_motion_disables_transitions(page, fx):
    with serving(fx) as (url, _):
        page.emulate_media(reduced_motion="reduce")
        ready(page, url)
        assert page.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches")
        assert page.eval_on_selector(".surface", "el => getComputedStyle(el).transitionDuration") == "0s"
