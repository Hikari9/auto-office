"""The Allocation surface in a real browser: the machine-wide scheduler, its panels and its controls."""
from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest

from office.web import server

LEGACY_RUN = "4d146f9a5ff05d85-synthetic0003"  # a 3.0 run in the fixture: read-only controls
RUN = "cd195abde9b4965c-synthetic0000"


@pytest.fixture
def served(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp_path / "user.yaml"))
    svc = server.build_fixture("small", home=tmp_path / "fx").start()
    httpd = server.make_server(svc, "127.0.0.1", 0)
    svc.run_poller(interval=0.2)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/", svc
    svc.close()
    httpd.shutdown()
    httpd.server_close()


def write(svc, sql: str, args=()):
    con = sqlite3.connect(svc.db_path, timeout=30)
    try:
        con.execute(sql, args)
        con.commit()
    finally:
        con.close()


def add_item(svc, item_id, kind, *, run_id=None, task_id=None, ref=None, title=None, priority="normal", paused=0,
             demoted_seq=None, enqueued_at="2026-09-01T00:00:00+00:00"):
    write(svc, "INSERT INTO sched_items(id, kind, run_id, task_id, ref, title, priority, paused, demoted_seq, "
               "enqueued_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
          (item_id, kind, run_id, task_id, ref, title, priority, paused, demoted_seq, enqueued_at, enqueued_at))


def open_allocation(page, url, width=1440, height=900):
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_viewport_size({"width": width, "height": height})
    page.goto(url)
    page.wait_for_selector("[data-testid=issue-row]", timeout=20000)
    page.click(".product-rail >> text=Allocation")
    page.wait_for_selector("[data-testid=alloc-row]", timeout=10000)
    return errors


def alloc_row(page, item_id):
    return page.locator(f'[data-testid=alloc-row][data-id="{item_id}"]')


def settled(page, label_part):
    """Wait until the last command (its label containing `label_part`) has a final result."""
    loc = page.locator("[data-testid=alloc-last-command]")
    assert wait_for(lambda: loc.count() and label_part in loc.text_content()
                    and loc.get_attribute("data-status") in ("completed", "failed")), label_part


def wait_for(fn, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.1)
    return False


def test_scheduler_rows_panels_and_no_fabricated_values(page, served):
    url, svc = served
    errors = open_allocation(page, url)
    assert page.locator("[data-testid=surface-allocation]").is_visible()
    assert not page.locator("[data-testid=surface-other]").is_visible()
    # Live runs with no queue item are projected: active, protected and marked as terminal-started.
    r = alloc_row(page, f"run:{RUN}")
    assert r.get_attribute("data-group") == "active"
    assert r.locator("[data-testid=alloc-terminal]").is_visible()
    assert r.locator("[data-testid=alloc-role]").text_content() == "Orchestrator"
    assert r.locator("[data-testid=alloc-repo]").text_content() == "synth-org-0/repo-00"
    assert r.locator("[data-testid=alloc-run]").text_content() == RUN[:8]
    comps = r.locator("[data-testid=alloc-score] .comp")
    assert {c.get_attribute("data-comp") for c in comps.all()} == {"priority", "aging", "critical_path", "protected"}
    assert r.locator("[data-testid=alloc-score] b").text_content() == "1020"
    assert r.locator("[data-testid=alloc-auto]").get_attribute("data-mode") == "on"
    # Global auto mode is the server's.
    assert page.get_attribute("[data-testid=alloc-auto-global]", "data-mode") == "on"
    # Host CPU and RAM are unavailable in the fixture and say so; quota is a separate panel.
    assert page.get_attribute("[data-testid=alloc-cpu]", "data-status") == "unavailable"
    assert "CPU unavailable" in page.text_content("[data-testid=alloc-cpu]")
    assert "RAM unavailable" in page.text_content("[data-testid=alloc-ram]")
    host = page.locator("[data-testid=alloc-host]")
    quota = page.locator("[data-testid=alloc-quota]")
    assert host.locator("[data-testid=alloc-quota-provider]").count() == 0
    statuses = {q.locator("b").text_content(): q.get_attribute("data-status")
                for q in quota.locator("[data-testid=alloc-quota-provider]").all()}
    assert statuses and all(s in ("unknown", "measured") for s in statuses.values())
    assert any(s == "unknown" for s in statuses.values())
    unknown = next(p for p, s in statuses.items() if s == "unknown")
    assert "quota unknown" in quota.locator(f"[data-testid=alloc-quota-provider]:has(b:text-is('{unknown}'))").text_content()
    # No recommended agent count without calibration evidence.
    rec = page.text_content("[data-testid=alloc-recommended]")
    assert "No recommended agent count" in rec
    assert not any(ch.isdigit() for ch in rec)
    assert errors == []


def test_queue_order_groups_and_critical_path_follow_the_server(page, served):
    url, svc = served
    con = sqlite3.connect(svc.db_path, timeout=30)
    deps = con.execute("SELECT id FROM tasks WHERE run_id=? ORDER BY id", (RUN,)).fetchall()
    con.close()
    first, second = deps[0][0], deps[1][0]
    write(svc, "UPDATE tasks SET depends_json=? WHERE run_id=? AND id=?", (json.dumps([first]), RUN, second))
    add_item(svc, "task:crit", "task", run_id=RUN, task_id=first, title="Critical task", priority="normal")
    add_item(svc, "issue:high", "issue", ref="synth-org-0/repo-00#7", title="High issue", priority="high")
    add_item(svc, "issue:low", "issue", ref="synth-org-0/repo-00#8", title="Low issue", priority="low")
    add_item(svc, "issue:demoted", "issue", ref="synth-org-0/repo-00#9", title="Demoted issue", priority="urgent",
             demoted_seq=1)
    add_item(svc, "issue:paused", "issue", ref="synth-org-0/repo-00#10", title="Paused issue", paused=1)
    errors = open_allocation(page, url)
    page.wait_for_selector('[data-testid=alloc-row][data-id="issue:paused"]')
    ready = [r.get_attribute("data-id") for r in page.locator("[data-testid=alloc-ready-group] [data-testid=alloc-row]").all()]
    # high (50) > critical-path normal (20 + 5) > low (0): the server's score order.
    assert ready == ["issue:high", "task:crit", "issue:low"]
    held = [r.get_attribute("data-id") for r in page.locator("[data-testid=alloc-held-group] [data-testid=alloc-row]").all()]
    assert held == ["issue:demoted", "issue:paused"]  # demoted before paused, urgent priority notwithstanding
    crit = alloc_row(page, "task:crit")
    assert crit.locator("[data-testid=alloc-critical]").text_content() == "critical path · unblocks 1"
    assert crit.locator("[data-testid=alloc-role]").text_content() == "Task"
    comps = {c.get_attribute("data-comp"): c.text_content() for c in crit.locator(".comp").all()}
    assert comps["priority"] == "priority 20" and comps["critical_path"] == "critical path 5"
    assert comps["protected"] == "protected 0"
    total = sum(float(v.rsplit(" ", 1)[1]) for v in comps.values())
    assert abs(float(crit.locator("[data-testid=alloc-score] b").text_content()) - total) < 0.01
    assert "demoted" in alloc_row(page, "issue:demoted").locator(".marks").text_content()
    paused = alloc_row(page, "issue:paused")
    assert "paused" in paused.locator(".marks").text_content()
    paused.locator("[data-testid=alloc-resume]").click()
    assert wait_for(lambda: any(c["args"] == ["queue", "resume", "issue:paused"] for c in svc.executor.calls))
    assert alloc_row(page, "issue:high").locator("[data-testid=alloc-role]").text_content() == "New run"
    assert errors == []


def test_controls_send_commands_and_show_only_server_state(page, served):
    url, svc = served
    add_item(svc, "issue:q", "issue", ref="synth-org-0/repo-00#7", title="Queued issue")
    errors = open_allocation(page, url)
    page.wait_for_selector('[data-testid=alloc-row][data-id="issue:q"]')
    calls = svc.executor.calls
    r = alloc_row(page, f"run:{RUN}")
    r.locator("[data-testid=alloc-pause]").click()
    assert wait_for(lambda: any(c["args"] == ["queue", "pause", "--run", RUN] for c in calls)), calls
    page.wait_for_selector("[data-testid=alloc-last-command][data-status=completed]")
    # The fake executor changes nothing: the row stays as the server reports it.
    assert r.get_attribute("data-group") == "active"
    assert r.locator("[data-testid=alloc-pause]").is_visible()

    q = alloc_row(page, "issue:q")
    q.locator("[data-testid=alloc-priority]").select_option("urgent")
    assert wait_for(lambda: any(c["args"] == ["queue", "priority", "issue:q", "urgent"] for c in calls)), calls
    settled(page, "Priority urgent")
    assert q.locator("[data-testid=alloc-priority]").input_value() == "normal"
    q.locator("[data-testid=alloc-demote]").click()
    assert wait_for(lambda: any(c["args"] == ["queue", "demote", "issue:q"] for c in calls)), calls

    r.locator("[data-testid=alloc-set_auto_mode]").click()
    assert wait_for(lambda: any(c["args"] == ["queue", "auto", "off", "--run", RUN] for c in calls)), calls
    page.click("[data-testid=alloc-auto-toggle]")
    assert wait_for(lambda: any(c["args"] == ["queue", "auto", "off"] for c in calls)), calls
    settled(page, "Auto mode off (machine)")
    assert page.get_attribute("[data-testid=alloc-auto-global]", "data-mode") == "on"

    # The server's state changes: the view follows, and offers an explicit resume.
    write(svc, "INSERT OR REPLACE INTO sched_state(scope, auto_mode, reason, updated_at) VALUES('global','off','t','x')")
    write(svc, "UPDATE sched_items SET priority='high' WHERE id='issue:q'")
    page.wait_for_selector("[data-testid=alloc-auto-global][data-mode=off]", timeout=8000)
    assert page.text_content("[data-testid=alloc-auto-toggle]") == "Resume auto mode"
    assert wait_for(lambda: q.locator("[data-testid=alloc-priority]").input_value() == "high")
    page.click("[data-testid=alloc-auto-toggle]")
    assert wait_for(lambda: any(c["args"] == ["queue", "auto", "on"] for c in calls)), calls
    # A run whose own auto mode is off offers an explicit per-run resume.
    write(svc, "DELETE FROM sched_state WHERE scope='global'")
    write(svc, "INSERT OR REPLACE INTO sched_state(scope, auto_mode, reason, updated_at) VALUES(?, 'off', 't', 'x')",
          (f"run:{RUN}",))
    r.locator("[data-testid=alloc-auto][data-mode=off]").wait_for(timeout=8000)
    resume = r.locator("[data-testid=alloc-set_auto_mode]")
    assert resume.text_content() == "Resume auto"
    resume.click()
    assert wait_for(lambda: any(c["args"] == ["queue", "auto", "on", "--run", RUN] for c in calls)), calls
    assert errors == []


def test_older_runtime_controls_are_disabled_with_the_reason(page, served):
    url, _ = served
    open_allocation(page, url)
    r = alloc_row(page, f"run:{LEGACY_RUN}")
    for testid in ("alloc-pause", "alloc-demote", "alloc-set_auto_mode", "alloc-priority"):
        el = r.locator(f"[data-testid={testid}]")
        assert el.is_disabled(), testid
        assert "3.3" in el.get_attribute("title"), testid
    assert "3.3" in r.locator("[data-testid=alloc-readonly]").text_content()
    assert alloc_row(page, f"run:{RUN}").locator("[data-testid=alloc-pause]").is_enabled()


def test_keyboard_operable_and_usable_at_1100(page, served):
    url, svc = served
    errors = open_allocation(page, url, 1100, 800)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.focus("[data-testid=alloc-auto-toggle]")
    page.keyboard.press("Tab")
    focused = page.evaluate("document.activeElement.dataset.testid")
    assert focused == "alloc-priority"
    assert page.evaluate("getComputedStyle(document.activeElement).outlineStyle") != "none"
    page.keyboard.press("Tab")
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.dataset.testid") == "alloc-demote"
    page.keyboard.press("Enter")
    assert wait_for(lambda: any(c["args"][:2] == ["queue", "demote"] for c in svc.executor.calls))
    # The last control used keeps focus through re-renders.
    settled(page, "Demote")
    assert page.evaluate("document.activeElement.dataset.testid") == "alloc-demote"
    assert errors == []


def test_unknown_states_are_shown_not_guessed(page, served):
    url, svc = served
    svc.host_probe = lambda: {"cpu": {"status": "ok", "value": 37.4, "unit": "percent"},
                              "ram": {"status": "ok", "value": 0.42, "unit": "fraction_used"}}  # hostmetrics vocabulary
    add_item(svc, "issue:odd", "issue", ref="synth-org-0/repo-00#7", title="Odd priority", priority="weird")
    write(svc, "INSERT OR REPLACE INTO sched_state(scope, auto_mode, reason, updated_at) VALUES(?, 'bogus', 't', 'x')",
          (f"run:{RUN}",))
    errors = open_allocation(page, url)
    page.wait_for_selector('[data-testid=alloc-row][data-id="issue:odd"]')
    sel = alloc_row(page, "issue:odd").locator("[data-testid=alloc-priority]")
    assert sel.input_value() == ""
    assert sel.locator("option:checked").text_content() == "weird"
    assert sel.locator("option:checked").is_disabled()
    r = alloc_row(page, f"run:{RUN}")
    r.locator("[data-testid=alloc-auto][data-mode=bogus]").wait_for(timeout=8000)
    assert r.locator("[data-testid=alloc-set_auto_mode]").count() == 0
    assert page.get_attribute("[data-testid=alloc-cpu]", "data-status") == "measured"
    assert page.locator("[data-testid=alloc-cpu] .v").text_content() == "37%"
    assert page.locator("[data-testid=alloc-ram] .v").text_content() == "42%"
    assert errors == []


def test_controls_column_is_not_clipped_at_1440(page, served):
    url, svc = served
    errors = open_allocation(page, url, 1440, 900)
    clipped = page.evaluate("""() => [...document.querySelectorAll('[data-testid=alloc-row]')].flatMap(row => {
        const table = row.closest('.atable').getBoundingClientRect();
        const cell = row.lastElementChild.getBoundingClientRect();
        const ctl = [...row.lastElementChild.querySelectorAll('button, select')].map(b => b.getBoundingClientRect());
        const overflow = cell.right > table.right + 0.5 || ctl.some(b => b.right > cell.right + 0.5 || b.right > table.right + 0.5);
        return overflow ? [row.dataset.id] : [];
    })""")
    assert clipped == []
    # Every table, controls column included, fits the surface: nothing is cut off at the viewport edge.
    assert page.evaluate("""() => { const s = document.getElementById('surface-allocation');
        const right = s.getBoundingClientRect().right;
        return s.scrollWidth <= s.clientWidth && [...document.querySelectorAll('.atable')].every(t =>
            t.scrollWidth <= t.clientWidth && t.getBoundingClientRect().right <= right + 0.5); }""")
    assert errors == []
