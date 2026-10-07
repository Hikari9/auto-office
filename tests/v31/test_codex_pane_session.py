"""Herdr-launched codex agents keep a resumable session id (#406, after #313 / PR #325).

In a herdr pane, `herdr agent start` reported no session id and `herdr agent
get` no `agent_session.value` for codex 0.160.1, while codex itself printed
`session id: <uuid>` in its startup banner. Every codex pane was then
unresumable: `office rerun --resume` was refused, and a plan RECHECK silently
went to a fresh reviewer instead of the same one.

Office now also reads the id from the banner (the adapter's
`session.output_pattern`, inside the banner box, below this dispatch's own
setup line, before any prompt is sent) and from the session's own transcript
(the rollout whose user prompt names this dispatch's brief). When no source
has it, nothing is invented, and a reviewer that cannot be resumed is
announced to the orchestrator before the fresh reviewer starts.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from conftest import PLAN_ONE, start_inline
from test_herdr_agent_launch import BUSY, _calls, _fake, _live_dispatch

SID = "019ff3a4-fbe0-73c0-bf5f-727665d09f20"
OLD = "00000000-1111-2222-3333-444444444444"


def codex_banner(sid: str, width: int = 58) -> str:
    """The startup box codex 0.160.1 draws in an interactive pane."""
    rows = [">_ OpenAI Codex (v0.160.1)", "", "model:      gpt-5.5 high   /model to change", "directory:  ~/repo",
            f"session id: {sid}"]
    return "\n".join(["╭" + "─" * width + "╮", *[f"│ {r.ljust(width - 2)} │" for r in rows], "╰" + "─" * width + "╯",
                      "", "› Improve documentation in @filename", "", "  100% context left · ? for shortcuts"])


def _pattern():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from office import adapters
    return adapters.session_output_pattern(adapters.load_all()["codex"])


# ---------------------------------------------------------------- the banner grammar

def test_the_codex_banner_box_yields_its_session_id():
    from office import dispatch
    screen = "$ . /r/D1/agent.env && cd /repo && touch /r/D1/shell-ready\n" + codex_banner(SID)
    assert dispatch.banner_session(screen, _pattern(), after="/r/D1/shell-ready") == SID


def test_a_banner_left_by_an_earlier_agent_in_a_reused_pane_is_not_read():
    from office import dispatch
    screen = codex_banner(OLD) + "\n$ . /r/D2/agent.env && cd /repo && touch /r/D2/shell-ready\n"
    assert dispatch.banner_session(screen, _pattern(), after="/r/D2/shell-ready") is None
    both = screen + codex_banner(SID)
    assert dispatch.banner_session(both, _pattern(), after="/r/D2/shell-ready") == SID


def test_a_setup_line_wrapped_at_the_pane_width_still_bounds_the_screen():
    from office import dispatch
    marker = "/very/long/state/home/runs/abcdef/dispatches/Dabc123/shell-ready"
    line = f"$ . /x/agent.env && cd /repo && touch {marker}"
    wrapped = "\n".join(line[i:i + 30] for i in range(0, len(line), 30))
    assert dispatch.banner_session(wrapped + "\n" + codex_banner(SID), _pattern(), after=marker) == SID


@pytest.mark.parametrize("screen", [
    f"session id: {SID}",  # not inside a header block
    "╭" + "─" * 20 + "╮\n│ model: m │\n╰" + "─" * 20 + "╯\n› write `session id: " + SID + "` somewhere",  # agent text
    codex_banner(SID) + "\n" + codex_banner(OLD),  # two ids: ambiguous
    codex_banner("not-a-uuid"),
])
def test_anything_but_one_id_inside_the_banner_is_not_taken(screen):
    from office import dispatch
    assert dispatch.banner_session(screen, _pattern()) is None


def test_the_headless_stream_and_the_pane_banner_share_one_grammar(monkeypatch):
    """The sniffer of `codex exec` output reads a boxed header the same way."""
    from office import adapters, dispatch
    seen = []
    monkeypatch.setattr(dispatch, "_record_session", lambda run, d, session, *, source: seen.append((session, source)))
    sniffer = dispatch._SessionSniffer({}, {"id": "D1"}, adapters.load_all()["codex"])
    sniffer.feed(codex_banner(SID).encode() + b"\n")
    assert seen == [(SID, "output")]


# ---------------------------------------------------------------- herdr pane launch

def _session(env, did):
    con = env.con()
    try:
        return con.execute("SELECT session_id FROM dispatches WHERE id=?", (did,)).fetchone()[0]
    finally:
        con.close()


def _codex_launch(env, monkeypatch, role="worker", **settings):
    """T1's dispatch, routed to codex (no id assigned at launch), launched into a fake herdr pane."""
    state_file = _fake(env, monkeypatch, reads=[BUSY])
    run, d = _live_dispatch(env, monkeypatch)
    for key, value in {"HERDR_ENV": "1", "HERDR_PANE_ID": "w1:pQ", "OFFICE_LAUNCHER": "herdr", "OFFICE_HERDR_LAND_TIMEOUT": "0",
                       "OFFICE_HERDR_KEY_DELAY": "0", "OFFICE_HERDR_SESSION_WAIT": "0", **settings}.items():
        monkeypatch.setenv(key, value)
    from office import db, dispatch, paths, state
    con = env.con()
    with db.transaction(con):
        con.execute("UPDATE dispatches SET adapter_id='codex', harness='codex', model='gpt-5.5', effort='high', "
                    "session_id=NULL, role=?, kind=? WHERE id=?",
                    ("code_reviewer" if role == "reviewer" else "executor",) * 2 + (d["id"],))
    d = state.get_dispatch(con, d["id"])
    con.close()
    monkeypatch.setattr(dispatch.frontdoor, "current_argv", lambda: (["true"], {}))
    ddir = paths.run_dir(run["id"]) / "dispatches" / d["id"]
    ddir.mkdir(parents=True, exist_ok=True)
    (ddir / "brief.md").write_text("ROLE executor\n")
    cwd = ddir if role == "reviewer" else env.repo
    res = dispatch.launch(run, d, role, ddir, cwd=cwd, output=ddir / "reply.txt" if role == "reviewer" else None)
    return state_file, run, d, ddir, res


@pytest.mark.approved
@pytest.mark.parametrize("role", ["worker", "reviewer"])
def test_a_codex_pane_records_the_id_from_its_banner_when_herdr_reports_none(env, monkeypatch, role):
    state_file, run, d, ddir, res = _codex_launch(env, monkeypatch, role, FAKE_HERDR_BANNER=codex_banner(SID))
    assert res["launcher"] == "herdr" and res["prompt_landed"] is True
    calls = _calls(state_file)
    assert any(c[:2] == ["agent", "get"] for c in calls), "herdr is still asked first"
    read = next(i for i, c in enumerate(calls) if c[:2] == ["pane", "read"])
    prompt = next(i for i, c in enumerate(calls) if c[:2] in (["agent", "prompt"], ["pane", "send-text"]))
    assert read < prompt, "the banner is read before any prompt reaches the agent"
    assert _session(env, d["id"]) == SID
    ledger = [json.loads(line) for line in (ddir.parents[1] / "panes.jsonl").read_text().splitlines()]
    assert ledger[-1]["session_id"] == SID
    con = env.con()
    assert not con.execute("SELECT 1 FROM events WHERE kind='launch.session'").fetchone()


@pytest.mark.approved
def test_an_old_banner_in_a_reused_pane_is_never_recorded(env, monkeypatch):
    state_file, run, d, ddir, res = _codex_launch(env, monkeypatch, FAKE_HERDR_PRELUDE=codex_banner(OLD))
    assert _session(env, d["id"]) is None
    con = env.con()
    (event,) = con.execute("SELECT summary FROM events WHERE kind='launch.session'").fetchall()
    assert "no session id was reported by herdr, the agent's startup banner or its transcript" in event[0]
    assert "office rerun --resume will refuse it" in event[0]


RERUN_HERDR = r'''#!{python}
import json, sys
args = sys.argv[1:]
if args[:2] == ["agent", "get"]:
    sys.stderr.write(json.dumps({{"error": {{"code": "agent_not_found"}}}})); sys.exit(1)
print(json.dumps({{"result": {{}}}}))
'''


def _end_and_close(env, did):
    """The agent ended and its pane was closed: only runs.db remembers the session."""
    herdr = env.bin / "herdr"
    herdr.write_text(RERUN_HERDR.format(python=sys.executable))
    con = env.con()
    con.execute("UPDATE dispatches SET status='exited', terminal_classification='success', ended_at=?, "
                "pane_closed_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(),) * 2 + (did,))
    con.execute("UPDATE tasks SET status='changes_required' WHERE id='T1'")
    con.commit()
    con.close()


@pytest.mark.approved
def test_rerun_resume_continues_a_banner_captured_codex_executor_after_its_pane_closed(env, monkeypatch):
    state_file, run, d, ddir, res = _codex_launch(env, monkeypatch, FAKE_HERDR_BANNER=codex_banner(SID))
    _end_and_close(env, d["id"])
    _, out = env.office("inspect", "task", "T1", env={"OFFICE_WORKER_LAUNCHER": "external"})
    assert f"session {SID}" in out and "unavailable" not in out, out
    from office import rerun, state
    con = env.con()
    res = rerun.rerun(con, state.get_run(con, run["id"]), "T1", resume=True, fresh=False)
    assert f"resuming {d['id']}" in res.lines[0], res.lines
    job = con.execute("SELECT payload_json FROM outbox WHERE kind='launch_agent' ORDER BY created_at DESC LIMIT 1").fetchone()
    resume = json.loads(job["payload_json"])["resume"]
    assert resume["session_id"] == SID and resume["argv"][-2:] == ["resume", SID] and resume["herdr_kind"] == "codex"
    assert "--model" not in resume["argv"] and resume["argv"][resume["argv"].index("-m") + 1] == "gpt-5.5"
    child = con.execute("SELECT session_id, resumed_from FROM dispatches WHERE resumed_from=?", (d["id"],)).fetchone()
    assert child["session_id"] == SID  # a resumed session keeps its id: it can be resumed again


@pytest.mark.approved
def test_the_same_codex_reviewer_is_resumed_for_a_recheck(env, monkeypatch):
    state_file, run, d, ddir, res = _codex_launch(env, monkeypatch, "reviewer", FAKE_HERDR_BANNER=codex_banner(SID))
    _end_and_close(env, d["id"])
    from office import dispatch, gates, state
    con = env.con()
    spec, route = gates._reviewer_resume(con, state.get_run(con, run["id"]), d["id"], "reviewer", ddir)
    assert spec is not None and spec["session_id"] == SID and spec["argv"][-2:] == ["resume", SID], spec
    assert spec["herdr_kind"] == "codex" and "--sandbox" in spec["argv"]
    assert not con.execute("SELECT 1 FROM events WHERE kind='review.resume_fallback'").fetchone()


def _rollout(env, sid: str, cwd: Path, user_text: str, *, role: str = "user") -> Path:
    day = datetime.now(timezone.utc)
    folder = env.home / ".codex" / "sessions" / f"{day:%Y}" / f"{day:%m}" / f"{day:%d}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"rollout-{day:%Y-%m-%dT%H-%M-%S}-{sid}.jsonl"
    rows = [{"type": "session_meta", "payload": {"id": sid, "cwd": str(cwd), "cli_version": "0.160.1"}},
            {"type": "response_item", "payload": {"type": "message", "role": role,
                                                  "content": [{"type": "input_text", "text": user_text}]}}]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


@pytest.mark.approved
def test_an_id_herdr_and_the_banner_missed_is_backfilled_from_the_sessions_own_transcript(env, monkeypatch):
    state_file, run, d, ddir, res = _codex_launch(env, monkeypatch)
    assert _session(env, d["id"]) is None
    _end_and_close(env, d["id"])
    brief = str(ddir / "brief.md")
    _rollout(env, OLD, env.repo, f"Read the brief at {brief}", role="assistant")  # only quoted by an agent: no
    _rollout(env, OLD, env.tmp, f"Read and carry out the brief at {brief}")  # another cwd: no
    from office import rerun, state
    con = env.con()
    with pytest.raises(rerun.Refused) as err:
        rerun.rerun(con, state.get_run(con, run["id"]), "T1", resume=True, fresh=False)
    assert "no stored harness session id" in err.value.message and _session(env, d["id"]) is None
    _rollout(env, SID, env.repo, f"Read and carry out the brief at {brief} exactly.")
    rerun.rerun(con, state.get_run(con, run["id"]), "T1", resume=True, fresh=False)
    assert _session(env, d["id"]) == SID
    job = con.execute("SELECT payload_json FROM outbox WHERE kind='launch_agent' ORDER BY created_at DESC LIMIT 1").fetchone()
    assert json.loads(job["payload_json"])["resume"]["argv"][-2:] == ["resume", SID]


# ---------------------------------------------------------------- a fallback is announced before it runs

RECHECK = "VERDICT: RECHECK\nFINDING P1 | high | blocking | T1 | add has no contract | add one\nNEXT fix it"
APPROVED = "VERDICT: APPROVED\nNEXT proceed"


def test_a_recheck_that_cannot_resume_its_reviewer_says_so_before_the_fresh_reviewer_starts(env, monkeypatch):
    env.trust()
    env.script(plan_reviewer=[{"reply": RECHECK}, {"reply": APPROVED}])
    start_inline(env, plan=PLAN_ONE, gear="express")
    env.office("approve", "plan", "--quote", "go", check=0)
    con = env.con()
    first = con.execute("SELECT reviewer_dispatch_id, route FROM gates WHERE kind='plan_review'").fetchone()
    # The RECHECK line itself already says the next round is not the same reviewer session.
    (recheck,) = con.execute("SELECT summary FROM events WHERE kind='plan.recheck'").fetchall()
    assert f"Reviewer {first['reviewer_dispatch_id']} cannot be resumed" in recheck[0], recheck[0]
    code, data = env.ojson("status")
    assert "a fresh reviewer on the same route reviews it" in data["next"] and "the same reviewer reviews" not in data["next"]
    from office import dispatch
    real, seen = dispatch.launch, []

    def launch(run, d, kind, *a, **kw):
        if d.get("role") == "plan_reviewer":
            c = env.con()
            seen.append([dict(r) for r in c.execute("SELECT audience, summary, payload_json FROM events "
                                                    "WHERE kind='review.resume_fallback'")])
            c.close()
        return real(run, d, kind, *a, **kw)

    monkeypatch.setattr(dispatch, "launch", launch)
    env.write_plan(PLAN_ONE.replace("- calc.add(2, 3) == 5", "- calc.add(2, 3) == 5\n- calc.add(0, 0) == 0"))
    code, out = env.office("amend", "plan", "--contract", "--", "add a contract")
    assert code == 0, out
    assert seen, "the recheck reviewer launched"
    (notice,) = seen[0]
    assert notice["audience"] == "orchestrator"
    assert re.search(rf"Reviewer {first['reviewer_dispatch_id']} cannot be resumed: .+\. The recheck continues in a "
                     rf"fresh session on its route {re.escape(first['route'])}", notice["summary"]), notice
    assert json.loads(notice["payload_json"])["parent"] == first["reviewer_dispatch_id"]
    second = con.execute("SELECT route, verdict FROM gates WHERE kind='plan_review' ORDER BY created_at DESC").fetchone()
    assert second["route"] == first["route"] and second["verdict"] == "APPROVED"
    # Announced once, not once per place that noticed it.
    assert con.execute("SELECT COUNT(*) FROM events WHERE kind='review.resume_fallback'").fetchone()[0] == 1


@pytest.mark.approved
def test_a_recheck_inside_the_ingest_transaction_backfills_through_that_transaction(env, monkeypatch):
    """The RECHECK line is written while ingest holds the write lock: a transcript
    backfill there must record through that same connection, never wait on it."""
    import time
    state_file, run, d, ddir, res = _codex_launch(env, monkeypatch, "reviewer")
    assert _session(env, d["id"]) is None
    _end_and_close(env, d["id"])
    _rollout(env, SID, ddir, f"Read and carry out the review brief at {ddir / 'brief.md'} exactly.")
    from office import db, gates, state
    con = env.con()
    started = time.time()
    with db.transaction(con):
        line = gates.recheck_continuity(con, state.get_run(con, run["id"]), d["id"])
    assert time.time() - started < 5
    assert line == f"the same reviewer ({d['id']}) reviews it", line
    assert _session(env, d["id"]) == SID
