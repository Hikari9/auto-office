"""An opencode executor under Herdr: a labelled `mini` pane that passes the pane identity check.

Scripted fakes stand in for herdr and opencode; nothing here calls a real model. The
session store test builds an opencode.db in the shape opencode 2.0.29 keeps (`session_v2`
and `session_message`, read off a real install's schema, content not copied).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import test_herdr_agent_launch as herdr_launch
from test_herdr_agent_launch import BUSY, _calls, _fake, _live_dispatch, launch_in_herdr
from test_opencode_reviewer import MODEL, opencode  # noqa: F401  (the fake opencode fixture)

SID = "ses_0e1f2a3b4c5dXyZ"
WALLS = ["Free usage limit reached", "Rate limit exceeded"]


@pytest.fixture(autouse=True)
def _no_inherited_session(opencode, monkeypatch):
    """The fixture dispatch is a claude route with a session id assigned at creation. Make its row an
    opencode one, which is assigned none, so the launch starts with no recorded session."""
    live = herdr_launch._live_dispatch

    def without_session(env, mp):
        run, d = live(env, mp)
        con = env.con()
        try:
            with con:
                con.execute("UPDATE dispatches SET session_id=NULL, harness='opencode', adapter_id='opencode', model=?, "
                            "effort='high' WHERE id=?", (MODEL, d["id"]))
        finally:
            con.close()
        return run, {**d, "session_id": None}
    monkeypatch.setattr(herdr_launch, "_live_dispatch", without_session)


def _herdr_describes(monkeypatch, repo, *, kind="opencode", cwd=None, session=SID):
    """Answer `pane get`, `agent list` and `agent get` the way herdr 0.9.3 does for the agent the fake
    started, with the cwd, kind and session this test chooses. Everything else goes to the real helper."""
    from office import dispatch
    original = dispatch._herdr_json

    def answer(args):
        state = json.loads(Path(os.environ["FAKE_HERDR_STATE"]).read_text())
        agents, kinds = state.get("pane_agents", {}), state.get("pane_kinds", {})
        where = str(cwd or repo)
        if args[:2] == ["pane", "get"]:
            pane = args[2]
            info = {"pane_id": pane, "cwd": where, "foreground_cwd": where, "focused": False}
            if pane in agents:
                info["agent"] = kind
                info["agent_session"] = {"agent": kind, "kind": "id", "source": f"herdr:{kind}", "value": session}
            return {"pane": info}
        entries = [{"name": name, "agent": kind, "pane_id": pane, "agent_status": "idle",
                    "agent_session": {"agent": kind, "kind": "id", "source": f"herdr:{kind}", "value": session}}
                   for pane, name in agents.items()]
        if args[:2] == ["agent", "list"]:
            return {"agents": entries}
        if args[:2] == ["agent", "get"]:
            return {"agent": next((e for e in entries if e["name"] == args[2]), {})}
        return original(args)
    monkeypatch.setattr(dispatch, "_herdr_json", answer)


@pytest.mark.approved
def test_an_opencode_worker_takes_the_mini_form_in_a_labelled_pane_and_passes_the_identity_check(opencode, monkeypatch):
    from office import dispatch
    _herdr_describes(monkeypatch, opencode.repo)
    state_file, run, d, ddir, res = launch_in_herdr(opencode, monkeypatch, adapter="opencode", model=MODEL,
                                                    effort="high", reads=[BUSY])
    # Herdr, not the headless fallback.
    assert res["launcher"] == "herdr" and res["prompt_landed"] is True, res
    spec = json.loads((ddir / "launch.json").read_text())
    assert "fallback_reason" not in spec and spec["herdr_agent"] == res["agent"]
    calls = _calls(state_file)
    start = [c for c in calls if c[:2] == ["agent", "start"]][-1]
    assert start[start.index("--kind") + 1] == "opencode"
    # The interactive form: `mini` with the pinned model and variant, never --auto, never `run`.
    assert start[start.index("--") + 1:] == ["mini", "--model", f"{MODEL}#high"]
    assert "--auto" not in start and "run" not in start[start.index("--") + 1:]
    # Labelled like every other pane executor.
    label = dispatch.pane_label(run, d, "worker")
    assert label and ["pane", "rename", res["pane"], label] in calls
    # The pane held the identity check: cwd, agent kind, agent name and session were all read and agreed.
    evidence = spec["pane_evidence"]
    assert evidence["mismatch"] is None
    assert (evidence["reported_agent"], evidence["reported_cwd"], evidence["reported_session"]) == (
        res["agent"], str(opencode.repo), SID)
    # The brief pointer was sent once, to that agent, after it started.
    prompts = [c for c in calls if c[:2] == ["agent", "prompt"]]
    assert len(prompts) == 1 and prompts[0][2] == res["agent"] and str(ddir / "brief.md") in prompts[0][3]
    assert calls.index(start) < calls.index(prompts[0])
    # Herdr's reported session is the one recorded for a later resume.
    con = opencode.con()
    try:
        row = con.execute("SELECT launcher, pane_id, session_id FROM dispatches WHERE id=?", (d["id"],)).fetchone()
    finally:
        con.close()
    assert tuple(row) == ("herdr", res["pane"], SID)
    # It ends the way any pane worker does: on its own `office submit`.
    monkeypatch.setattr(dispatch, "_submitted", lambda con, disp: True)
    assert dispatch.watch_herdr_agent(d["id"], spec, poll=0) == (0, "success")


@pytest.mark.approved
@pytest.mark.parametrize("reported, expect", [
    ({"kind": "claude"}, "runs claude"),
    ({"session": "ses_someoneelse"}, None),
], ids=["another-agent-kind", "unrecorded-session-is-not-a-mismatch"])
def test_the_identity_check_stops_an_opencode_pane_that_is_not_this_dispatchs(opencode, monkeypatch, reported, expect):
    _herdr_describes(monkeypatch, opencode.repo, **reported)
    state_file, run, d, ddir, res = launch_in_herdr(opencode, monkeypatch, adapter="opencode", model=MODEL,
                                                    effort="high", reads=[BUSY])
    spec = json.loads((ddir / "launch.json").read_text())
    prompts = [c for c in _calls(state_file) if c[:2] == ["agent", "prompt"]]
    if expect is None:
        # Herdr's own session id is simply the one recorded: nothing to disagree with yet.
        assert spec["pane_evidence"]["mismatch"] is None and len(prompts) == 1
        return
    assert expect in spec["identity_failure"] and spec["prompt_landed"] is False
    assert not prompts  # no brief into a pane that is not this dispatch's opencode


@pytest.mark.approved
def test_an_opencode_pane_in_another_directory_never_gets_the_agent_or_the_brief(opencode, monkeypatch):
    elsewhere = opencode.tmp / "another-task"
    elsewhere.mkdir()
    _herdr_describes(monkeypatch, opencode.repo, cwd=elsewhere)
    state_file, run, d, ddir, res = launch_in_herdr(opencode, monkeypatch, adapter="opencode", model=MODEL,
                                                    effort="high", reads=[BUSY])
    calls = _calls(state_file)
    assert not [c for c in calls if c[:2] in (["agent", "start"], ["agent", "prompt"])]
    spec = json.loads((ddir / "launch.json").read_text())
    assert spec["fallback_reason"].startswith("pane-mismatch") and "does not belong" in spec["fallback_reason"]


# ------------------------------------------------------------------------- wall cap

def _opencode_dispatch(env, monkeypatch, *, launched_minutes_ago: float, gets=("working",), reads=(BUSY,)):
    """A live pane-hosted opencode dispatch recorded `launched_minutes_ago` minutes ago."""
    state_file = _fake(env, monkeypatch, gets=list(gets), reads=list(reads))
    run, d = _live_dispatch(env, monkeypatch)
    launched = (datetime.now(timezone.utc) - timedelta(minutes=launched_minutes_ago)).isoformat()
    con = env.con()
    try:
        with con:
            con.execute("UPDATE dispatches SET adapter_id='opencode', harness='opencode', model=?, effort='high', "
                        "launcher='herdr', pane_id='w1:p101', launched_at=?, status='running' WHERE id=?",
                        (MODEL, launched, d["id"]))
    finally:
        con.close()
    data = json.loads(state_file.read_text())
    data.update(pane_agents={"w1:p101": "office-d"}, pane_kinds={"w1:p101": "opencode"})
    state_file.write_text(json.dumps(data))
    from office import state
    con = env.con()
    try:
        d = state.get_dispatch(con, d["id"])
    finally:
        con.close()
    return state_file, run, d


def test_the_pane_cap_is_the_profiles_max_minutes_only_where_the_pane_form_asks_for_it(opencode, monkeypatch):
    from office import adapters, dispatch
    monkeypatch.delenv("OFFICE_WORKER_MAX_MINUTES", raising=False)
    for kind in ("worker", "reviewer"):
        assert adapters.profile(adapters.load_all()["opencode"], kind)["interactive"]["wall_cap"] is True
        assert dispatch._pane_wall_cap({"adapter_id": "opencode"}, {"kind": kind}) == 60 * 60
    monkeypatch.setenv("OFFICE_WORKER_MAX_MINUTES", "5")
    assert dispatch._pane_wall_cap({"adapter_id": "opencode"}, {"kind": "worker"}) == 300
    # Other pane agents keep running without a cap.
    for adapter_id in ("claude", "codex", "agy", "pi"):
        assert dispatch._pane_wall_cap({"adapter_id": adapter_id}, {"kind": "worker"}) is None, adapter_id


@pytest.mark.approved
def test_an_opencode_pane_worker_past_max_minutes_is_stopped_and_its_pane_closed(opencode, monkeypatch):
    from office import dispatch
    monkeypatch.delenv("OFFICE_WORKER_MAX_MINUTES", raising=False)
    state_file, run, d = _opencode_dispatch(opencode, monkeypatch, launched_minutes_ago=61)
    monkeypatch.setenv("OFFICE_RECLAIM_GRACE", "0")
    spec = {"kind": "worker", "herdr_agent": "office-d"}
    assert dispatch.watch_herdr_agent(d["id"], spec, poll=0) == (None, "timeout")
    closed = [c for c in _calls(state_file) if c[:2] == ["pane", "close"]]
    assert closed == [["pane", "close", "w1:p101"]]


@pytest.mark.approved
def test_an_opencode_pane_worker_inside_its_cap_keeps_running_until_it_submits(opencode, monkeypatch):
    from office import dispatch
    monkeypatch.delenv("OFFICE_WORKER_MAX_MINUTES", raising=False)
    state_file, run, d = _opencode_dispatch(opencode, monkeypatch, launched_minutes_ago=59)
    spec = {"kind": "worker", "herdr_agent": "office-d"}
    polls = {"n": 0}

    def submitted(con, disp):
        polls["n"] += 1
        return polls["n"] > 3
    monkeypatch.setattr(dispatch, "_submitted", submitted)
    assert dispatch.watch_herdr_agent(d["id"], spec, poll=0) == (0, "success")
    assert not [c for c in _calls(state_file) if c[:2] == ["pane", "close"]]


@pytest.mark.approved
def test_a_pane_reviewer_with_a_finished_reply_is_not_cut_off_by_the_cap(opencode, monkeypatch):
    """The cap only ends what is still running: a reply file already written is the result."""
    from office import dispatch
    state_file, run, d = _opencode_dispatch(opencode, monkeypatch, launched_minutes_ago=600, gets=["idle"],
                                            reads=["> composer empty"])
    out = opencode.tmp / "reply.txt"
    out.write_text("VERDICT: APPROVED\n")
    spec = {"kind": "reviewer", "herdr_agent": "office-d", "output": str(out)}
    assert dispatch.watch_herdr_agent(d["id"], spec, poll=0) == (0, "success")


# ------------------------------------------------------------------- wall classification

@pytest.mark.approved
@pytest.mark.parametrize("wall", WALLS)
def test_a_wall_opencode_prints_in_its_pane_is_classified_from_the_pane_tail(opencode, monkeypatch, wall):
    from office import gates, paths
    state_file, run, d = _opencode_dispatch(opencode, monkeypatch, launched_minutes_ago=1)
    data = json.loads(state_file.read_text())
    data["reads"] = [f"working on it\n{wall}\n\n  ctrl+p commands\n"]
    state_file.write_text(json.dumps(data))
    ddir = paths.run_dir(run["id"]) / "dispatches" / d["id"]
    ddir.mkdir(parents=True, exist_ok=True)
    # The headless log is empty for a pane agent; the wall shows only in the pane tail.
    assert not gates._quota_signature(gates._log_text(d, ddir))
    tail = gates._log_text(d, ddir, pane=True)
    assert gates._quota_signature(tail) and wall in tail
    assert wall in (ddir / "pane-tail.txt").read_text()
    assert not gates._auth_signature(tail)


def test_every_opencode_quota_signature_is_a_quota_wall_and_the_others_are_not(opencode):
    from office import adapters, gates
    sigs = {s["id"]: s for s in adapters.load_all()["opencode"]["failure_signatures"]}
    assert {sigs[i]["pattern"] for i in ("free-usage-limit-reached", "rate-limit-exceeded")} == set(WALLS)
    for i in ("free-usage-limit-reached", "rate-limit-exceeded"):
        assert gates._quota_signature(sigs[i]["pattern"]), i
    for i in ("unknown-variant-fails-resolution", "unknown-model-fails-resolution"):
        assert not gates._quota_signature(sigs[i]["pattern"]), i


# ------------------------------------------------------------------- the session store

def _session_store(home: Path, sessions) -> Path:
    """An opencode.db with the tables and columns Office reads, in the 2.0.29 shape:
    `sessions` is [(id, directory, [(type, created_ms, data_dict), ...])]."""
    db = home / ".local" / "share" / "opencode" / "opencode.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE session_v2 (id text PRIMARY KEY, project_id text NOT NULL, directory text NOT NULL,
                                 time_created integer NOT NULL, time_updated integer NOT NULL);
        CREATE TABLE session_message (id text PRIMARY KEY, session_id text NOT NULL, type text NOT NULL,
                                      seq integer NOT NULL, time_created integer NOT NULL,
                                      time_updated integer NOT NULL, data text NOT NULL);
    """)
    n = 0
    for sid, directory, messages in sessions:
        con.execute("INSERT INTO session_v2 VALUES(?,?,?,?,?)", (sid, "p", str(directory), 1, 1))
        for seq, (kind, created, data) in enumerate(messages):
            n += 1
            con.execute("INSERT INTO session_message VALUES(?,?,?,?,?,?,?)",
                        (f"m{n}", sid, kind, seq, created, created, json.dumps(data)))
    con.commit()
    con.close()
    return db


def _ms(minutes_ago: float = 0) -> int:
    return int((time.time() - minutes_ago * 60) * 1000)


def _prompt(brief: Path) -> dict:
    return {"time": {"created": 1}, "text": f"Read and carry out the brief at {brief} exactly.", "files": []}


def test_the_session_that_was_sent_the_brief_path_is_seen_and_named(opencode, monkeypatch, tmp_path):
    from office import transcripts
    monkeypatch.setenv("XDG_DATA_HOME", str(opencode.home / ".local" / "share"))
    cwd, brief = tmp_path / "wt", tmp_path / "dispatches" / "D1" / "brief.md"
    cwd.mkdir()
    _session_store(opencode.home, [
        ("ses_other", cwd, [("user", _ms(1), {"text": "an unrelated prompt"})]),
        ("ses_mine", cwd, [("user", _ms(1), _prompt(brief)),
                           ("assistant", _ms(), {"content": [{"type": "text", "text": f"VERDICT: PASS {brief}"}]})]),
    ])
    kw = dict(marker=str(brief), cwd=cwd, since=time.time() - 600)
    assert transcripts.prompt_seen("opencode", **kw) is True
    assert transcripts.session_id("opencode", **kw) == "ses_mine"
    # Nothing is read for another harness, and a reply is never taken from the store.
    assert transcripts.prompt_seen("claude", **kw) is False
    assert transcripts.final_reply("opencode", **kw) is None


def test_a_session_that_only_printed_the_path_or_ran_elsewhere_or_is_old_is_not_the_dispatchs(opencode, monkeypatch,
                                                                                            tmp_path):
    from office import transcripts
    monkeypatch.setenv("XDG_DATA_HOME", str(opencode.home / ".local" / "share"))
    cwd, elsewhere = tmp_path / "wt", tmp_path / "other"
    brief = tmp_path / "dispatches" / "D2" / "brief.md"
    cwd.mkdir(), elsewhere.mkdir()
    _session_store(opencode.home, [
        # An assistant or tool message that mentions the path is not a prompt.
        ("ses_tool", cwd, [("assistant", _ms(1), {"content": [{"type": "text", "text": str(brief)}]})]),
        ("ses_dir", elsewhere, [("user", _ms(1), _prompt(brief))]),
        ("ses_old", cwd, [("user", _ms(24 * 60), _prompt(brief))]),
    ])
    kw = dict(marker=str(brief), cwd=cwd, since=time.time() - 600)
    assert transcripts.prompt_seen("opencode", **kw) is False
    assert transcripts.session_id("opencode", **kw) is None


def test_two_sessions_with_the_same_brief_path_name_no_session(opencode, monkeypatch, tmp_path):
    from office import transcripts
    monkeypatch.setenv("XDG_DATA_HOME", str(opencode.home / ".local" / "share"))
    cwd, brief = tmp_path / "wt", tmp_path / "dispatches" / "D3" / "brief.md"
    cwd.mkdir()
    _session_store(opencode.home, [(s, cwd, [("user", _ms(1), _prompt(brief))]) for s in ("ses_a", "ses_b")])
    kw = dict(marker=str(brief), cwd=cwd, since=time.time() - 600)
    assert transcripts.prompt_seen("opencode", **kw) is True
    assert transcripts.session_id("opencode", **kw) is None


@pytest.mark.parametrize("make", ["missing", "empty-file", "another-shape"])
def test_a_missing_or_unrecognised_store_reads_as_no_session(opencode, monkeypatch, tmp_path, make):
    from office import transcripts
    monkeypatch.setenv("XDG_DATA_HOME", str(opencode.home / ".local" / "share"))
    db = opencode.home / ".local" / "share" / "opencode" / "opencode.db"
    if make != "missing":
        db.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(db)
        if make == "another-shape":
            con.execute("CREATE TABLE session (id text)")
        con.commit()
        con.close()
    kw = dict(marker=str(tmp_path / "brief.md"), cwd=tmp_path, since=0)
    assert transcripts.prompt_seen("opencode", **kw) is False
    assert transcripts.session_id("opencode", **kw) is None


def test_the_store_is_opened_read_only(opencode, monkeypatch, tmp_path):
    from office import transcripts
    monkeypatch.setenv("XDG_DATA_HOME", str(opencode.home / ".local" / "share"))
    cwd, brief = tmp_path / "wt", tmp_path / "dispatches" / "D4" / "brief.md"
    cwd.mkdir()
    db = _session_store(opencode.home, [("ses_mine", cwd, [("user", _ms(1), _prompt(brief))])])
    before = (db.read_bytes(), db.stat().st_mtime_ns)
    assert transcripts.prompt_seen("opencode", marker=str(brief), cwd=cwd, since=0)
    assert (db.read_bytes(), db.stat().st_mtime_ns) == before
    assert not db.with_name(db.name + "-journal").exists()


@pytest.mark.approved
def test_the_landing_probe_and_session_record_use_the_store_when_herdr_reports_nothing(opencode, monkeypatch):
    """Herdr reports no session for the pane: the opencode store, not pane text, names it."""
    from office import dispatch, transcripts
    monkeypatch.setenv("XDG_DATA_HOME", str(opencode.home / ".local" / "share"))
    _herdr_describes(monkeypatch, opencode.repo, session=None)
    briefs = {}
    real_seen = transcripts.prompt_seen

    def seen(harness, *, marker, cwd=None, since=None):
        briefs["marker"], briefs["harness"] = marker, harness
        _session_store(opencode.home, [("ses_from_store", cwd, [("user", _ms(0), _prompt(Path(marker)))])])
        return real_seen(harness, marker=marker, cwd=cwd, since=since)
    monkeypatch.setattr(transcripts, "prompt_seen", seen)
    state_file, run, d, ddir, res = launch_in_herdr(opencode, monkeypatch, adapter="opencode", model=MODEL,
                                                    effort="high", reads=[])
    assert res["launcher"] == "herdr", res
    assert briefs["harness"] == "opencode" and briefs["marker"] == str(ddir / "brief.md")
    assert res["prompt_landed"] is True  # the store saw the prompt; no busy footer was needed
    con = opencode.con()
    try:
        assert con.execute("SELECT session_id FROM dispatches WHERE id=?", (d["id"],)).fetchone()[0] == "ses_from_store"
    finally:
        con.close()
