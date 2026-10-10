"""#510: a harness held on an interactive startup prompt keeps its pane until the orchestrator answers."""
from __future__ import annotations

import json

import pytest

import test_herdr_agent_launch as hal
from office import db, startup, state
from office.state import OfficeError, Refused, Usage

UPDATE = ("Update available · 0.162.0 → 0.162.1\n"
          "› 1. Update now (runs `sh -c 'curl -fsSL https://chatgpt.com/codex/install.sh | sh'`)\n"
          "  2. Skip\n  3. Skip until next version\n  enter continue · esc skip")
WHATSNEW = "What's new in 0.162\n  • faster startup\n  press enter to continue"

# Codex on its "Update available" screen: herdr registers the agent, sees it blocked and gives up.
BRANCH = r'''elif os.environ.get("FAKE_HERDR_UPDATE") and args[:2] == ["agent", "start"]:
    code = 1
    result = {{"error": {{"code": os.environ.get("FAKE_HERDR_UPDATE_CODE", "agent_not_ready"),
                          "message": "agent is blocked during startup"}}}}
    data.setdefault("pane_agents", {{}})[args[args.index("--pane") + 1]] = args[2]
    data["update"] = True
elif args[:2] == ["pane", "get"] and os.environ.get("FAKE_HERDR_PANE_GONE") and data.get("update_reads", 0) >= 2:
    json.dump(data, open(state, "w"))
    print(json.dumps({{"error": {{"code": "pane_not_found"}}}}))
    sys.exit(1)
elif args[:2] == ["pane", "read"] and data.get("update"):
    data["update_reads"] = data.get("update_reads", 0) + 1
    print(os.environ["FAKE_HERDR_WHATSNEW"] if data.get("second") else os.environ["FAKE_HERDR_UPDATE_SCREEN"])
    json.dump(data, open(state, "w"))
    sys.exit(0)
elif args[:2] == ["pane", "send-keys"] and data.get("update"):
    key = args[3]
    data.setdefault("update_keys", []).append(key)
    if key == "1":
        data["ran_update"] = True
    elif data.get("second") and key == "Enter":
        data["update"] = False
    elif key in ("2", "3", "esc") and not data.get("second"):
        if os.environ.get("FAKE_HERDR_THEN_WHATSNEW"):
            data["second"] = True
        else:
            data["update"] = False
elif args[:2] == ["agent", "get"] and data.get("update"):
    result = {{"agent": {{"name": args[2], "agent_status": "blocked", "launch_pending": True}}}}
'''


@pytest.fixture
def update_herdr(monkeypatch):
    anchor = 'elif args[:2] == ["agent", "start"] and os.environ.get("FAKE_HERDR_TRUST_DIALOG"):'
    monkeypatch.setattr(hal, "FAKE_HERDR", hal.FAKE_HERDR.replace(anchor, BRANCH + anchor, 1))
    for k, v in {"FAKE_HERDR_UPDATE": "1", "FAKE_HERDR_UPDATE_SCREEN": UPDATE, "FAKE_HERDR_WHATSNEW": WHATSNEW,
                 "OFFICE_STARTUP_PROMPT_WAIT": "30", "OFFICE_STARTUP_PROMPT_POLL": "0.01",
                 "OFFICE_STARTUP_PROMPT_SETTLE": "0"}.items():
        monkeypatch.setenv(k, v)


def _open_row():
    con = db.connect()
    try:
        r = con.execute("SELECT * FROM startup_prompts WHERE state='waiting' ORDER BY created_at DESC LIMIT 1").fetchone()
        return dict(r) if r else None
    finally:
        con.close()


def _rows():
    con = db.connect()
    try:
        return [dict(r) for r in con.execute("SELECT * FROM startup_prompts ORDER BY created_at")]
    finally:
        con.close()


def _answer(row, **kw):
    con = db.connect()
    try:
        run = state.get_run(con, row["run_id"])
        d = state.get_dispatch(con, row["dispatch_id"])
        return startup.answer(con, run, d, keys=kw.get("keys"), choice=kw.get("choice"), expect=kw.get("expect"))
    finally:
        con.close()


def _on_poll(monkeypatch, *steps):
    """Run one step per launcher poll while a prompt is open (the orchestrator's turns)."""
    pending = list(steps)

    def tick(_seconds):
        row = _open_row()
        if pending and row is not None:
            pending.pop(0)(row)
    monkeypatch.setattr(startup, "_sleep", tick)
    return pending


def _brief_prompts(state_file):
    return [c for c in hal._calls(state_file) if c[:2] in (["agent", "prompt"], ["pane", "send-text"])
            and "brief.md" in " ".join(c)]


@pytest.mark.approved
def test_update_prompt_is_held_answered_with_esc_and_the_brief_lands_once(env, monkeypatch, update_herdr):
    seen = {}

    def answer_from_cli(row):
        seen["row"] = row
        con = db.connect()
        try:
            seen["status"] = [ln for ln in startup.recorded(con, state.get_run(con, row["run_id"]))]
        finally:
            con.close()
        code, out = env.office("answer", row["dispatch_id"], "--keys", "esc", "--expect", row["fingerprint"])
        assert code == 0, out
    _on_poll(monkeypatch, answer_from_cli)
    state_file, run, d, ddir, res = hal.launch_in_herdr(env, monkeypatch, gets=["idle"], reads=[hal.BUSY],
                                                        adapter="codex", model="gpt-5.5", effort="high")
    assert res["launcher"] == "herdr" and res["prompt_landed"] is True, res
    data = json.loads(state_file.read_text())
    assert data["update_keys"] == ["esc"] and not data.get("ran_update")
    assert len(_brief_prompts(state_file)) == 1
    row = seen["row"]
    assert row["screen"] == "'Update available' prompt" and row["pane_id"] and row["harness"] == "codex"
    assert json.loads(row["options_json"])[1] == {"n": 2, "label": "Skip"}
    assert "Update available" in open(row["snapshot_path"]).read()
    line = seen["status"][0]
    assert "[startup codex]" in line and f"--expect {row['fingerprint']}" in line and "user's decision" in line
    assert [r["state"] for r in _rows()] == ["resolved"]
    assert hal._launch_events(env, run) == []
    assert json.loads((ddir / "launch.json").read_text())["startup_prompt"]["resolution"].startswith("answered (esc)")


@pytest.mark.approved
def test_herdr_startup_timeout_on_a_prompt_is_held_too(env, monkeypatch, update_herdr):
    monkeypatch.setenv("FAKE_HERDR_UPDATE_CODE", "timeout")
    _on_poll(monkeypatch, lambda row: _answer(row, choice=3, expect=row["fingerprint"]))
    state_file, run, d, ddir, res = hal.launch_in_herdr(env, monkeypatch, gets=["idle"], reads=[hal.BUSY],
                                                        adapter="codex", model="gpt-5.5")
    assert res["launcher"] == "herdr" and res["prompt_landed"] is True
    assert json.loads(state_file.read_text())["update_keys"] == ["3"]


@pytest.mark.approved
def test_status_and_wait_report_the_blocker_once(env, monkeypatch, update_herdr):
    reports = []

    def look(row):
        con = db.connect()
        try:
            run = state.get_run(con, row["run_id"])
            from office import guide, questions
            reports.append(startup.report(con, run))
            reports.append(startup.report(con, run))
            reports.append(guide.status(con, run).lines)
            reports.append(questions.recorded(con, run))
        finally:
            con.close()
    _on_poll(monkeypatch, look, lambda row: _answer(row, choice=2, expect=row["fingerprint"]))
    hal.launch_in_herdr(env, monkeypatch, gets=["idle"], reads=[hal.BUSY], adapter="codex", model="gpt-5.5")
    (fresh1, all1), (fresh2, all2), status_lines, recorded = reports
    assert len(fresh1) == 1 and fresh2 == [] and all1 == all2
    assert any("question:" in ln and "[startup" in ln for ln in status_lines), status_lines
    assert recorded == all1


@pytest.mark.approved
def test_stale_fingerprint_bad_option_and_bad_keys_are_refused_without_input(env, monkeypatch, update_herdr):
    errors = []

    def bad(row):
        for kw in ({"keys": "esc", "expect": "000000000000"}, {"choice": 7}, {"keys": "ctrl-c"},
                   {"keys": "esc", "choice": 2}):
            try:
                _answer(row, **kw)
            except (Refused, Usage) as exc:
                errors.append(exc.category)
    _on_poll(monkeypatch, bad, lambda row: _answer(row, keys="esc"))
    state_file, *_ = hal.launch_in_herdr(env, monkeypatch, gets=["idle"], reads=[hal.BUSY], adapter="codex", model="gpt-5.5")
    assert errors == ["stale-answer", "no-option", "startup-keys", "startup-answer-usage"]
    assert json.loads(state_file.read_text())["update_keys"] == ["esc"]


@pytest.mark.approved
def test_concurrent_second_answer_is_refused(env, monkeypatch, update_herdr):
    errors = []
    real = startup.subprocess.run

    def racing_run(argv, **kw):
        if argv[:3] == ["herdr", "pane", "send-keys"] and not errors:
            # A second orchestrator answers while the first is pressing keys.
            try:
                _answer(_rows()[-1], keys="2")
            except Refused as exc:
                errors.append(exc.category)
        return real(argv, **kw)
    monkeypatch.setattr(startup.subprocess, "run", racing_run)
    _on_poll(monkeypatch, lambda row: _answer(row, keys="esc"))
    state_file, *_ = hal.launch_in_herdr(env, monkeypatch, gets=["idle"], reads=[hal.BUSY], adapter="codex", model="gpt-5.5")
    assert errors == ["startup-answer-pending"]
    assert json.loads(state_file.read_text())["update_keys"] == ["esc"]


@pytest.mark.approved
def test_an_answer_that_opens_another_screen_is_rerecorded_and_old_answers_go_stale(env, monkeypatch, update_herdr):
    monkeypatch.setenv("FAKE_HERDR_THEN_WHATSNEW", "1")
    seen, errors = [], []

    def second(row):
        seen.append(row)
        try:
            _answer(row, keys="esc", expect=seen[0]["fingerprint"])
        except Refused as exc:
            errors.append(exc.category)
        _answer(row, keys="enter", expect=row["fingerprint"])
    _on_poll(monkeypatch, lambda row: (seen.append(row), _answer(row, keys="esc")), second)
    state_file, run, d, ddir, res = hal.launch_in_herdr(env, monkeypatch, gets=["idle"], reads=[hal.BUSY],
                                                        adapter="codex", model="gpt-5.5")
    assert res["launcher"] == "herdr" and res["prompt_landed"] is True
    assert seen[1]["round"] == 2 and seen[1]["fingerprint"] != seen[0]["fingerprint"]
    assert seen[1]["screen"] == startup.UNRECOGNIZED and errors == ["stale-answer"]
    assert json.loads(state_file.read_text())["update_keys"] == ["esc", "Enter"]
    assert len(_brief_prompts(state_file)) == 1


@pytest.mark.approved
def test_unanswered_prompt_expires_into_an_attributed_headless_fallback(env, monkeypatch, update_herdr):
    monkeypatch.setenv("OFFICE_STARTUP_PROMPT_WAIT", "0.05")
    monkeypatch.setattr(startup, "_sleep", lambda s: __import__("time").sleep(0.02))
    state_file, run, d, ddir, res = hal.launch_in_herdr(env, monkeypatch, reads=[hal.BUSY], adapter="codex", model="gpt-5.5")
    assert res["launcher"] == "process-fallback"
    assert not json.loads(state_file.read_text()).get("update_keys")  # nothing pressed on the user's behalf
    assert [r["state"] for r in _rows()] == ["expired"]
    events = hal._launch_events(env, run)
    assert len(events) == 1 and "startup prompt SP-" in events[0] and "'Update available' prompt" in events[0]
    assert "unanswered within" in events[0]
    spec = json.loads((ddir / "launch.json").read_text())
    assert spec["startup_prompt"]["resolution"].startswith("unanswered within")
    assert "startup prompt" in spec["fallback_reason"]


@pytest.mark.approved
def test_no_wait_configured_records_the_prompt_and_falls_back_without_input(env, monkeypatch, update_herdr):
    monkeypatch.setenv("OFFICE_STARTUP_PROMPT_WAIT", "0")
    state_file, run, d, ddir, res = hal.launch_in_herdr(env, monkeypatch, reads=[hal.BUSY], adapter="codex", model="gpt-5.5")
    assert res["launcher"] == "process-fallback"
    assert not any(c[:2] == ["pane", "send-keys"] for c in hal._calls(state_file))
    assert [r["state"] for r in _rows()] == ["expired"]


@pytest.mark.approved
def test_closed_pane_ends_the_hold(env, monkeypatch, update_herdr):
    monkeypatch.setenv("FAKE_HERDR_PANE_GONE", "1")
    monkeypatch.setattr(startup, "_sleep", lambda s: None)
    state_file, run, d, ddir, res = hal.launch_in_herdr(env, monkeypatch, reads=[hal.BUSY], adapter="codex", model="gpt-5.5")
    assert res["launcher"] == "process-fallback"
    assert [r["state"] for r in _rows()] == ["pane_closed"]


@pytest.mark.approved
def test_a_dispatch_ended_during_the_hold_is_not_relaunched_headless(env, monkeypatch, update_herdr):
    def revoke(row):
        con = db.connect()
        try:
            with db.transaction(con):
                con.execute("UPDATE dispatches SET status='revoked', ended_at=? WHERE id=?", ("2026-10-10T00:00:00+00:00",
                                                                                          row["dispatch_id"]))
        finally:
            con.close()
    _on_poll(monkeypatch, revoke, lambda row: None)
    state_file, run, d, ddir, res = hal.launch_in_herdr(env, monkeypatch, reads=[hal.BUSY], adapter="codex", model="gpt-5.5")
    assert res.get("cancelled") is True and res["launcher"] == "herdr"
    assert [r["state"] for r in _rows()] == ["cancelled"]
    assert not json.loads(state_file.read_text()).get("update_keys")


def test_dead_launcher_abandons_the_prompt_and_answers_are_refused(env, monkeypatch):
    hal._fake(env, monkeypatch)
    run, d = hal._live_dispatch(env, monkeypatch)
    con = env.con()
    try:
        with db.transaction(con):
            con.execute("INSERT INTO startup_prompts(id, run_id, dispatch_id, pane_id, agent, harness, screen, fingerprint, "
                        "options_json, snapshot_path, state, launcher_pid, launcher_identity, created_at, updated_at, "
                        "expires_at) VALUES('SP-dead', ?, ?, 'w1:p9', 'a', 'codex', 's', 'f', '[]', NULL, 'waiting', "
                        "999999, 'host:999999@Thu Jan  1 00:00:00 1970', '2026-10-10T00:00:00+00:00', "
                        "'2026-10-10T00:00:00+00:00', '2099-01-01T00:00:00+00:00')", (run["id"], d["id"]))
        with pytest.raises(OfficeError) as exc:
            from office import questions
            questions.answer(con, state.get_run(con, run["id"]), d["id"], "", keys="esc")
        assert exc.value.category == "no-startup-prompt"
        assert con.execute("SELECT state FROM startup_prompts WHERE id='SP-dead'").fetchone()[0] == "abandoned"
    finally:
        con.close()


def test_fingerprint_ignores_the_selection_cursor_and_options_parse():
    moved = UPDATE.replace("› 1.", "  1.").replace("  2. Skip", "› 2. Skip")
    assert startup.fingerprint(UPDATE) == startup.fingerprint(moved)
    assert startup.fingerprint(UPDATE) != startup.fingerprint(WHATSNEW)
    assert [o["n"] for o in startup.options(UPDATE)] == [1, 2, 3]
