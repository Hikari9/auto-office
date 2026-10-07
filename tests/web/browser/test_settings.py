"""The Settings surface in a real browser: the Machine -> Repository -> Run cascade and its edits."""
from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest

from office.web import server

RUN = "cd195abde9b4965c-synthetic0000"
REPO = "synth-org-0/repo-00"
INTAKE = ["intake.queue_issues", "intake.authorization", "intake.ready_to_land"]


@pytest.fixture
def served(tmp_path, monkeypatch):
    user = tmp_path / "user.yaml"
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(user))
    svc = server.build_fixture("small", home=tmp_path / "fx").start()
    httpd = server.make_server(svc, "127.0.0.1", 0)
    svc.run_poller(interval=0.2)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/", svc, user
    svc.close()
    httpd.shutdown()
    httpd.server_close()


def open_settings(page, url, width=1440, height=900):
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_viewport_size({"width": width, "height": height})
    page.goto(url)
    page.wait_for_selector("[data-testid=issue-row]", timeout=20000)
    page.click(".product-rail >> text=Settings")
    page.wait_for_selector("[data-testid=setting-row]", timeout=10000)
    return errors


def setting(page, key):
    return page.locator(f'[data-testid=setting-row][data-key="{key}"]')


def wait_for(fn, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.1)
    return False


def test_machine_scope_rows_badges_and_intake_defaults(page, served):
    url, _, _ = served
    errors = open_settings(page, url)
    assert page.get_attribute("[data-testid=settings-scope]", "data-scope") == "machine"
    intake = page.locator("[data-testid=settings-intake] [data-testid=setting-row]")
    assert [r.get_attribute("data-key") for r in intake.all()] == INTAKE
    q = setting(page, "intake.queue_issues")
    assert q.locator("[data-testid=setting-value]").text_content() == "false"
    assert q.locator("[data-testid=setting-source]").text_content() == "Default"
    assert q.locator("[data-testid=setting-marker]").text_content() == "inherited"
    assert q.locator("[data-testid=setting-apply]").get_attribute("data-apply") == "immediate"
    # Every row has a value, a source, a marker and an apply badge.
    for r in page.locator("[data-testid=setting-row]").all()[:40]:
        for part in ("setting-value", "setting-source", "setting-marker", "setting-apply"):
            assert r.locator(f"[data-testid={part}]").count() == 1
    applies = {b.get_attribute("data-apply") for b in page.locator("[data-testid=setting-apply]").all()}
    assert {"immediate", "before-dispatch", "future-runs", "restart"} <= applies
    # Inspection shows every tier; only the machine tier is editable at machine scope.
    q.locator("[data-testid=setting-inspect]").click()
    q.locator("[data-testid=setting-tiers]").wait_for()
    tiers = q.locator("[data-testid=setting-tier]")
    assert [t.get_attribute("data-tier") for t in tiers.all()] == ["default", "machine", "repository", "run-pinned"]
    assert [b.get_attribute("data-tier") for b in q.locator("[data-testid=setting-edit]").all()] == ["machine"]
    assert errors == []


def test_machine_edit_goes_through_settings_set_and_shows_the_reread_value(page, served):
    url, svc, user = served
    errors = open_settings(page, url)
    q = setting(page, "intake.queue_issues")
    q.locator("[data-testid=setting-inspect]").click()
    q.locator("[data-testid=setting-edit][data-tier=machine]").click()
    q.locator("[data-testid=setting-input]").select_option("true")
    # The value shown comes from the server's re-read, not from what was typed.
    user.write_text("intake:\n  queue_issues: true\n", encoding="utf-8")
    assert q.locator("[data-testid=setting-value]").text_content() == "false"
    q.locator("[data-testid=setting-save]").click()
    calls = svc.executor.calls
    assert wait_for(lambda: any(c["args"] == ["config", "--user", "--", "intake.queue_issues", "true"] for c in calls)), calls
    page.wait_for_selector('[data-testid=setting-row][data-key="intake.queue_issues"][data-source=machine]', timeout=8000)
    q = setting(page, "intake.queue_issues")
    assert q.locator("[data-testid=setting-value]").text_content() == "true"
    assert q.locator("[data-testid=setting-marker]").text_content() == "overridden"
    # Unset is offered for a tier that has a value, through settings_unset.
    q.locator("[data-testid=setting-edit][data-tier=machine]").click()
    q.locator("[data-testid=setting-unset]").click()
    assert wait_for(lambda: any(c["args"] == ["config", "--user", "--unset", "--", "intake.queue_issues"] for c in calls))
    assert errors == []


def test_repository_scope_offers_the_repository_tier(page, served):
    url, svc, _ = served
    errors = open_settings(page, url)
    page.select_option("[data-testid=scope-repo]", REPO)
    page.wait_for_selector("[data-testid=settings-scope][data-scope=repository]")
    a = setting(page, "intake.authorization")
    a.locator("[data-testid=setting-inspect]").click()
    a.locator("[data-testid=setting-tiers]").wait_for()
    assert [b.get_attribute("data-tier") for b in a.locator("[data-testid=setting-edit]").all()] == ["machine", "repository"]
    a.locator("[data-testid=setting-edit][data-tier=repository]").click()
    a.locator("[data-testid=setting-input]").fill("pr")
    a.locator("[data-testid=setting-input]").press("Enter")
    calls = svc.executor.calls
    assert wait_for(lambda: any(c["args"] == ["config", "--repo", "--", "intake.authorization", "pr"] for c in calls)), calls
    call = next(c for c in calls if c["args"][:2] == ["config", "--repo"])
    assert call["cwd"] and call["cwd"].endswith("synth-org-0__repo-00")
    assert errors == []


def test_run_pinned_values_are_read_only_with_the_reason(page, served):
    url, svc, _ = served
    con = sqlite3.connect(svc.db_path)
    con.execute("UPDATE runs SET policy_json=? WHERE id=?", (json.dumps({"quota": {"reserve_percent": 12}}), RUN))
    con.commit()
    con.close()
    errors = open_settings(page, url)
    page.select_option("[data-testid=scope-run]", RUN)
    page.wait_for_selector("[data-testid=settings-scope][data-scope=run]")
    page.fill("[data-testid=settings-filter]", "reserve_percent")
    r = setting(page, "quota.reserve_percent")
    assert r.get_attribute("data-source") == "run-pinned"
    assert r.locator("[data-testid=setting-value]").text_content() == "12"
    assert r.locator("[data-testid=setting-marker]").text_content() == "overridden"
    assert "never edited" in r.locator("[data-testid=setting-pinned]").text_content()
    r.locator("[data-testid=setting-inspect]").click()
    r.locator("[data-testid=setting-tiers]").wait_for()
    pinned = r.locator("[data-testid=setting-tier][data-tier=run-pinned]")
    assert pinned.locator("dd").text_content() == "12"
    assert pinned.locator("[data-testid=setting-edit]").count() == 0
    assert "never edited" in pinned.locator("[data-testid=setting-readonly]").text_content()
    assert r.locator("[data-testid=setting-tier][data-tier=default] dd").text_content() == "5"
    assert errors == []


def test_keyboard_operable_and_usable_at_1100(page, served):
    url, svc, _ = served
    errors = open_settings(page, url, 1100, 800)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.focus('[data-testid=setting-row][data-key="intake.authorization"] [data-testid=setting-inspect]')
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.closest('[data-testid=setting-row]').dataset.key") == "intake.ready_to_land"
    assert page.evaluate("getComputedStyle(document.activeElement).outlineStyle") != "none"
    page.keyboard.press("Enter")
    r = setting(page, "intake.ready_to_land")
    r.locator("[data-testid=setting-tiers]").wait_for()
    assert page.evaluate("document.activeElement.dataset.testid") == "setting-inspect"  # focus survives the re-render
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.dataset.testid") == "setting-edit"
    page.keyboard.press("Enter")
    assert wait_for(lambda: page.evaluate("document.activeElement.dataset.testid") == "setting-input")
    page.keyboard.press("Escape")
    assert wait_for(lambda: r.locator("[data-testid=setting-editor]").count() == 0)
    assert wait_for(lambda: page.evaluate("document.activeElement.dataset.testid") == "setting-edit")
    assert svc.executor.calls == []
    assert errors == []
