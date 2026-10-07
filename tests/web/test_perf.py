"""Performance budgets on the large synthetic workspace (`--fixture large`).

Budgets come from measurements recorded in docs/web-ui.md (machine and numbers)
and carry headroom for slower or busier machines. `measure()` prints what each
test observed, so `pytest -s` reproduces the table.
"""
from __future__ import annotations

import json
import statistics
import threading
import time

import pytest

from office import db
from office.web import server

pytestmark = pytest.mark.integration

# Budgets: measured medians in docs/web-ui.md ("Performance", Apple M1 Pro, 3 runs) times about 3 to 6,
# so they hold under `pytest -n 2` and on slower machines.
SNAPSHOT_BUILD_S = 2.0       # measured 0.48 s
SNAPSHOT_BYTES = 30_000_000  # measured 15.2 MB
DELTA_LATENCY_S = 2.5        # measured 0.76 s (0.2 s poll interval plus a rebuild)
FIRST_RENDER_S = 3.0         # measured 0.67 s
SCROLL_FRAME_MS = 40.0       # measured 7.5 ms p95
RECONNECT_RESYNC_S = 3.0     # measured 0.56 s


def measure(name: str, value: float, budget: float, unit: str) -> None:
    print(f"perf {name}: {value:.3f} {unit} (budget {budget:g} {unit})")
    assert value <= budget, f"{name} {value:.3f} {unit} exceeds the {budget:g} {unit} budget"


@pytest.fixture
def page():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - no browser binary installed
            pytest.skip(f"chromium unavailable: {str(exc).splitlines()[0]}")
        try:
            yield browser.new_page()
        finally:
            browser.close()


@pytest.fixture(scope="module")
def large(tmp_path_factory):
    home = tmp_path_factory.mktemp("large")
    mp = pytest.MonkeyPatch()
    mp.setenv("OFFICE_USER_CONFIG", str(home / "user.yaml"))
    svc = server.build_fixture("large", home=home / "fx").start()
    svc.run_poller(interval=0.2)
    httpd = server.make_server(svc, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield svc, httpd, f"http://127.0.0.1:{httpd.server_address[1]}/"
    svc.close()
    httpd.shutdown()
    httpd.server_close()
    mp.undo()


def test_snapshot_build_time_and_payload_size(large):
    svc, _, _ = large
    times = []
    for _ in range(3):
        t0 = time.perf_counter()
        svc.poll(force=True)
        times.append(time.perf_counter() - t0)
    measure("snapshot build", statistics.median(times), SNAPSHOT_BUILD_S, "s")
    snap = svc.snapshot()
    assert len(snap["entities"]["issues"]) >= 2000
    size = len(json.dumps(snap, separators=(",", ":")).encode())
    measure("snapshot payload", size, SNAPSHOT_BYTES, "bytes")


def test_delta_latency(large):
    svc, _, _ = large
    run_id = next(r["run_id"] for r in svc.snapshot()["entities"]["runs"].values() if r["liveness"] == "live")
    samples = []
    for i in range(3):
        rev = svc.rev
        t0 = time.perf_counter()
        con = db.connect(svc.db_path)
        try:
            with db.transaction(con):
                con.execute("UPDATE runs SET goal=? WHERE id=?", (f"perf goal {i}", run_id))
        finally:
            con.close()
        deltas = []
        while not deltas and time.perf_counter() - t0 < DELTA_LATENCY_S * 3:
            deltas = [d for d in svc.wait_for(rev, 0.5) if (d["upserts"].get("runs") or {})]
        samples.append(time.perf_counter() - t0)
        assert deltas, "no delta for the changed run"
    measure("delta latency", max(samples), DELTA_LATENCY_S, "s")


def test_first_render_scroll_and_reconnect_in_chromium(large, page):
    svc, httpd, url = large
    page.set_viewport_size({"width": 1440, "height": 900})
    t0 = time.perf_counter()
    page.goto(url)
    page.wait_for_selector("[data-testid=issue-row]", timeout=60000)
    measure("first render", time.perf_counter() - t0, FIRST_RENDER_S, "s")
    frames = page.evaluate("""() => {
        const el = document.getElementById('issue-table');
        const out = [];
        for (let i = 1; i <= 60; i += 1) {
            const t0 = performance.now();
            el.scrollTop = i * (el.scrollHeight / 60);
            el.dispatchEvent(new Event('scroll'));
            out.push(performance.now() - t0);
        }
        return out.sort((a, b) => a - b);
    }""")
    measure("table scroll p95 frame", frames[int(len(frames) * 0.95) - 1], SCROLL_FRAME_MS, "ms")
    # A dropped stream that cannot resume: reconnect from scratch and wait for the full snapshot.
    before = page.evaluate("window.officeStore.stats.snapshots + window.officeStore.stats.resyncs")
    t0 = time.perf_counter()
    page.evaluate("window.officeStore.connect({ fresh: true })")
    page.wait_for_function("(n) => window.officeStore.stats.snapshots + window.officeStore.stats.resyncs > n"
                           " && window.officeStore.status === 'live'", arg=before, timeout=int(RECONNECT_RESYNC_S * 3000))
    page.wait_for_selector("[data-testid=issue-row]")
    measure("reconnect to resync", time.perf_counter() - t0, RECONNECT_RESYNC_S, "s")
