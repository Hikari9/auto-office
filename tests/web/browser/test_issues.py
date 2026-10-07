"""The Issues surface in a real browser: the dense table, filters, inspector, actions and command receipts."""
from __future__ import annotations

import json
import threading
import time

import pytest

from office.web import server

COLUMNS = ["Repository", "Issue", "Owner", "Phase", "Progress", "Priority", "Authorization", "PR / run", "State",
           "Office gates", "GitHub checks", "Action"]


def _serve(svc):
    httpd = server.make_server(svc, "127.0.0.1", 0)
    svc.run_poller(interval=0.2)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return httpd


@pytest.fixture
def served(tmp_path, monkeypatch):
    """(url, service) for a seeded fixture; `scale` defaults to small."""
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp_path / "user.yaml"))
    started = []

    def start(scale: str = "small"):
        svc = server.build_fixture(scale, home=tmp_path / scale, seed_receipts=True).start()
        httpd = _serve(svc)
        started.append((svc, httpd))
        return f"http://127.0.0.1:{httpd.server_address[1]}/", svc
    yield start
    for svc, httpd in started:
        svc.close()
        httpd.shutdown()
        httpd.server_close()


def open_page(page, url, width=1920, height=1080):
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_viewport_size({"width": width, "height": height})
    page.goto(url)
    page.wait_for_selector("[data-testid=issue-row]", timeout=20000)
    return errors


def row(page, issue_id):
    return page.locator(f'[data-testid=issue-row][data-id="{issue_id}"]')


def find(page, url_part: str):
    """Filter to one issue by search text and select it."""
    page.fill("[data-testid=issue-search]", url_part)
    page.locator("[data-testid=issue-row]").first.click()


def cells(locator):
    return [c.strip() for c in locator.locator("[role=gridcell]").all_text_contents()]


def test_large_fixture_is_virtualized_and_scrolls_smoothly(page, served):
    url, svc = served("large")
    errors = open_page(page, url)
    count = page.text_content("[data-testid=issue-count]")
    shown, _, total = count.partition(" of ")
    assert int(shown) == int(total.split()[0]) >= 2000
    assert page.locator("[data-testid=issue-row]").count() < 80  # only the visible window is in the DOM
    timing = page.evaluate("""async () => {
        const el = document.getElementById('issue-table');
        const frames = [], gaps = [];
        for (let i = 1; i <= 60; i += 1) {
            const t0 = performance.now();
            el.scrollTop = i * (el.scrollHeight / 60);
            el.dispatchEvent(new Event('scroll'));  // the scroll handler renders synchronously
            frames.push(performance.now() - t0);
            const shown = [...document.querySelectorAll('[data-testid=issue-row]')].map(r => +r.getAttribute('aria-rowindex') - 2);
            const top = Math.floor(el.scrollTop / 44), bottom = Math.floor((el.scrollTop + el.clientHeight - 41) / 44);
            const total = +el.getAttribute('aria-rowcount') - 1;
            if (Math.min(...shown) > top || Math.max(...shown) < Math.min(bottom, total - 1)) gaps.push(i);
            await new Promise(requestAnimationFrame);
        }
        const rows = document.querySelectorAll('[data-testid=issue-row]');
        return {worst: Math.max(...frames), gaps, rows: rows.length,
                last: rows[rows.length - 1].getAttribute('aria-rowindex'), total: el.getAttribute('aria-rowcount')};
    }""")
    assert timing["worst"] < 50, timing  # each scroll re-render stays well inside a frame budget
    assert timing["rows"] < 80 and timing["last"] == timing["total"]
    assert timing["gaps"] == []  # every intermediate position has its visible rows rendered
    assert errors == []


def test_table_columns_and_running_and_incoming_rows(page, served):
    url, svc = served()
    open_page(page, url)
    assert page.locator("[role=columnheader]").all_text_contents() == COLUMNS
    live = cells(row(page, "issue:repo:github.com/synth-org-0/repo-00#1"))
    assert live[0] == "synth-org-0/repo-00" and live[1].startswith("#1 Issue 1")
    assert live[2].endswith("orchestrator") and live[3] == "executing"
    assert "/" in live[4]  # weighted progress fraction next to the bar
    assert live[5] == "normal" and live[7].startswith("run ")
    assert live[8].startswith("Live run") and live[9].startswith("code review")
    assert live[10] in ("—", "unavailable") or "success" in live[10]  # GitHub checks are their own column
    assert live[11] == "Attach"
    incoming = cells(row(page, "issue:repo:github.com/synth-org-0/not-ready#1"))
    assert incoming[2] == "No owner" and incoming[3] == "Incoming" and incoming[4] == "—"
    assert incoming[7] == "no run" and incoming[8] == "No run" and incoming[11] == "Copy office start"
    bars = row(page, "issue:repo:github.com/synth-org-0/repo-00#1").locator(".bar")
    assert bars.count() == 1


def test_search_repository_filter_and_empty_states(page, served):
    url, svc = served()
    open_page(page, url)
    total = int(page.text_content("[data-testid=issue-count]").split()[2])
    page.click('[data-testid="repo-synth-org-0/not-ready"]')
    assert page.locator("[data-testid=issue-row]").count() == 3
    assert page.text_content("[data-testid=issue-count]") == f"3 of {total} issues"
    page.fill("[data-testid=issue-search]", "#2")
    assert page.locator("[data-testid=issue-row]").count() == 1
    page.fill("[data-testid=issue-search]", "zzz-nothing")
    assert page.get_attribute("[data-testid=table-empty]", "data-kind") == "no-results"
    page.click("[data-testid=table-empty] >> text=Clear search")
    assert page.locator("[data-testid=issue-row]").count() == 3
    page.click('[data-testid="repo-synth-org-1/archived-repo"]')
    assert page.get_attribute("[data-testid=table-empty]", "data-kind") == "no-issues"
    page.fill("[data-testid=repo-search]", "repo-01")
    assert page.locator("[data-testid=repo-list] .repo").count() == 2  # All + the match
    page.click("[data-testid=repo-all]")
    assert page.text_content("[data-testid=issue-count]") == f"{total} of {total} issues"


def test_inspector_lists_runs_and_stacked_prs_with_provenance(page, served):
    url, svc = served()
    open_page(page, url)
    row(page, "issue:repo:github.com/synth-org-0/repo-00#1").click()
    inspector = page.locator("[data-testid=inspector]")
    runs = inspector.locator("[data-testid=run-card]")
    assert runs.count() == 2  # two runs on one issue, listed separately
    assert len(set(runs.evaluate_all("els => els.map(e => e.dataset.run)"))) == 2
    assert "Provenance: office-record (landing_json.issue)" in runs.first.text_content()
    prs = inspector.locator("[data-testid=pr-card]")
    assert prs.count() >= 1
    assert all("Provenance: office-record (tasks.pr_json)" in t for t in prs.all_text_contents())
    assert all("←" in t for t in inspector.locator("[data-testid=pr-base-head]").all_text_contents())
    snap = svc.snapshot()
    stacked = [p for r in snap["entities"]["runs"].values() for p in r["prs"] if p.get("stacked_on")]
    run = snap["entities"]["runs"][stacked[0]["task"].replace("task:", "run:").rsplit("/", 1)[0]]
    page.click("[data-testid=repo-all]")
    find(page, run["run_id"])
    stack = page.locator("[data-testid=pr-stacked]")
    assert stack.count() >= 1 and "task T" in stack.first.text_content()
    base = page.locator("[data-testid=pr-card]", has=page.locator("[data-testid=pr-stacked]")).first
    assert "office/" in base.locator("[data-testid=pr-base-head]").text_content()  # stacked: base is a branch


def test_no_run_issue_offers_copyable_start_and_authorization(page, served):
    url, svc = served()
    open_page(page, url)
    row(page, "issue:repo:github.com/synth-org-0/repo-00#3").click()
    cmd = page.text_content("[data-testid=start-command]")
    assert cmd == ('office start --issue https://github.com/synth-org-0/repo-00/issues/3 --end-state preview '
                   '"Resolve issue https://github.com/synth-org-0/repo-00/issues/3"')
    labels = page.locator("[data-testid=auth-start] option").all_text_contents()
    assert labels == ["Preview only", "PR / no merge", "Merge to main", "Production"]
    page.select_option("[data-testid=auth-start]", "e2e")
    assert "--end-state e2e" in page.text_content("[data-testid=start-command]")
    queue = page.locator("[data-testid=auth-queue] option")
    disabled = queue.evaluate_all("els => els.filter(o => o.disabled).map(o => [o.value, o.title])")
    assert [d[0] for d in disabled] == ["ask", "merge", "e2e"]
    assert all(d[1].startswith("Queued work launches with the repository default") for d in disabled)
    assert page.is_enabled("[data-testid=action-start_issue]")
    assert page.is_enabled("[data-testid=action-queue_issue]")
    assert page.is_enabled("[data-testid=action-copy_start]")


def test_not_ready_repository_disables_start_and_names_prerequisites(page, served):
    url, svc = served()
    open_page(page, url)
    row(page, "issue:repo:github.com/synth-org-0/not-ready#1").click()
    assert page.is_disabled("[data-testid=action-start_issue]")
    assert page.is_disabled("[data-testid=action-queue_issue]")
    why = page.text_content("[data-testid=why-start_issue]")
    assert "checkout_exists" in why and "git_repository" in why and "origin_matches" in why
    assert page.text_content("[data-testid=why-queue_issue]") == why
    failing = page.locator("[data-testid=prerequisites] li.fail").all_text_contents()
    assert [f.split(":")[0] for f in failing] == ["checkout_exists", "git_repository", "origin_matches"]
    assert page.is_enabled("[data-testid=action-copy_start]")  # the copyable command stays available


def test_run_actions_attach_resume_and_capability_unavailable(page, served):
    url, svc = served()
    open_page(page, url)
    resumable = next(i for i in svc.snapshot()["entities"]["issues"].values() if i["resumable_run"])
    row(page, resumable["id"]).click()
    buttons = page.locator("[data-testid=actions] button")
    assert buttons.all_text_contents()[:2] == ["Resume", "Attach"]
    assert "primary" in buttons.first.get_attribute("class")
    assert cells(row(page, resumable["id"]))[11] == "Resume"
    live = "issue:repo:github.com/synth-org-0/repo-00#1"
    row(page, live).click()
    assert page.locator("[data-testid=actions] button").all_text_contents() == ["Attach"]
    legacy = [r for r in svc.snapshot()["entities"]["runs"].values() if r["controls"]["runtime"]["read_only"]
              and r["liveness"] != "terminal"]
    assert legacy
    page.fill("[data-testid=issue-search]", legacy[0]["run_id"])
    page.locator("[data-testid=issue-row]").first.click()
    card = page.locator(f'[data-testid=run-card][data-run="{legacy[0]["id"]}"]')
    assert "Capability unavailable: Auto Office 3.0 run" in card.text_content()


def test_commands_carry_fresh_ids_and_show_receipts(page, served):
    url, svc = served()
    open_page(page, url)
    sent = []
    page.on("request", lambda r: sent.append(json.loads(r.post_data)) if r.url.endswith("/api/commands") else None)
    row(page, "issue:repo:github.com/synth-org-0/repo-00#3").click()
    page.select_option("[data-testid=auth-start]", "merge")
    page.click("[data-testid=action-start_issue]")
    page.wait_for_selector('[data-testid=receipt][data-status=completed] >> text=Start #3', timeout=10000)
    page.click("[data-testid=action-queue_issue]")
    page.wait_for_selector('[data-testid=receipt][data-status=completed] >> text=Auto Queue #3', timeout=10000)
    assert [s["kind"] for s in sent] == ["start_issue", "queue_issue"]
    assert sent[0]["payload"]["end_state"] == "merge" and sent[0]["target"] == {"repo": "synth-org-0/repo-00", "issue": 3}
    ids = [s["id"] for s in sent]
    assert all(i.startswith("web-") for i in ids) and len(set(ids)) == 2
    assert svc.command(ids[0])["status"] == "completed"
    statuses = set(page.locator("[data-testid=receipt]").evaluate_all("els => els.map(e => e.dataset.status)"))
    assert {"pending", "completed", "failed", "unknown"} <= statuses  # seeded accepted/running read as pending


def test_unknown_result_is_shown_and_never_retried(page, served):
    url, svc = served()
    open_page(page, url)
    attempts = []

    def drop(route):
        attempts.append(route.request.post_data)
        route.abort("connectionreset")
    page.route("**/api/commands", drop)
    row(page, "issue:repo:github.com/synth-org-0/repo-00#3").click()
    page.click("[data-testid=action-start_issue]")
    receipt = page.locator("[data-testid=receipt][data-status=unknown] >> text=Start #3")
    receipt.wait_for(timeout=10000)
    assert "result unknown, not retried" in receipt.text_content()
    # The same command is not offered again while its result is unknown.
    assert page.is_disabled("[data-testid=action-start_issue]")
    assert "unknown result" in page.text_content("[data-testid=why-start_issue]")
    rev = page.evaluate("window.officeStore.state.rev")
    page.evaluate("""(token) => fetch('/api/fixture/github', {method: 'POST', headers: {'Content-Type': 'application/json',
        'X-Office-Token': token}, body: JSON.stringify({repo: 'synth-org-1/repo-01', state: 'rate_limited'})})""", svc.token)
    page.wait_for_function("(r) => window.officeStore.state.rev > r", arg=rev, timeout=10000)
    time.sleep(3)  # longer than the first backoff steps; nothing re-sends it
    assert len(attempts) == 1
    assert page.locator("[data-testid=receipt][data-status=unknown] >> text=Start #3").count() == 1
    page.click("[data-testid=receipt-checked]")  # the operator checked the result: sending again is allowed
    assert page.is_enabled("[data-testid=action-start_issue]")
    assert len(attempts) == 1


def test_a_second_click_while_pending_sends_nothing(page, served):
    url, svc = served()
    open_page(page, url)
    release = []
    seen = []

    def hold(route):
        seen.append(route.request.post_data)
        release.append(route)
    page.route("**/api/commands", hold)
    row(page, "issue:repo:github.com/synth-org-0/repo-00#3").click()
    page.click("[data-testid=action-start_issue]")
    page.wait_for_selector("[data-testid=receipt][data-status=pending] >> text=Start #3", timeout=10000)
    assert page.is_disabled("[data-testid=action-start_issue]")
    page.click("[data-testid=action-start_issue]", force=True)
    page.wait_for_timeout(300)
    assert len(seen) == 1
    release[0].continue_()
    page.wait_for_selector('[data-testid=receipt][data-status=completed] >> text=Start #3', timeout=10000)


def test_failed_command_is_shown_failed(page, served):
    url, svc = served()
    open_page(page, url)
    row(page, "issue:repo:github.com/synth-org-0/repo-00#3").click()
    page.route("**/api/commands", lambda route: route.fulfill(
        status=409, content_type="application/json",
        body=json.dumps({"ok": False, "reason": "repo-not-ready", "message": "not execution-ready", "receipt": None})))
    page.click("[data-testid=action-start_issue]")
    failed = page.locator("[data-testid=receipt][data-status=failed] >> text=Start #3")
    failed.wait_for(timeout=10000)
    assert failed.get_attribute("title") == "not execution-ready"


def test_stale_office_disables_commands_with_reason(page, served):
    url, svc = served()
    open_page(page, url)
    row(page, "issue:repo:github.com/synth-org-0/repo-00#3").click()
    assert page.is_enabled("[data-testid=action-start_issue]")
    with svc.lock:
        svc.stale_after = -1  # every read is now older than the limit
        svc._rebuild(reuse_entities=True)
    page.wait_for_selector("[data-testid=banner-office-stale]", timeout=10000)
    for kind in ("start_issue", "queue_issue"):
        assert page.is_disabled(f"[data-testid=action-{kind}]")
        assert "Office data is stale" in page.text_content(f"[data-testid=why-{kind}]")
    live = row(page, "issue:repo:github.com/synth-org-0/repo-00#1")
    assert live.locator("button").is_disabled() and "Office data is stale" in live.locator("button").get_attribute("title")
    with svc.lock:
        svc.stale_after = 15.0
        svc.poll(force=True)
    page.wait_for_selector("[data-testid=banner-office-stale]", state="detached", timeout=10000)
    assert page.is_enabled("[data-testid=action-start_issue]") and live.locator("button").is_enabled()


def test_revoked_repository_hides_github_issues_but_keeps_office_runs(page, served):
    url, svc = served()
    open_page(page, url)
    before = page.locator("[data-testid=issue-row]").count()
    page.evaluate("""(token) => fetch('/api/fixture/github', {method: 'POST', headers: {'Content-Type': 'application/json',
        'X-Office-Token': token}, body: JSON.stringify({repo: 'synth-org-1/repo-01', state: 'revoked'})})""", svc.token)
    page.wait_for_selector("[data-testid=banner-revoked]", timeout=10000)
    assert "synth-org-1/repo-01" in page.text_content("[data-testid=banner-revoked]")
    mark = page.locator('[data-testid="repo-synth-org-1/repo-01"] [data-testid=mark-github]')
    assert mark.text_content().startswith("GitHub revoked")
    assert row(page, "issue:repo:github.com/synth-org-1/repo-01#1").count() == 1  # still known from its run
    assert cells(row(page, "issue:repo:github.com/synth-org-1/repo-01#1"))[1].endswith("From Office record")
    assert page.locator("[data-testid=issue-row]").count() < before
