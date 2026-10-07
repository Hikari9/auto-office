"""End to end: the real `office web serve --fixture small` process driven by Chromium.

Everything the tests know about the workspace comes from what the page itself
received (`window.officeStore.state`) or from the HTTP API, never from the
service object: the server is a separate process, restarted in place for the
reconnect test.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

from office.web.executor import office_argv

VIEWPORTS = [(1440, 900), (1920, 1080), (1100, 800)]
SURFACES = ["issues", "agents", "allocation", "settings"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    """`office web serve --fixture small` on a fixed port, in its own Office homes."""

    def __init__(self, home: Path):
        self.home, self.port, self.proc = home, _free_port(), None
        self.env = {**os.environ, "OFFICE_STATE_HOME": str(home / "state"), "OFFICE_USER_CONFIG": str(home / "user.yaml"),
                    "OFFICE_DATA_HOME": str(home / "data")}
        self.url = f"http://127.0.0.1:{self.port}/"

    def start(self, timeout: float = 60) -> "Server":
        pid = self.home / "state" / "web" / "web.pid"
        pid.unlink(missing_ok=True)
        log = open(self.home / "serve.log", "ab")  # noqa: SIM115 - closed with the process
        self.proc = subprocess.Popen([*office_argv(), "web", "serve", "--fixture", "small", "--port", str(self.port)],
                                     env=self.env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if pid.exists() and json.loads(pid.read_text()).get("pid") == self.proc.pid:
                return self
            if self.proc.poll() is not None:
                raise AssertionError((self.home / "serve.log").read_text()[-2000:])
            time.sleep(0.1)
        raise AssertionError("office web serve did not report ready")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)


@pytest.fixture
def server(tmp_path):
    srv = Server(tmp_path).start()
    yield srv
    srv.stop()


def open_ui(page, url, width=1440, height=900):
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.set_viewport_size({"width": width, "height": height})
    page.goto(url)
    page.wait_for_selector("[data-testid=issue-row]", timeout=30000)
    return errors


def state(page) -> dict:
    return page.evaluate("() => window.officeStore.state")


def post(page, body) -> dict:
    """POST a command the way the page does (same token), returning {status, body}."""
    return page.evaluate("""async (body) => {
        const token = document.querySelector('meta[name="office-token"]').content;
        const res = await fetch('/api/commands', {method: 'POST', body: JSON.stringify(body),
            headers: {'Content-Type': 'application/json', 'X-Office-Token': token}});
        return {status: res.status, body: await res.json()};
    }""", body)


def fixture_github(page, repo, mode):
    return page.evaluate("""async ([repo, state]) => {
        const token = document.querySelector('meta[name="office-token"]').content;
        const res = await fetch('/api/fixture/github', {method: 'POST', body: JSON.stringify({repo, state}),
            headers: {'Content-Type': 'application/json', 'X-Office-Token': token}});
        return res.status;
    }""", [repo, mode])


def wait_command(page, cid, timeout=15000):
    page.wait_for_function("""(id) => { const c = window.officeStore.state.entities.commands['command:' + id];
        return c && ['completed', 'failed', 'unknown'].includes(c.status); }""", arg=cid, timeout=timeout)
    return state(page)["entities"]["commands"][f"command:{cid}"]


def row(page, issue_id):
    return page.locator(f'[data-testid=issue-row][data-id="{issue_id}"]')


def show(page, query):
    page.fill("[data-testid=issue-search]", query)


# ------------------------------------------------------------------ every surface at every viewport

@pytest.mark.parametrize("width,height", VIEWPORTS, ids=[f"{w}x{h}" for w, h in VIEWPORTS])
@pytest.mark.parametrize("surface", SURFACES)
def test_each_surface_renders_at_each_viewport(page, server, surface, width, height):
    errors = open_ui(page, server.url, width, height)
    if surface != "issues":
        page.click(f"[data-surface={surface}]")
    view = page.locator(f"[data-testid=surface-{surface}]")
    assert view.is_visible()
    page.wait_for_function("(s) => document.querySelector(`[data-testid=surface-${s}]`).childElementCount > 0", arg=surface)
    # The page itself never scrolls sideways: wide tables scroll inside their own container.
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    rail = page.locator(".product-rail").bounding_box()
    assert rail and rail["x"] >= 0 and rail["x"] + rail["width"] <= width
    assert errors == []


# ------------------------------------------------------------------ Issues

def test_repo_switching_filters_rows_and_back(page, server):
    open_ui(page, server.url)
    page.click("[data-testid=filter-open]")
    total = page.text_content("[data-testid=issue-count]")
    page.click('[data-testid="repo-synth-org-0/repo-00"]')
    names = page.locator("[data-testid=issue-row] [role=gridcell]:first-child").all_text_contents()
    assert names and set(names) == {"synth-org-0/repo-00"}
    page.click('[data-testid="repo-synth-org-1/repo-01"]')
    names = page.locator("[data-testid=issue-row] [role=gridcell]:first-child").all_text_contents()
    assert names and set(names) == {"synth-org-1/repo-01"}
    assert page.get_attribute('[data-testid="repo-synth-org-1/repo-01"]', "aria-pressed") == "true"
    page.click("[data-testid=repo-all]")
    assert page.text_content("[data-testid=issue-count]") == total


def test_issue_drills_down_to_its_runs_and_prs(page, server):
    open_ui(page, server.url)
    s = state(page)
    issue = next(i for i in s["entities"]["issues"].values()
                 if any(s["entities"]["runs"].get(r, {}).get("prs") for r in i["runs"]))
    show(page, f"#{issue['number']}")
    row(page, issue["id"]).click()
    runs = [s["entities"]["runs"][r] for r in issue["runs"]]
    cards = page.locator("[data-testid=run-card]")
    assert cards.count() == len(runs)
    assert {cards.nth(i).get_attribute("data-run") for i in range(cards.count())} == {r["id"] for r in runs}
    assert page.locator("[data-testid=pr-card]").count() == sum(len(r["prs"]) for r in runs)
    base_head = page.locator("[data-testid=pr-base-head]").first.text_content()
    assert "←" in base_head


def test_start_attach_resume_eligibility(page, server):
    open_ui(page, server.url)
    s = state(page)
    issues = s["entities"]["issues"]
    live = next(i for i in issues.values() if i["live_run"])
    resumable = next(i for i in issues.values() if i["resumable_run"])
    show(page, "")
    row(page, live["id"]).click()
    assert page.locator("[data-testid=actions] button").all_text_contents() == ["Attach"]
    row(page, resumable["id"]).click()
    assert page.locator("[data-testid=actions] button").all_text_contents()[:2] == ["Resume", "Attach"]
    page.click("[data-testid=filter-incoming]")
    ready = "issue:repo:github.com/synth-org-0/repo-00#3"
    row(page, ready).click()
    assert page.is_enabled("[data-testid=action-start_issue]")
    not_ready = "issue:repo:github.com/synth-org-0/not-ready#1"
    row(page, not_ready).click()
    assert page.is_disabled("[data-testid=action-start_issue]")
    assert "not execution-ready" in page.text_content("[data-testid=why-start_issue]")
    # Start runs through the fixture launcher and leaves a completed receipt for that exact issue.
    row(page, ready).click()
    page.click("[data-testid=action-start_issue]")
    page.wait_for_function("() => Object.values(window.officeStore.state.entities.commands)"
                           ".some(c => c.kind === 'start_issue' && c.origin === 'web' && c.status === 'completed')")
    started = [c for c in state(page)["entities"]["commands"].values() if c["kind"] == "start_issue" and c["origin"] == "web"]
    assert [c["target"] for c in started] == [{"repo": "synth-org-0/repo-00", "issue": 3}]


def test_plan_authorization_is_a_copyable_command_not_a_control(page, server):
    open_ui(page, server.url)
    s = state(page)
    run = next(r for r in s["entities"]["runs"].values() if r.get("awaiting_plan_authorization") and r["issue"])
    show(page, run["run_id"])
    page.locator("[data-testid=issue-row]").first.click()
    card = page.locator(f'[data-testid=run-card][data-run="{run["id"]}"]')
    assert card.locator("[data-testid=plan-approval-command]").text_content() == 'office approve plan --quote "<words>"'
    assert page.locator("button", has_text="Approve").count() == 0
    refused = post(page, {"id": "e2e-plan-00001", "kind": "approve_plan", "target": {"run_id": run["run_id"]},
                          "payload": {"quote": "yes"}})
    assert (refused["status"], refused["body"]["reason"]) == (400, "unknown-kind")


# ------------------------------------------------------------------ Allocation

def test_pause_and_resume_from_allocation(page, server):
    open_ui(page, server.url)
    page.click("[data-surface=allocation]")
    paused = page.locator("[data-testid=alloc-row]:has([data-testid=alloc-pause]:enabled)").first
    item = paused.get_attribute("data-id")
    paused.locator("[data-testid=alloc-pause]").click()
    page.wait_for_function("(id) => Object.values(window.officeStore.state.entities.commands)"
                           ".some(c => c.kind === 'pause' && c.status === 'completed')", arg=item)
    cmd = next(c for c in state(page)["entities"]["commands"].values() if c["kind"] == "pause")
    entry = state(page)["entities"]["queue"][item]
    assert cmd["target"] == ({"run_id": entry["run_id"], **({"task_id": entry["task_id"]} if entry.get("task_id") else {})}
                             if entry.get("run_id") else {"item": item})
    assert cmd["result"]["args"][:2] == ["queue", "pause"]


def test_unavailable_telemetry_is_shown_not_invented(page, server):
    open_ui(page, server.url)
    assert page.text_content("[data-testid=kpi-cpu]").endswith("CPU telemetry unavailable")
    page.click("[data-surface=allocation]")
    statuses = page.locator(".metric").evaluate_all("els => els.map(e => e.dataset.status)")
    assert "unavailable" in statuses
    assert not page.locator(".metric[data-status=unavailable] .v").filter(has_text="%").count()


# ------------------------------------------------------------------ Agents: chat and routing

def orchestrators(page):
    s = state(page)
    return [a for a in s["entities"]["agents"].values() if a["column"] == "orchestrators"
            and s["entities"]["runs"][a["run"]]["controls"]["chat_send"]["allowed"]]


def inspect(page, node_id):
    page.locator(f'[data-testid=agent-node][data-id="{node_id}"]').click()
    page.wait_for_selector(f'[data-testid=agent-inspector][data-id="{node_id}"]', timeout=5000)


def test_chat_goes_to_the_exact_target_and_never_retargets(page, server):
    open_ui(page, server.url)
    page.click("[data-surface=agents]")
    page.wait_for_selector("[data-testid=agent-node]")
    page.check("[data-testid=agents-include-completed]")
    a, b = orchestrators(page)[:2]
    inspect(page, a["id"])
    page.fill("[data-testid=chat-input]", "for A only")
    inspect(page, b["id"])
    assert page.input_value("[data-testid=chat-input]") == ""  # A's draft did not follow the selection
    inspect(page, a["id"])
    assert page.input_value("[data-testid=chat-input]") == "for A only"
    run = state(page)["entities"]["runs"][a["run"]]
    # A command naming another session of the same run is refused before anything is delivered.
    wrong = post(page, {"id": "e2e-chat-wrong1", "kind": "chat_send",
                        "target": {"run_id": run["run_id"], "session": b["id"]}, "payload": {"text": "x"}})
    assert wrong["status"] in (400, 409) and wrong["body"]["reason"] != "ok"
    commands = state(page)["entities"]["commands"]
    assert not any(c["kind"] == "chat_send" and c["status"] == "completed" for c in commands.values())


def test_adaptive_routing_shows_the_recorded_decision(page, server):
    open_ui(page, server.url)
    page.click("[data-surface=agents]")
    page.wait_for_selector("[data-testid=agent-node]")
    page.check("[data-testid=agents-include-completed]")
    s = state(page)
    node = next(a for a in s["entities"]["agents"].values() if a["column"] == "executors" and a.get("task")
                and s["entities"]["tasks"][a["task"]]["route"]["available"])
    inspect(page, node["id"])
    route = s["entities"]["tasks"][node["task"]]["route"]
    assert page.text_content("[data-testid=route-primary]") == route["primary"]


# ------------------------------------------------------------------ duplicate and stale commands

def test_a_duplicate_command_id_runs_once(page, server):
    open_ui(page, server.url)
    run = next(r for r in state(page)["entities"]["runs"].values()
               if r["liveness"] == "live" and r["controls"]["pause"]["allowed"])
    body = {"id": "e2e-dup-000001", "kind": "pause", "target": {"run_id": run["run_id"]}}
    first, second = post(page, body), post(page, body)
    assert first["status"] in (200, 202) and second["body"]["receipt"]["replayed"] is True
    other = post(page, {**body, "payload": {"reason": "changed"}})
    assert other["body"]["reason"] == "idempotency-conflict"
    wait_command(page, "e2e-dup-000001")
    assert sum(1 for c in state(page)["entities"]["commands"].values() if c["id"] == "e2e-dup-000001") == 1


def test_a_command_built_on_a_stale_view_is_refused(page, server):
    open_ui(page, server.url)
    run = next(r for r in state(page)["entities"]["runs"].values()
               if r["liveness"] == "live" and r["controls"]["pause"]["allowed"])
    stale = post(page, {"id": "e2e-stale-00001", "kind": "pause", "target": {"run_id": run["run_id"]},
                        "expect": {"phase": "a-phase-it-no-longer-has"}})
    assert (stale["status"], stale["body"]["reason"]) == (409, "expectation-failed")
    assert wait_command(page, "e2e-stale-00001")["status"] == "failed"


# ------------------------------------------------------------------ GitHub states

def test_github_revoked_and_rate_limited_states(page, server):
    open_ui(page, server.url)
    assert fixture_github(page, "synth-org-1/repo-01", "rate_limited") == 200
    page.wait_for_selector("[data-testid=banner-rate-limited]", timeout=10000)
    assert "synth-org-1/repo-01" in page.text_content("[data-testid=banner-rate-limited]")
    assert fixture_github(page, "synth-org-0/repo-00", "revoked") == 200
    page.wait_for_selector("[data-testid=banner-revoked]", timeout=10000)
    mark = page.locator('[data-testid="repo-synth-org-0/repo-00"] [data-testid=mark-github]')
    assert mark.get_attribute("data-tone") == "err"
    # Issues known from Office records stay; start is refused for the revoked repository.
    show(page, "")
    page.click("[data-testid=filter-incoming]")
    assert not page.locator('[data-testid=issue-row][data-id^="issue:repo:github.com/synth-org-0/repo-00#"]').count()


# ------------------------------------------------------------------ restart

def test_reconnects_and_resyncs_after_a_service_restart(page, server):
    open_ui(page, server.url)
    before = state(page)["epoch"]
    server.stop()
    page.wait_for_selector("[data-testid=banner-disconnected]", timeout=15000)
    t0 = time.monotonic()
    server.start()
    page.wait_for_function("(e) => window.officeStore.state.epoch !== e && window.officeStore.status === 'live'",
                           arg=before, timeout=30000)
    assert time.monotonic() - t0 < 30
    page.wait_for_selector("[data-testid=banner-disconnected]", state="detached", timeout=10000)
    assert page.locator("[data-testid=issue-row]").count() > 0
    assert page.text_content("[data-testid=stream-state]") == "stream live"
