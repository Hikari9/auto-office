"""The run on a display-only herdr-projects board card (docs/board-card.md).

A fake `herdr-projects` records every call and keeps its cards in a JSON file. Nothing here
ever reaches a real board: HOME points into the test's own folder and HERDR_* are stripped.
"""
import json
import os
import sys
import threading

import pytest

from conftest import start_inline

FAKE_BOARD = r'''#!{python}
import json, os, sys
path = os.environ["FAKE_BOARD_STATE"]
st = json.load(open(path)) if os.path.exists(path) else {{"calls": [], "cards": {{}}, "fail": [], "n": 0}}
argv = sys.argv[1:]
st["calls"].append(argv)
def save():
    json.dump(st, open(path, "w"))
def flag(name):
    return argv[argv.index(name) + 1] if name in argv else None
cmd = argv[0] if argv else ""
if cmd in st.get("fail", []):
    save(); sys.stderr.write(cmd + " refused\n"); sys.exit(1)
if cmd == "add":
    st["n"] += 1
    cid = "repo/%d" % st["n"]
    st["cards"][cid] = {{"id": cid, "title": argv[1], "description": flag("--desc") or "", "stage": "todo",
                         "msg": "", "progress": None, "kind": None}}
    save(); print(json.dumps({{"id": cid, "title": argv[1]}}))
elif cmd == "list":
    q = flag("--query") or ""
    save(); print(json.dumps([c for c in st["cards"].values() if q in c["title"]]))
elif cmd == "details":
    save(); print(json.dumps({{"task": st["cards"][argv[1]]}}))
elif cmd == "describe":
    st["cards"][argv[1]]["description"] = argv[2].strip()
    save(); print(argv[1] + ": updated (description)")
elif cmd == "report":
    c = st["cards"][argv[1]]
    c["stage"] = flag("--state"); c["msg"] = flag("--msg") or ""; c["kind"] = flag("--kind")
    c["progress"] = int(flag("--progress")) if flag("--progress") else None
    save(); print(argv[1] + ": updated")
else:
    save(); sys.exit(2)
'''


@pytest.fixture
def board(env, monkeypatch):
    """A fake board CLI at HERDR_PROJECTS_BIN; HOME is the test's own, so ~/.herdr-projects is empty."""
    monkeypatch.setenv("HOME", str(env.home))
    cli = env.bin / "herdr-projects"
    cli.write_text(FAKE_BOARD.format(python=sys.executable))
    cli.chmod(0o755)
    state_file = env.tmp / "board.json"
    monkeypatch.setenv("FAKE_BOARD_STATE", str(state_file))
    monkeypatch.setenv("HERDR_PROJECTS_BIN", str(cli))

    class Board:
        def state(self):
            return json.loads(state_file.read_text()) if state_file.exists() else {"calls": [], "cards": {}}

        def calls(self, cmd=None):
            return [c for c in self.state()["calls"] if cmd is None or c[0] == cmd]

        def card(self, cid):
            return self.state()["cards"][cid]

        def seed(self, cid, description=""):
            st = self.state()
            st.setdefault("fail", [])
            st.setdefault("n", 0)
            st["cards"][cid] = {"id": cid, "title": "rico-mode card", "description": description, "stage": "todo",
                                "msg": "", "progress": None, "kind": None}
            state_file.write_text(json.dumps(st))

        def fail(self, *cmds):
            st = self.state()
            st.setdefault("n", 0)
            st["fail"] = list(cmds)
            state_file.write_text(json.dumps(st))

    return Board()


def _run(env):
    con = env.con()
    try:
        row = con.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()
        from office import state
        return state.get_run(con, row["id"])
    finally:
        con.close()


def test_no_key_and_no_board_cli_never_calls_the_board(env, board, monkeypatch):
    monkeypatch.setenv("HERDR_PROJECTS_BIN", str(env.tmp / "no-such-herdr-projects"))
    start_inline(env)
    env.office("approve", "plan", "--quote", "approved", check=0)
    env.office("status", check=0)
    code, out = env.office("card", "status")
    assert code == 0 and "no board here" in out
    env.office("close", "--abandon", "test over", check=0)
    assert board.calls() == []
    assert "card" not in _run(env)["landing"]


def test_given_key_links_and_shows_plan_approval_as_needs_you(env, board, monkeypatch):
    board.seed("repo/7")
    monkeypatch.setenv("OFFICE_BOARD_CARD", "repo/7")
    start_inline(env)
    run = _run(env)
    assert run["landing"]["card"]["id"] == "repo/7"
    assert run["landing"]["card"]["created_by"] == "given"
    assert board.calls("add") == []
    card = board.card("repo/7")
    assert card["stage"] == "blocked" and card["kind"] == "permission"
    assert card["msg"] == "Plan p1 ready for your approval · answer in rico-mode"
    assert "Plan p1 (awaiting your approval):" in card["description"]
    assert "### T1: Implement add" in card["description"]  # the plan's own text


def test_flag_overrides_env_key(env, board, monkeypatch):
    board.seed("repo/8")
    monkeypatch.setenv("OFFICE_BOARD_CARD", "repo/7")
    start_inline(env, extra=("--card", "repo/8"))
    assert _run(env)["landing"]["card"]["id"] == "repo/8"


def test_authorized_plan_then_executing_with_progress_then_abandon(env, board, monkeypatch):
    board.seed("repo/7")
    monkeypatch.setenv("OFFICE_BOARD_CARD", "repo/7")
    start_inline(env)
    env.office("approve", "plan", "--quote", "go ahead", check=0)
    card = board.card("repo/7")
    assert card["stage"] == "working"
    assert card["msg"].startswith("Executing: 0/1 accepted")
    assert card["progress"] == 15
    assert 'authorized by user' in card["description"] and '"go ahead"' in card["description"]
    env.office("close", "--abandon", "not needed", check=0)
    card = board.card("repo/7")
    assert card["stage"] == "error" and card["msg"] == "Abandoned: not needed"


def test_unchanged_state_sends_nothing_again(env, board, monkeypatch):
    board.seed("repo/7")
    monkeypatch.setenv("OFFICE_BOARD_CARD", "repo/7")
    start_inline(env)
    before = (len(board.calls("describe")), len(board.calls("report")))
    env.office("card", "sync", check=0)
    env.office("status", check=0)
    assert (len(board.calls("describe")), len(board.calls("report"))) == before


def test_rico_mode_text_and_card_edits_are_kept(env, board, monkeypatch):
    board.seed("repo/7", description="Brief from rico-mode: fix the login page")
    monkeypatch.setenv("OFFICE_BOARD_CARD", "repo/7")
    start_inline(env)
    desc = board.card("repo/7")["description"]
    assert "Kept from the card:" in desc and "Brief from rico-mode: fix the login page" in desc
    # Rico edits the card; the next publish keeps his line.
    st = board.state()
    st["cards"]["repo/7"]["description"] = desc + "\nRico: please also check mobile"
    (env.tmp / "board.json").write_text(json.dumps(st))
    env.office("approve", "plan", "--quote", "approved", check=0)
    desc = board.card("repo/7")["description"]
    assert "Rico: please also check mobile" in desc
    assert "Brief from rico-mode: fix the login page" in desc


@pytest.mark.parametrize("herdr", [False, True], ids=["started-by-hand", "inside-herdr"])
def test_own_card_whenever_the_board_is_installed_is_created_once(env, board, monkeypatch, herdr):
    """Every run shows on the board: a run started by hand outside herdr gets its card too."""
    if herdr:
        monkeypatch.setenv("HERDR_ENV", "1")
    start_inline(env)
    adds = board.calls("add")
    assert len(adds) == 1 and "--delegate" not in adds[0]
    run = _run(env)
    assert adds[0][1].startswith(f"Office {run['id'][:8]}:")
    assert run["landing"]["card"] == {**run["landing"]["card"], "id": "repo/1", "created_by": "office"}
    env.office("card", "link", "--create", check=0)
    assert len(board.calls("add")) == 1


def test_lost_link_adopts_the_existing_card(env, board, monkeypatch):
    start_inline(env)
    run = _run(env)
    # The DB lost the link (as after a crash between add and the write): link --create adopts the card.
    con = env.con()
    try:
        from office import db, state
        with db.transaction(con):
            landing = dict(run["landing"])
            landing.pop("card")
            state.update_run(con, run["id"], landing=landing)
    finally:
        con.close()
    code, out = env.office("card", "link", "--create")
    assert code == 0, out
    assert "repo/1 (adopted)" in out
    assert len(board.calls("add")) == 1


def test_concurrent_creates_add_one_card(env, board, monkeypatch):
    start_inline(env, extra=("--no-card",))
    run = _run(env)
    from office import board_card
    cli = os.environ["HERDR_PROJECTS_BIN"]
    results = []

    def go():
        con = env.con()
        try:
            results.append(board_card.create(con, run["id"], cli)[0]["id"])
        finally:
            con.close()
    threads = [threading.Thread(target=go) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(board.calls("add")) == 1
    assert set(results) == {"repo/1"}


@pytest.mark.parametrize("how", ["flag", "env", "config"])
def test_opt_outs_add_no_card(env, board, monkeypatch, how):
    extra = ()
    if how == "flag":
        extra = ("--no-card",)
    elif how == "env":
        monkeypatch.setenv("OFFICE_BOARD_CARD", "off")
    else:
        (env.tmp / "user-config.yaml").write_text("board:\n  card: 'off'\n")
    start_inline(env, extra=extra)
    assert board.calls("add") == []
    assert "card" not in _run(env)["landing"]


def test_given_key_wins_over_creation(env, board, monkeypatch):
    board.seed("repo/9")
    monkeypatch.setenv("OFFICE_BOARD_CARD", "repo/9")
    start_inline(env)
    assert board.calls("add") == []
    assert _run(env)["landing"]["card"]["id"] == "repo/9"


def test_board_failure_never_fails_the_run(env, board, monkeypatch):
    board.seed("repo/7")
    board.fail("describe", "report", "details")
    monkeypatch.setenv("OFFICE_BOARD_CARD", "repo/7")
    start_inline(env)
    code, out = env.office("approve", "plan", "--quote", "approved")
    assert code == 0, out
    con = env.con()
    try:
        kinds = [r[0] for r in con.execute("SELECT kind FROM events WHERE kind='card.publish_failed'")]
    finally:
        con.close()
    assert len(kinds) == 1  # one event per distinct error, not one per publish
    code, out = env.office("card", "status")
    assert code == 0 and "last error: details exited 1" in out


def _seed_reviews_and_routes(env, run_id):
    from office import db, state
    con = env.con()
    try:
        run = state.get_run(con, run_id)
        with db.transaction(con):
            con.execute("INSERT INTO dispatches(id, run_id, role, kind, task_id, triple, status) VALUES"
                        "('Dx1', ?, 'executor', 'executor', 'T1', 'claude@2/claude-sonnet-5-5@high', 'exited'),"
                        "('Dx2', ?, 'code_reviewer', 'reviewer', 'T1', 'claude@2/opus@low', 'exited')",
                        (run_id, run_id))
            con.execute("INSERT INTO gates(id, run_id, subject, task_id, kind, input_key, status, verdict, round, route, "
                        "created_at) VALUES('Gr1', ?, 'T1', 'T1', 'convergence_review', 'k1', 'done', 'RECHECK', 1, "
                        "'codex@0/luna@high', '2026-11-12T00:00:01'),"
                        "('Gr2', ?, 'T1', 'T1', 'convergence_review', 'k2', 'done', 'APPROVED', 2, "
                        "'claude@2/opus@low', '2026-11-12T00:00:02')", (run_id, run_id))
            con.execute("INSERT INTO findings(id, run_id, gate_id, severity, code, summary, state, created_at) VALUES"
                        "('f1', ?, 'Gr1', 'high', 'F1', 'Null archive flag counts as eligible', 'resolved', 'a'),"
                        "('f2', ?, 'Gr1', 'medium', 'F2', 'Wraps on desktop', 'open', 'b')", (run_id, run_id))
            state.emit(con, run, "route.fallback", "T1 code_reviewer fell back from codex@0/luna@high to "
                       "opencode@1/deepseek@max: codex@0/luna@high: exit None (nonzero) with no reply",
                       task_id="T1", payload={"before": "codex@0/luna@high", "after": "opencode@1/deepseek@max",
                                              "reason": "usage_limit", "stage": "code_reviewer", "gate": "Gr2"})
            state.emit(con, run, "route.fallback", "T1 code_reviewer fell back from opencode@1/deepseek@max to "
                       "claude@2/opus@low: auth", task_id="T1",
                       payload={"before": "opencode@1/deepseek@max", "after": "claude@2/opus@low",
                                "reason": "auth", "stage": "code_reviewer", "gate": "Gr2"})
            state.emit(con, run, "route.fallback", "T1 fell back from claude@2/claude-sonnet-5-5@high to "
                       "codex@0/luna@high as Dx3 after a runtime failure (exit 1)", task_id="T1")
    finally:
        con.close()


def test_card_shows_routes_fallback_chains_and_review_rounds(env, board, monkeypatch):
    board.seed("repo/7")
    monkeypatch.setenv("OFFICE_BOARD_CARD", "repo/7")
    start_inline(env)
    env.office("approve", "plan", "--quote", "approved", check=0)
    _seed_reviews_and_routes(env, _run(env)["id"])
    env.office("card", "sync", check=0)
    desc = board.card("repo/7")["description"]
    assert "  T1 executor: claude/claude-sonnet-5-5@high (exited)" in desc
    assert "  T1 code_reviewer: claude/opus@low (exited)" in desc
    assert ("T1 code_reviewer: codex/luna@high → opencode/deepseek@max (usage_limit) → claude/opus@low (auth)"
            in desc)
    assert "T1: claude/claude-sonnet-5-5@high → codex/luna@high (exit 1)" in desc
    assert "  convergence_review T1 r1: RECHECK by codex/luna@high · 1 high, 1 medium" in desc
    assert "    - [high] F1 Null archive flag counts as eligible (resolved)" in desc
    assert "  convergence_review T1 r2: APPROVED by claude/opus@low" in desc
    card = board.card("repo/7")
    assert "review T1 r2: APPROVED" in card["msg"]


def test_long_findings_are_trimmed_oldest_first(env, board, monkeypatch):
    from office import board_card
    board.seed("repo/7")
    monkeypatch.setenv("OFFICE_BOARD_CARD", "repo/7")
    monkeypatch.setattr(board_card, "MAX_DESCRIPTION", 5000)
    start_inline(env)
    env.office("approve", "plan", "--quote", "approved", check=0)
    run_id = _run(env)["id"]
    _seed_reviews_and_routes(env, run_id)
    from office import db
    con = env.con()
    try:
        with db.transaction(con):
            for i in range(60):
                con.execute("INSERT INTO findings(id, run_id, gate_id, severity, code, summary, state, created_at) "
                            "VALUES(?, ?, 'Gr1', 'low', ?, ?, 'open', ?)",
                            (f"x{i}", run_id, f"X{i}", "an old finding with a long explanation " * 3, f"c{i:03}"))
            con.execute("INSERT INTO findings(id, run_id, gate_id, severity, code, summary, state, created_at) "
                        "VALUES('new', ?, 'Gr2', 'low', 'N1', 'the newest finding', 'open', 'z')", (run_id,))
    finally:
        con.close()
    env.office("card", "sync", check=0)
    desc = board.card("repo/7")["description"]
    assert len(desc) <= 5000
    assert "the newest finding" in desc
    assert "an old finding" not in desc
    assert "more finding line(s) in" in desc


def test_card_status_without_a_card(env, board):
    start_inline(env, extra=("--no-card",))
    code, out = env.office("card", "status")
    assert code == 0 and "no board card" in out
    code, out = env.office("card", "sync")
    assert code != 0 and "has no board card" in out


@pytest.mark.parametrize("nxt, kind, reason", [
    ('ask the user the plan questions (native question tool), then office amend plan --contract -- "<answers>"',
     "question", "Plan questions need your answers"),
    ('ask the user (native question tool) for authorization of r1, then office approve plan --quote "<user\'s words>"',
     "permission", "Plan p3 ready for your approval"),
    ('new authority entry X2 (deploy prod) needs authorization: ask the user (native question tool), then office approve '
     'X2 --quote "<words>"', "permission", "X2 (deploy prod) needs your authorization"),
    ("plan defect PD1 blocks T2; trace it to the requirement or assumption behind it. ... ask the user", "question",
     "Plan defect PD1 needs your call"),
    ("revise the plan: INTAKE_GAP needs office amend requirements --quote", "question",
     "Intake gap: a decision only you can make"),
    ("ask the user (native question tool): merge | preview deploy | merge + prod | stop; then office land --merge",
     "permission", "ask the user (native question tool): merge | preview deploy | merge + prod | stop"),
])
def test_human_only_steps_map_to_a_blocked_reason(nxt, kind, reason):
    from office import board_card, root_wake
    assert root_wake._human_only(nxt)
    assert board_card._human_reason({"plan_version": 3}, nxt) == (kind, reason)


def test_closed_run_shows_done(env, board, monkeypatch):
    board.seed("repo/7")
    monkeypatch.setenv("OFFICE_BOARD_CARD", "repo/7")
    start_inline(env)
    run = _run(env)
    from office import board_card, db, state
    con = env.con()
    try:
        with db.transaction(con):
            state.update_run(con, run["id"], phase="closed", landing={**run["landing"], "handoff": "work/7-x"})
            state.emit(con, run, "run.closed", "run closed")
        board_card.publish_terminal(con, run["id"])
    finally:
        con.close()
    card = board.card("repo/7")
    assert card["stage"] == "done" and card["msg"] == "Done: work/7-x"


def test_fallback_triples_drop_the_adapter_version():
    from office import board_card
    assert board_card._fmt_triple("claude@2/claude-opus-5-5@low") == "claude/claude-opus-5-5@low"
    assert board_card._fmt_triple("codex@0/luna@high") == "codex/luna@high"
    assert board_card._fmt_triple(None) == "unrecorded"


def _exe(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return str(path)


def test_cli_lookup_order_outside_pytest(tmp_path, monkeypatch):
    from office import board_card
    for k in ("HERDR_PROJECTS_BIN", "HERDR_PROJECTS_DATA_DIR", "PYTEST_CURRENT_TEST"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", str(tmp_path / "pathbin"))
    assert board_card.find_cli() is None
    on_path = _exe(tmp_path / "pathbin" / "herdr-projects")
    assert board_card.find_cli() == on_path
    home = _exe(tmp_path / "home" / ".herdr-projects" / "data" / "bin" / "herdr-projects")
    assert board_card.find_cli() == home
    monkeypatch.setenv("HERDR_PROJECTS_DATA_DIR", str(tmp_path / "data"))
    data = _exe(tmp_path / "data" / "bin" / "herdr-projects")
    assert board_card.find_cli() == data
    monkeypatch.setenv("HERDR_PROJECTS_BIN", _exe(tmp_path / "explicit"))
    assert board_card.find_cli() == str(tmp_path / "explicit")
    assert board_card.board_present() == str(tmp_path / "explicit")  # no HERDR_ENV needed


def test_under_pytest_only_an_explicit_cli_counts(tmp_path, monkeypatch):
    """The suite keeps the real HOME: its default board must never be found from a test."""
    from office import board_card
    monkeypatch.delenv("HERDR_PROJECTS_BIN", raising=False)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "x")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _exe(tmp_path / "home" / ".herdr-projects" / "data" / "bin" / "herdr-projects")
    monkeypatch.setenv("PATH", str(tmp_path / "pathbin"))
    _exe(tmp_path / "pathbin" / "herdr-projects")
    assert board_card.find_cli() is None
    monkeypatch.setenv("HERDR_PROJECTS_BIN", _exe(tmp_path / "explicit"))
    assert board_card.find_cli() == str(tmp_path / "explicit")
