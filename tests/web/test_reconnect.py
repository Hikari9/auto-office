"""Snapshot + delta stream: revisions, resync on gaps and restarts, freshness."""
from __future__ import annotations

import http.client
import json
import threading

import pytest

from office import db
from office.web import server, synthetic
from office.web.service import RING, Command, CommandRefused, apply_delta


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp_path / "user.yaml"))
    s = server.build_fixture("small", home=tmp_path / "fx").start()
    yield s
    s.close()


def write(svc, fn):
    con = db.connect(svc.db_path)
    try:
        with db.transaction(con):
            fn(con)
    finally:
        con.close()


def test_initial_snapshot_shape(svc):
    snap = svc.snapshot()
    assert snap["rev"] == 1 and snap["epoch"] == svc.epoch
    assert set(snap["entities"]) == {"repos", "issues", "prs", "runs", "tasks", "agents", "queue", "commands"}
    assert snap["freshness"]["office"]["state"] == "live" and snap["freshness"]["github"]["state"] == "fresh"
    assert set(snap["scalars"]) >= {"scheduler", "host", "fixture"}
    run_id, run = next(iter(snap["entities"]["runs"].items()))
    assert run_id == run["id"] and run_id.startswith("run:")
    assert all(k.startswith("task:") for k in snap["entities"]["tasks"])
    assert svc.events_since(None) == ("snapshot", [])


def test_incremental_delta_and_no_op_poll(svc):
    client = svc.snapshot()
    assert svc.poll() is None  # nothing moved: no revision
    write(svc, lambda con: synthetic.insert_run(con, "NEW-RUN", git_common_dir="/synthetic/src/repo-00/.git"))
    delta = svc.poll()
    assert delta["base_rev"] == 1 and delta["rev"] == 2 and delta["epoch"] == svc.epoch
    assert list(delta["upserts"]["runs"]) == ["run:NEW-RUN"]
    assert "removes" in delta and "scalars" in delta
    assert apply_delta(client, delta) == "applied"
    assert client["entities"] == svc.snapshot()["entities"] and client["rev"] == 2
    write(svc, lambda con: con.execute("DELETE FROM runs WHERE id='NEW-RUN'"))
    gone = svc.poll()
    assert gone["removes"]["runs"] == ["run:NEW-RUN"]
    assert apply_delta(client, gone) == "applied" and "run:NEW-RUN" not in client["entities"]["runs"]


def test_event_seq_change_alone_rebuilds(svc):
    run_id = next(iter(svc.snapshot()["entities"]["runs"].values()))["run_id"]
    before = svc.marker
    write(svc, lambda con: synthetic.insert_event(con, run_id, "note", "hello"))
    svc.poll()
    assert svc.marker != before


def test_duplicate_delta_is_ignored(svc):
    client = svc.snapshot()
    write(svc, lambda con: synthetic.insert_run(con, "DUP", git_common_dir="/x/.git"))
    delta = svc.poll()
    assert apply_delta(client, delta) == "applied"
    assert apply_delta(client, delta) == "duplicate"
    assert client["rev"] == delta["rev"]


def test_gap_resyncs(svc):
    client = svc.snapshot()
    for n in range(2):
        write(svc, lambda con, n=n: synthetic.insert_run(con, f"GAP{n}", git_common_dir="/x/.git"))
        svc.poll()
    last = svc.ring[-1]
    assert apply_delta(client, last) == "resync"  # base_rev 2 != client rev 1
    mode, pending = svc.events_since(f"{svc.epoch}:1")
    assert mode == "deltas" and [(d["base_rev"], d["rev"]) for d in pending] == [(1, 2), (2, 3)]
    for d in pending:
        assert apply_delta(client, d) == "applied"
    assert client["entities"] == svc.snapshot()["entities"]
    for bad in ("1", f"{svc.epoch}:", f"{svc.epoch}:\u00b2", f"{svc.epoch}:\u0663", f"{svc.epoch}:-1"):
        assert svc.events_since(bad) == ("resync", [])
    assert svc.events_since(f"{svc.epoch}:99")[0] == "resync"  # from the future
    svc.ring.clear()
    assert svc.events_since(f"{svc.epoch}:1")[0] == "resync"  # fell out of the ring
    assert RING >= 64


def test_ring_eviction_resyncs_the_oldest_base(svc, monkeypatch):
    import collections
    monkeypatch.setattr(svc, "ring", collections.deque(maxlen=2))
    for n in range(3):
        write(svc, lambda con, n=n: synthetic.insert_run(con, f"EV{n}", git_common_dir="/x/.git"))
        svc.poll()
    assert svc.events_since(f"{svc.epoch}:1") == ("resync", [])  # rev 2's delta was evicted
    assert [d["rev"] for d in svc.events_since(f"{svc.epoch}:2")[1]] == [3, 4]


def test_restart_gets_a_new_epoch_and_resyncs(svc, tmp_path):
    old = svc.snapshot()
    again = server.Service(svc.db_path, svc.state_home, launcher=svc.launcher, executor=svc.executor,
                           resolver=svc.resolver, github=svc.github, readiness=svc.readiness,
                           host_probe=svc.host_probe, observer_ctx=svc.observer_ctx).start()
    try:
        assert again.epoch != old["epoch"] and again.host_id == svc.host_id
        assert again.events_since(f"{old['epoch']}:{old['rev']}") == ("resync", [])
        assert again.events_since("garbage") == ("resync", [])
        delta_from_old = {**again.snapshot(), "base_rev": old["rev"], "rev": old["rev"] + 1}
        assert apply_delta(old, delta_from_old) == "resync"
    finally:
        again.close()


def test_office_freshness_stale_and_disconnected(svc, tmp_path):
    run = next(r for r in svc.snapshot()["entities"]["runs"].values() if r["liveness"] == "live")
    now = [svc.clock()]
    svc.clock = lambda: now[0]
    svc.poll(force=True)
    now[0] += svc.stale_after + 1
    assert svc.office_freshness()["state"] == "stale"
    with pytest.raises(CommandRefused) as info:
        svc.submit(Command.parse({"id": "cmd-fresh-01", "kind": "pause", "target": {"run_id": run["run_id"]}}))
    assert info.value.reason == "office-stale" and svc.executor.calls == []
    assert svc.poll()["scalars"]["freshness"]["office"]["state"] == "live"
    moved = tmp_path / "moved.db"
    svc.db_path.rename(moved)
    delta = svc.poll()
    assert delta["scalars"]["freshness"]["office"]["state"] == "disconnected"
    assert not delta["upserts"] and not delta["removes"]  # entities kept, marked not live
    assert svc.snapshot()["freshness"]["office"]["state"] == "disconnected"
    with pytest.raises(CommandRefused) as info:
        svc.submit(Command.parse({"id": "cmd-fresh-02", "kind": "pause", "target": {"run_id": run["run_id"]}}))
    assert info.value.reason == "office-stale"
    moved.rename(svc.db_path)
    assert svc.poll()["scalars"]["freshness"]["office"]["state"] == "live"


# ------------------------------------------------------------------ over HTTP

@pytest.fixture
def http_svc(svc):
    httpd = server.make_server(svc, "127.0.0.1", 0)
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield svc, httpd.server_address[1]
    svc.stopping.set()
    with svc.cond:
        svc.cond.notify_all()
    httpd.shutdown()
    httpd.server_close()


def read_event(resp) -> dict:
    fields = {}
    while True:
        line = resp.fp.readline().decode().rstrip("\n")
        if line == "":
            if fields:
                return fields
            continue
        if line.startswith(":"):
            continue
        k, _, v = line.partition(": ")
        fields[k] = v


def stream(port, last_event_id=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Host": f"127.0.0.1:{port}"}
    if last_event_id:
        headers["Last-Event-ID"] = last_event_id
    conn.request("GET", "/api/stream", headers=headers)
    resp = conn.getresponse()
    assert resp.status == 200 and resp.getheader("Content-Type") == "text/event-stream"
    return conn, resp


def test_sse_snapshot_then_delta_then_reconnect(http_svc):
    svc, port = http_svc
    conn, resp = stream(port)
    first = read_event(resp)
    assert first["event"] == "snapshot" and first["id"] == f"{svc.epoch}:1"
    write(svc, lambda con: synthetic.insert_run(con, "SSE-1", git_common_dir="/x/.git"))
    svc.poll()
    ev = read_event(resp)
    data = json.loads(ev["data"])
    assert ev["event"] == "delta" and ev["id"] == f"{svc.epoch}:2" and data["base_rev"] == 1
    conn.close()
    write(svc, lambda con: synthetic.insert_run(con, "SSE-2", git_common_dir="/x/.git"))
    svc.poll()
    conn, resp = stream(port, f"{svc.epoch}:2")  # caught up from the ring
    ev = read_event(resp)
    assert ev["event"] == "delta" and json.loads(ev["data"])["base_rev"] == 2
    conn.close()
    conn, resp = stream(port, "otherepoch:2")
    ev = read_event(resp)
    assert ev["event"] == "resync" and json.loads(ev["data"])["rev"] == 3
    conn.close()
