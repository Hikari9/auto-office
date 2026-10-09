"""#305 B3 and B4: reviewer reply capture and a dispatch that ends with no classification.

A reply read off a terminal survives hard-wrap and scrollback; an empty reply or one that is
only the TUI footer is invalid and never a pass; a reviewer dispatch with no recorded end says
what state it was left in. Also: resubmitting an identical, already reviewed tree reuses the
self-review ledger. Transcripts and pane captures here are synthetic.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import GOOD_ADD, approved_run, task_row
from test_rerun import FAKE_HERDR

SUBMIT_ONLY = [{"write": {"calc.py": GOOD_ADD}, "submit": True}]
from test_convergence_contract import APPROVED, _gates, _q, _scope, _start

def hard_wrap(text: str, width: int) -> str:
    """What a terminal does to a long line: cut it at the pane width, wherever that falls."""
    out = []
    for line in text.splitlines():
        out += [line[i:i + width] for i in range(0, len(line), width)] or [""]
    return "\n".join(out) + "\n"


REPLY = ("VERDICT: CHANGES_REQUIRED\n"
         "FINDING F1 | material | calc.py:2 | add subtracts instead of adding when both operands are negative, "
         "which breaks the sum invariant the callers rely on | return a + b\n")
WRAPPED = hard_wrap(REPLY, 60)


# ------------------------------------------------------------------ parsing a wrapped or empty capture


def test_a_finding_the_terminal_wrapped_mid_word_is_rejoined():
    from office import review_parse
    assert WRAPPED.count("\n") > REPLY.count("\n")
    p = review_parse.parse(WRAPPED)
    assert p.valid and p.verdict == "CHANGES_REQUIRED", p.errors
    assert p.findings == review_parse.parse(REPLY).findings
    assert p.findings[0]["summary"].endswith("which breaks the sum invariant the callers rely on")
    assert p.findings[0]["action"] == "return a + b"


CONVERGENCE_REPLY = ("VERDICT: RECHECK\n"
                     "FINDING F1 | high | blocking | calc.py:2 | add subtracts instead of adding for negative operands "
                     "| return a + b | owner: T1\n"
                     "NEXT repair F1 in calc.py, then resubmit so the lane recomposes and the same reviewer checks it again\n")


@pytest.mark.parametrize("reply, contract", [(REPLY, None), (CONVERGENCE_REPLY, "convergence-v1")])
def test_a_reply_wrapped_at_any_width_parses_as_the_unwrapped_reply(reply, contract):
    """Whatever the pane width, a capture that keeps its line ends reads exactly as the reply; one
    that trimmed the spaces at the line ends loses none of the words."""
    from office import review_parse
    want = review_parse.parse(reply, contract=contract)
    assert want.valid
    for width in range(40, 200):
        got = review_parse.parse(hard_wrap(reply, width), contract=contract)
        assert got.valid and got.findings == want.findings and got.next_action == want.next_action, width
        trimmed = "\n".join(line.rstrip() for line in hard_wrap(reply, width).split("\n"))
        got = review_parse.parse(trimmed, contract=contract)
        assert got.valid and got.verdict == want.verdict and not got.errors, width

        def shape(findings):
            return [(f["code"], f["severity"], f["location"], re.sub(r"\s+", "", f["summary"]),
                     re.sub(r"\s+", "", f["action"]), f.get("blocking"), f.get("owners")) for f in findings]

        assert shape(got.findings) == shape(want.findings), width
        assert (got.next_action or "").replace(" ", "") == (want.next_action or "").replace(" ", ""), width


def test_a_tui_word_wrapped_finding_is_rejoined_with_a_space():
    from office import review_parse
    text = ("VERDICT: CHANGES_REQUIRED\n"
            "FINDING F1 | material | calc.py:2 | add subtracts instead of adding when both operands are\n"
            "  negative, which breaks the sum invariant the callers rely on | return a + b\n")
    p = review_parse.parse(text)
    assert p.valid and "operands are negative, which" in p.findings[0]["summary"], p.errors


def test_a_wrapped_convergence_reply_keeps_its_blocking_flag_and_next_line():
    from office import review_parse
    reply = ("VERDICT: RECHECK\n"
             "FINDING F1 | high | blocking | calc.py:2 | add subtracts instead of adding for negative operands "
             "| return a + b | owner: T1\n"
             "NEXT repair F1 in calc.py, then resubmit so the lane recomposes and the same reviewer checks it again\n")
    for width in (40, 61, 72):
        p = review_parse.parse(hard_wrap(reply, width), contract="convergence-v1")
        assert p.valid and p.verdict == "RECHECK", (width, p.errors)
        assert p.findings == review_parse.parse(reply, contract="convergence-v1").findings, width
        assert p.findings[0]["blocking"] and p.findings[0]["owners"] == ["T1"]
        assert p.next_action.endswith("same reviewer checks it again"), (width, p.next_action)


def test_an_unwrapped_reply_is_read_exactly_as_before():
    from office import review_parse
    text = ("VERDICT: CHANGES_REQUIRED\nFINDING F1 | material | calc.py:2 | subtracts | fix it\n"
            "FINDING F2 | minor | calc.py:9 | a nit | rename\n")
    p = review_parse.parse(text)
    assert [f["code"] for f in p.findings] == ["F1", "F2"] and p.findings[0]["summary"] == "subtracts"


FOOTERS = [
    "",
    "   \n\n",
    "? for shortcuts\n",
    "────────────────────────────────────────\n>\n  ? for shortcuts\n",
    "› Improve documentation in @filename\n\n  example-model high · 100% left · ~/review\n",
    "ctx: 12k · $0.03\nesc to interrupt\n",
]


@pytest.mark.parametrize("text", FOOTERS)
@pytest.mark.parametrize("contract", [None, "convergence-v1"])
def test_an_empty_or_footer_only_reply_is_invalid_and_never_a_pass(text, contract):
    from office import gates, review_parse
    p = review_parse.parse(gates._last_block(text), contract=contract)
    assert not p.valid and p.verdict is None, p
    assert p.errors[0] in ("empty reply", "reply holds only the terminal footer, no review"), p.errors


def test_the_format_line_echoed_back_is_not_a_verdict():
    from office import review_parse
    for text, contract in (("VERDICT: PASS | CHANGES_REQUIRED | PLAN_DEFECT | BRIEF_DEFECT", None),
                           ("VERDICT: APPROVED | RECHECK | INTAKE_GAP\nNEXT x", "convergence-v1")):
        p = review_parse.parse(text, contract=contract)
        assert not p.valid and p.verdict is None and "format" in p.errors[0], p.errors
    assert review_parse.parse("VERDICT: PASS | nothing else to add").valid


def test_a_wall_the_pane_width_broke_across_lines_is_still_a_quota_wall():
    from office import gates
    wall = gates.reviewer_wall("some output\nYou've hit your usage\nlimit, try again later\n")
    assert wall and wall["kind"] == "quota", wall
    assert gates.reviewer_wall("all good\n") is None
    assert gates.reviewer_wall("Please run /login\n")["kind"] == "auth"
    assert gates.reviewer_wall("Not logged\nin · Please run\n/login\n")["kind"] == "auth"
    assert "quota stall" in gates._failure_reason({}, "claude@1/sonnet@high", wall)


# ------------------------------------------------------------------ scrollback and transcripts


def _closing_message(transcript: Path, cwd: Path, text="Done: the review is in reply.txt."):
    row = {"type": "assistant", "cwd": str(cwd), "message": {"id": "m3", "role": "assistant",
                                                             "content": [{"type": "text", "text": text}]}}
    with transcript.open("a") as fh:
        fh.write(json.dumps(row) + "\n")


def test_a_reviewer_that_closes_after_its_review_still_yields_the_review(env):
    from office import transcripts
    from test_herdr_review_capture import FINAL_CR, _claude_transcript
    cwd, brief = env.tmp / "review", env.tmp / "brief.md"
    cwd.mkdir()
    f = _claude_transcript(env.home, cwd, brief, FINAL_CR)
    _closing_message(f, cwd)
    verdict = re.compile(r"^\W*VERDICT\s*:", re.M)
    assert transcripts.final_reply("claude", marker=str(brief), cwd=cwd, prefer=verdict) == FINAL_CR
    # Without a preference the last message stays the answer.
    assert transcripts.final_reply("claude", marker=str(brief), cwd=cwd).startswith("Done:")


def test_a_supervisor_that_died_and_was_not_reaped_is_not_alive():
    import os
    import signal
    import time
    from office import dispatch
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert dispatch._supervisor_alive(child.pid)
        os.kill(child.pid, signal.SIGKILL)
        deadline = time.time() + 10
        while dispatch._supervisor_alive(child.pid) and time.time() < deadline:
            time.sleep(0.05)
        assert not dispatch._supervisor_alive(child.pid), "a zombie answers signal 0; the wait must still see it gone"
    finally:
        child.kill()
        child.wait()


def test_a_closing_line_never_brings_back_a_verdict_the_reviewer_only_quoted(env):
    from office import transcripts
    from test_herdr_review_capture import _claude_transcript
    cwd, brief = env.tmp / "review", env.tmp / "brief.md"
    cwd.mkdir()
    f = _claude_transcript(env.home, cwd, brief, "Reading the diff.")
    rows = [json.loads(line) for line in f.read_text().splitlines()]
    rows.insert(2, {"type": "assistant", "cwd": str(cwd), "message": {"id": "m0", "role": "assistant", "content": [
        {"type": "text", "text": "The README says:\nVERDICT: APPROVED\nand I am not convinced."}]}})
    rows.append({"type": "assistant", "cwd": str(cwd), "message": {"id": "m9", "role": "assistant", "content": [
        {"type": "text", "text": "Done: the review is in reply.txt."}]}})
    f.write_text("".join(json.dumps(r) + "\n" for r in rows))
    verdict = re.compile(r"^\W*VERDICT\s*:", re.M)
    assert transcripts.final_reply("claude", marker=str(brief), cwd=cwd, prefer=verdict).startswith("Done:")


def test_a_pane_read_the_pane_width_wrapped_is_rejoined_and_unwrapped_is_preferred(monkeypatch):
    from office import dispatch
    wrapped = WRAPPED
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        source = argv[argv.index("--source") + 1] if "--source" in argv else None
        ok = source == "recent" or (source == "recent-unwrapped" and fake_run.unwrapped)
        text = wrapped if source == "recent" else "UNWRAPPED"
        return subprocess.CompletedProcess(argv, 0 if ok else 1, text if ok else "", "")

    monkeypatch.setattr(dispatch.subprocess, "run", fake_run)
    fake_run.unwrapped = False
    text = dispatch._pane_snapshot("office-d1", "p1")
    assert text.strip() == REPLY.strip()
    fake_run.unwrapped = True
    assert dispatch._pane_snapshot("office-d1", "p1") == "UNWRAPPED"
    monkeypatch.setattr(dispatch.shutil, "which", lambda name: "/bin/herdr")
    assert dispatch.pane_text({"id": "D1", "launcher": "herdr", "pane_id": "p1"}) == "UNWRAPPED"
    assert dispatch.pane_text({"id": "D1", "launcher": "sync", "pane_id": None}) == ""


# ------------------------------------------------------------------ end to end: never a pass


@pytest.mark.review_contract("v3.1")
def test_a_wrapped_reply_file_records_the_whole_finding(env):
    approved = {"executor": [{"write": {"calc.py": GOOD_ADD}, "submit": True}], "code_reviewer": [{"reply": WRAPPED}]}
    from conftest import start_inline
    env.trust()
    env.script(**approved)
    start_inline(env)
    env.office("approve", "plan", "--quote", "approved", check=0)
    env.office("dispatch", "T1", check=0)
    rows = _q(env, "SELECT summary FROM findings WHERE task_id='T1'")
    assert rows and rows[0]["summary"].endswith("which breaks the sum invariant the callers rely on"), rows


@pytest.mark.parametrize("text, why", [("? for shortcuts\n────────\n", "terminal footer"), ("", "empty reply")])
def test_a_footer_only_reply_never_approves_a_lane(env, text, why):
    _start(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], convergence_reviewer=[
        {"raw": text, "reply": None}])
    env.office("dispatch", "T1", check=0)
    gates = _gates(env, "convergence_review")
    assert gates and all(g["verdict"] is None and g["review_status"] == "UNAVAILABLE" for g in gates), gates
    assert why in gates[-1]["summary"], gates[-1]["summary"]
    assert _scope(env, "L-T1")["status"] == "unavailable"
    assert task_row(env)["status"] == "accepted", "the task's own checks passed; the lane is what waits"


def test_a_silent_reviewer_whose_narration_mentions_a_quota_is_not_a_quota_stall(env):
    """It printed prose about quota code and no review: that is a reviewer with nothing usable, not a wall."""
    narration = {"raw": "I read the quota docs: they are exhausted of examples.", "reply": None}
    _start(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], convergence_reviewer=[narration])
    env.office("dispatch", "T1", check=0)
    gate = _gates(env, "convergence_review")[-1]
    assert gate["review_status"] == "UNAVAILABLE" and "no usable reply" in gate["summary"], gate["summary"]
    assert "quota stall" not in gate["summary"]
    assert not [d for d in _q(env, "SELECT stall_kind FROM dispatches") if d["stall_kind"]]


# ------------------------------------------------------------------ end to end: never a pass


@pytest.mark.review_contract("v3.1")
@pytest.mark.parametrize("text", ["? for shortcuts\n────────\n", "VERDICT: PASS | CHANGES_REQUIRED"])
def test_v31_a_reply_with_no_review_in_it_never_passes_the_task(env, text):
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], code_reviewer=[
        {"raw": text, "reply": None}])
    env.office("dispatch", "T1", check=0)
    assert task_row(env)["status"] != "accepted"
    assert all(g["verdict"] != "PASS" for g in _gates(env, "code_review"))


# ------------------------------------------------------------------ B4: no recorded classification


def test_a_dispatch_with_no_classification_says_what_state_it_was_left_in():
    from office import gates
    d = {"status": "running", "launcher": "herdr", "pid": 99999999, "terminal_classification": None, "exit_code": None}
    reason = gates._failure_reason(d, "claude@1/sonnet@high", None)
    assert "(None)" not in reason and "exit None" not in reason, reason
    assert "no recorded classification" in reason and "status running" in reason, reason
    assert "launcher herdr" in reason and "supervisor pid 99999999 gone" in reason, reason
    classified = gates._failure_reason({**d, "terminal_classification": "nonzero", "exit_code": 2}, "r", None)
    assert classified == "r: exit 2 (nonzero) with no reply"


def test_a_reviewer_that_never_ends_is_reported_with_its_state_not_exit_none(env, monkeypatch):
    from office import dispatch
    real = dispatch.launch
    monkeypatch.setattr(dispatch, "launch", lambda run, d, kind, *a, **kw: {} if kind in ("reviewer", "vision")
                        else real(run, d, kind, *a, **kw))
    _start(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], convergence_reviewer=[{"reply": APPROVED}])
    env.office("dispatch", "T1", check=0)
    gate = _gates(env, "convergence_review")[-1]
    assert gate["review_status"] == "UNAVAILABLE", gate
    assert "no recorded classification" in gate["summary"] and "status launching" in gate["summary"], gate["summary"]
    assert "None" not in gate["summary"].replace("None qualif", ""), gate["summary"]


def test_a_supervisor_that_dies_without_an_end_is_recorded_lost_with_its_reason(env, monkeypatch):
    from office import dispatch
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": False}])
    env.office("dispatch", "T1", env={"OFFICE_WORKER_LAUNCHER": "external"}, check=0)
    did = task_row(env)["current_dispatch_id"]
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    con = env.con()
    con.execute("UPDATE dispatches SET launcher='herdr', pid=?, status='running' WHERE id=?", (gone.pid, did))
    con.commit()
    log = next(Path(env.state).rglob(f"dispatches/{did}"), None)
    assert log is not None
    (log / "supervisor.log").write_text("Traceback ...\nRuntimeError: herdr socket closed\n")
    monkeypatch.setenv("OFFICE_SUPERVISOR_GRACE", "0")
    ended = dispatch._wait_terminal(did, timeout=30)
    assert ended["terminal"] == "supervisor_lost", ended
    row = _q(env, "SELECT status, terminal_classification FROM dispatches WHERE id=?", (did,))[0]
    assert row == {"status": "failed", "terminal_classification": "supervisor_lost"}
    event = _q(env, "SELECT summary FROM events WHERE kind='dispatch.ended' AND dispatch_id=?", (did,))[-1]["summary"]
    assert "supervisor (pid" in event and "herdr socket closed" in event, event


def _lost_herdr_dispatch(env, monkeypatch, mode):
    from office import dispatch
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": False}])
    env.office("dispatch", "T1", env={"OFFICE_WORKER_LAUNCHER": "external"}, check=0)
    did = task_row(env)["current_dispatch_id"]
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    herdr = env.bin / "herdr"
    herdr.write_text(FAKE_HERDR.format(python=sys.executable))
    herdr.chmod(0o755)
    monkeypatch.setenv("FAKE_HERDR_AGENT", mode)
    con = env.con()
    con.execute("UPDATE dispatches SET launcher='herdr', pid=?, status='running', pane_id='p1' WHERE id=?", (gone.pid, did))
    con.commit()
    ddir = next(Path(env.state).rglob(f"dispatches/{did}"))
    return dispatch, did, ddir


def test_a_lost_supervisor_whose_pane_agent_still_works_is_not_ended(env, monkeypatch):
    dispatch, did, ddir = _lost_herdr_dispatch(env, monkeypatch, "alive")
    d = dispatch.state_dispatch(did)
    assert dispatch.lost_dispatch(did, d) is False
    assert dispatch.state_dispatch(did)["status"] == "running"


def test_a_lost_supervisor_whose_reviewer_wrote_its_reply_ends_as_a_success(env, monkeypatch):
    dispatch, did, ddir = _lost_herdr_dispatch(env, monkeypatch, "alive")
    reply = ddir / "reply.txt"
    reply.write_text("VERDICT: PASS\n")
    (ddir / "launch.json").write_text(json.dumps({"output": str(reply)}))
    assert dispatch.lost_dispatch(did, dispatch.state_dispatch(did)) is True
    row = dispatch.state_dispatch(did)
    assert row["terminal_classification"] == "success" and row["exit_code"] == 0


def test_a_lost_supervisor_says_what_its_own_log_says_and_never_what_the_agent_printed(env, monkeypatch):
    dispatch, did, ddir = _lost_herdr_dispatch(env, monkeypatch, "gone")
    (ddir / "output.log").write_text("export API_KEY=sk-secret-123\n")
    (ddir / "supervisor.log").write_text("Traceback ...\nRuntimeError: herdr socket closed\n")
    assert dispatch.lost_dispatch(did, dispatch.state_dispatch(did)) is True
    event = _q(env, "SELECT summary FROM events WHERE kind='dispatch.ended' AND dispatch_id=?", (did,))[-1]["summary"]
    assert "herdr socket closed" in event and "sk-secret" not in event, event
    assert dispatch.state_dispatch(did)["terminal_classification"] == "supervisor_lost"


def test_a_lost_supervisor_whose_herdr_cannot_be_asked_ends_as_lost(env, monkeypatch):
    dispatch, did, ddir = _lost_herdr_dispatch(env, monkeypatch, "unreachable")
    assert dispatch.lost_dispatch(did, dispatch.state_dispatch(did)) is True
    assert dispatch.state_dispatch(did)["terminal_classification"] == "supervisor_lost"


def test_the_wait_keeps_going_while_a_pane_agent_works_after_its_supervisor_died(env, monkeypatch):
    dispatch, did, ddir = _lost_herdr_dispatch(env, monkeypatch, "alive")
    monkeypatch.setenv("OFFICE_SUPERVISOR_GRACE", "0")
    monkeypatch.setattr(dispatch.time, "sleep", lambda s: None)
    ended = dispatch._wait_terminal(did, timeout=0.3)
    assert ended["terminal"] == "timeout" and dispatch.state_dispatch(did)["status"] == "running"


def test_a_held_reprompt_is_reported_with_the_phrase_the_reviewer_loop_looks_for(env, monkeypatch):
    from office import dispatch, gates, review_parse, state
    approved_run(env, executor=SUBMIT_ONLY, convergence_reviewer=[{"reply": APPROVED}])
    con = env.con()
    run = state.get_run(con, _q(env, "SELECT id FROM runs")[0]["id"])
    monkeypatch.setattr(gates, "_agent_alive", lambda name: True)
    monkeypatch.setattr(dispatch, "submit_prompt", lambda name, prompt, pane=None: "held")
    d = {"id": "D1", "launcher": "herdr", "pane_id": "p1", "role": "code_reviewer", "triple": "x", "task_id": "T1"}
    _, _, attention = gates._reprompt_until_valid(con, run, d, env.tmp, env.tmp / "reply.txt", review_parse.parse(""),
                                                  plan_review=False, visual=False)
    assert gates.HELD_PROMPT in attention, attention


def test_a_bad_grace_setting_does_not_end_the_wait(monkeypatch):
    from office import dispatch
    monkeypatch.setenv("OFFICE_SUPERVISOR_GRACE", "soon")
    assert dispatch._supervisor_grace() == dispatch.SUPERVISOR_GRACE_SECONDS


# ------------------------------------------------------------------ an identical, already reviewed tree


LEDGER = """COMMIT {head}
ROUND 1
LENS security reviewed
LENS edge-cases reviewed
LENS platform reviewed
LENS test-strength reviewed
"""
CR = "VERDICT: CHANGES_REQUIRED\nFINDING F1 | material | calc.py:2 | wrong | fix it"


@pytest.mark.review_contract("v3.1")
def test_resubmitting_an_identical_reviewed_tree_reuses_its_self_review_ledger(env):
    from office import gates, paths, preflight, state
    approved_run(env, executor=[{}], code_reviewer=[{"reply": "", "exit": 1}])
    env.office("dispatch", "T1", env={"OFFICE_WORKER_LAUNCHER": "external"}, check=0)
    t = task_row(env)
    con = env.con()
    d = dict(con.execute("SELECT * FROM dispatches WHERE id=?", (t["current_dispatch_id"],)).fetchone())
    wt = Path(d["worktree"])
    (wt / "calc.py").write_text(GOOD_ADD)
    subprocess.run(["git", "-C", str(wt), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(wt), "-c", "user.email=a@b", "-c", "user.name=w", "commit", "-qm", "add"], check=True)
    head = paths.git(wt, "rev-parse", "HEAD")
    (wt / "OFFICE_SELF_REVIEW.md").write_text(LEDGER.format(head=head))
    wenv = {"OFFICE_RUN_ID": d["run_id"], "OFFICE_DISPATCH_ID": d["id"], "OFFICE_TASK_ID": "T1", "OFFICE_ROLE": "executor"}
    env.office("submit", cwd=wt, env=wenv, check=0)
    assert not (wt / "OFFICE_SELF_REVIEW.md").exists(), "submit consumes the ledger"
    first = task_row(env)["current_revision_id"]
    assert gates.ledger_archive_path(state.get_run(con, d["run_id"]), "T1", first).is_file()
    assert task_row(env)["status"] == "blocked", "the reviewer could not finish: nothing is left to fix"

    # The executor is relaunched on the same tree: nothing changed, so nothing is owed again.
    con.execute("UPDATE dispatches SET status='exited', terminal_classification='success', ended_at=started_at "
                "WHERE id=?", (d["id"],))
    con.commit()
    env.office("rerun", "T1", "--fresh", check=0)
    restored = (wt / "OFFICE_SELF_REVIEW.md").read_text()
    assert restored.startswith(f"COMMIT {head}\n") and "LENS test-strength reviewed" in restored
    changed = ["calc.py"]
    stop, fix = preflight.ledger_verdict(wt, state.get_task(con, d["run_id"], "T1"), changed, head)
    assert (stop, fix) == ([], []), (stop, fix)
    events = [e["summary"] for e in _q(env, "SELECT summary FROM events WHERE kind='launch.ledger_reused'")]
    assert events and first in events[0]


@pytest.mark.review_contract("v3.1")
def test_a_fix_round_never_starts_with_the_old_ledger(env):
    """Findings mean the tree will change: the ledger of the reviewed tree would only be stale at submit."""
    from office import paths
    approved_run(env, executor=[{}], code_reviewer=[{"reply": CR}])
    env.office("dispatch", "T1", env={"OFFICE_WORKER_LAUNCHER": "external"}, check=0)
    con = env.con()
    d = dict(con.execute("SELECT * FROM dispatches WHERE id=?", (task_row(env)["current_dispatch_id"],)).fetchone())
    wt = Path(d["worktree"])
    (wt / "calc.py").write_text(GOOD_ADD)
    subprocess.run(["git", "-C", str(wt), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(wt), "-c", "user.email=a@b", "-c", "user.name=w", "commit", "-qm", "add"], check=True)
    (wt / "OFFICE_SELF_REVIEW.md").write_text(LEDGER.format(head=paths.git(wt, "rev-parse", "HEAD")))
    wenv = {"OFFICE_RUN_ID": d["run_id"], "OFFICE_DISPATCH_ID": d["id"], "OFFICE_TASK_ID": "T1", "OFFICE_ROLE": "executor"}
    env.office("submit", cwd=wt, env=wenv, check=0)
    assert task_row(env)["status"] == "changes_required"
    con.execute("UPDATE dispatches SET status='exited', terminal_classification='success', ended_at=started_at "
                "WHERE id=?", (d["id"],))
    con.commit()
    env.office("rerun", "T1", "--fresh", check=0)
    assert not (wt / "OFFICE_SELF_REVIEW.md").exists()


@pytest.mark.review_contract("v3.1")
def test_a_changed_tree_or_a_ledger_the_executor_wrote_is_never_replaced(env):
    from office import gates, paths, state
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}}], code_reviewer=[{"reply": CR}])
    env.office("dispatch", "T1", env={"OFFICE_WORKER_LAUNCHER": "external"}, check=0)
    con = env.con()
    d = dict(con.execute("SELECT * FROM dispatches WHERE id=?", (task_row(env)["current_dispatch_id"],)).fetchone())
    wt = Path(d["worktree"])
    run = state.get_run(con, d["run_id"])
    task = state.get_task(con, run["id"], "T1")
    (wt / "calc.py").write_text(GOOD_ADD)
    subprocess.run(["git", "-C", str(wt), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(wt), "-c", "user.email=a@b", "-c", "user.name=w", "commit", "-qm", "add"], check=True)
    head = paths.git(wt, "rev-parse", "HEAD")
    tree = paths.git(wt, "rev-parse", "HEAD^{tree}")
    con.execute("INSERT INTO revisions(id, run_id, task_id, seq, dispatch_id, commit_sha, tree_sha, base_commit, "
                "requirements_version, plan_version, applied_version, env_fingerprint, operation_id, status, created_at) "
                "VALUES('R9', ?, 'T1', 9, ?, ?, ?, ?, 1, 1, 1, 'x', 'op9', 'superseded', '2026-01-01T00:00:00+00:00')",
                (run["id"], d["id"], head, tree, d["base_commit"]))
    con.commit()
    archive = gates.ledger_archive_path(run, "T1", "R9")
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_text(LEDGER.format(head="0" * 40))
    # An executor's own ledger stays.
    (wt / "OFFICE_SELF_REVIEW.md").write_text("mine\n")
    assert gates.restore_ledger(con, run, task, wt) is None and (wt / "OFFICE_SELF_REVIEW.md").read_text() == "mine\n"
    (wt / "OFFICE_SELF_REVIEW.md").unlink()
    # A dirty tree is not the reviewed tree.
    (wt / "calc.py").write_text(GOOD_ADD + "# edit\n")
    assert gates.restore_ledger(con, run, task, wt) is None
    (wt / "calc.py").write_text(GOOD_ADD)
    assert gates.restore_ledger(con, run, task, wt) == "R9"
    assert (wt / "OFFICE_SELF_REVIEW.md").read_text().startswith(f"COMMIT {head}\n")
    # New content: no revision holds this tree.
    (wt / "OFFICE_SELF_REVIEW.md").unlink()
    (wt / "calc.py").write_text(GOOD_ADD + "# edit\n")
    subprocess.run(["git", "-C", str(wt), "-c", "user.email=a@b", "-c", "user.name=w", "commit", "-qam", "edit"], check=True)
    assert gates.restore_ledger(con, run, task, wt) is None


@pytest.mark.review_contract("v3.1")
def test_an_archived_ledger_is_never_overwritten_and_an_unreadable_one_is_skipped(env):
    from office import gates, paths, state
    approved_run(env, executor=[{}], code_reviewer=[{"reply": CR}])
    env.office("dispatch", "T1", env={"OFFICE_WORKER_LAUNCHER": "external"}, check=0)
    con = env.con()
    d = dict(con.execute("SELECT * FROM dispatches WHERE id=?", (task_row(env)["current_dispatch_id"],)).fetchone())
    wt = Path(d["worktree"])
    run = state.get_run(con, d["run_id"])
    task = state.get_task(con, run["id"], "T1")
    (wt / "OFFICE_SELF_REVIEW.md").write_text("first draft\n")
    gates.archive_ledger(run, task, "R1", d)
    (wt / "OFFICE_SELF_REVIEW.md").write_text("a new draft for a different tree\n")
    gates.archive_ledger(run, task, "R1", d)
    assert gates.ledger_archive_path(run, "T1", "R1").read_text() == "first draft\n", "a re-plan keeps what vouched"
    (wt / "OFFICE_SELF_REVIEW.md").unlink()
    tree = paths.git(wt, "rev-parse", "HEAD^{tree}")
    con.execute("INSERT INTO revisions(id, run_id, task_id, seq, dispatch_id, commit_sha, tree_sha, base_commit, "
                "requirements_version, plan_version, applied_version, env_fingerprint, operation_id, status, created_at) "
                "VALUES('R1', ?, 'T1', 1, ?, 'c', ?, ?, 1, 1, 1, 'x', 'op1', 'superseded', '2026-01-01T00:00:00+00:00')",
                (run["id"], d["id"], tree, d["base_commit"]))
    con.commit()
    gates.ledger_archive_path(run, "T1", "R1").write_text(LEDGER.format(head="0" * 40))
    assert gates.restore_ledger(con, run, task, wt) == "R1", "the control: a readable archive of this tree is restored"
    (wt / "OFFICE_SELF_REVIEW.md").unlink()
    gates.ledger_archive_path(run, "T1", "R1").write_bytes(b"\xff\xfe not utf-8")
    assert gates.restore_ledger(con, run, task, wt) is None and not (wt / "OFFICE_SELF_REVIEW.md").exists()


def test_a_reviewers_next_paragraph_is_not_glued_to_its_longest_line():
    from office import review_parse
    reply = REPLY + "Detail: the sum is subtracted when both operands are negative.\n"
    p = review_parse.parse(reply)
    assert p.valid and p.findings == review_parse.parse(REPLY).findings
    convergence = CONVERGENCE_REPLY + "Notes: add() was not checked with floats.\n"
    assert review_parse.parse(convergence, contract="convergence-v1").next_action.endswith("checks it again")


def test_a_review_that_opens_with_a_sentence_still_beats_a_closing_line(env):
    from office import transcripts
    from test_herdr_review_capture import _claude_transcript
    cwd, brief = env.tmp / "review", env.tmp / "brief.md"
    cwd.mkdir()
    review = "Review complete.\n\nVERDICT: PASS\nNEXT land it"
    f = _claude_transcript(env.home, cwd, brief, review)
    _closing_message(f, cwd, "Done, reply written.")
    verdict = re.compile(r"^\W*VERDICT\s*:", re.M)
    assert transcripts.final_reply("claude", marker=str(brief), cwd=cwd, prefer=verdict) == review


@pytest.mark.parametrize("name", ["test_reviewer_reliability.py", "test_review_reroute.py"])
def test_no_test_in_these_suites_is_defined_twice(name):
    """A second definition shadows the first, which then never runs."""
    import ast
    tree = ast.parse((Path(__file__).parent / name).read_text())
    names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]
    assert len(names) == len(set(names)), sorted({n for n in names if names.count(n) > 1})
