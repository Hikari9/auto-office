"""Every dispatch records its harness session id so `office rerun --resume` and
`amend --reviewer same` can resume it (#200 follow-up).

Where the harness accepts an assigned id (claude --session-id) Office assigns it
at launch, for every role, before the agent starts. Otherwise the id arrives
from a worker hook payload or from herdr and is backfilled once. Fixtures stand
in for the harnesses: nothing here starts a live agent.
"""
from __future__ import annotations

import io
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """A runs.db in tmp holding one run, plus a repo whose `.office/active` names it."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    monkeypatch.setenv("OFFICE_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("OFFICE_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # hooks read ~/.office/hooks.off
    from office import db, state, version
    version.current.cache_clear()
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".office" / "active").mkdir(parents=True)
    (repo / ".office" / "active" / "R1").write_text("executing\tgoal\n")
    con = db.connect()
    con.execute("INSERT INTO runs(id, office_version, phase, created_at) VALUES('R1', ?, 'executing', 'now')",
                (version.current(),))

    class Ledger:
        pass

    led = Ledger()
    led.con, led.repo, led.tmp = con, repo, tmp_path
    led.run = state.get_run(con, "R1")
    led.count = 0

    def dispatch(**cols):
        led.count += 1
        row = {"id": f"D{led.count}", "run_id": "R1", "role": "executor", "kind": "executor", "task_id": "T1",
               "harness": "claude", "adapter_id": "claude", "model": "m", "effort": "high", "status": "running",
               "started_at": "now", **cols}
        con.execute(f"INSERT INTO dispatches({', '.join(row)}) VALUES({', '.join('?' * len(row))})", tuple(row.values()))
        return row["id"]

    led.dispatch = dispatch
    led.session = lambda did: con.execute("SELECT session_id FROM dispatches WHERE id=?", (did,)).fetchone()[0]
    led.events = lambda kind: [dict(r) for r in con.execute("SELECT * FROM events WHERE kind=?", (kind,))]
    yield led
    con.close()


def _hook(ledger, monkeypatch, dispatch_id, session, *, harness="claude", event="PostToolUse"):
    """One worker hook event for `dispatch_id`, as the harness would pipe it."""
    from office import hooks
    monkeypatch.setenv("OFFICE_RUN_ID", "R1")
    monkeypatch.setenv("OFFICE_DISPATCH_ID", dispatch_id)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": session, "cwd": str(ledger.repo)})))
    return hooks.main([event, "--harness", harness])


def _sync_launch(ledger, monkeypatch, on_supervisor=None):
    """Sync launches run `office _supervise` in the foreground; `on_supervisor(did)`
    stands in for it (the moment the agent would start), and any other
    subprocess runs for real."""
    from office import dispatch as dispatch_mod
    real = subprocess.run

    def run(cmd, *args, **kw):
        if isinstance(cmd, list) and "_supervise" in cmd:
            if on_supervisor:
                on_supervisor(cmd[-1])
            return subprocess.CompletedProcess(cmd, 0)
        return real(cmd, *args, **kw)

    monkeypatch.setenv("OFFICE_LAUNCHER", "sync")
    monkeypatch.setattr(dispatch_mod.frontdoor, "current_argv", lambda: (["office"], {}))
    monkeypatch.setattr(dispatch_mod.subprocess, "run", run)
    # tests run the supervisor in-process (conftest); stand in for that the same way
    monkeypatch.setattr(dispatch_mod, "_supervise_in_process",
                        lambda did, cwd, extra: on_supervisor(did) if on_supervisor else None)
    return dispatch_mod


def _row(ledger, did):
    return dict(ledger.con.execute("SELECT * FROM dispatches WHERE id=?", (did,)).fetchone())


# ---------------------------------------------------------------- adapter seeds

def test_seed_adapters_declare_how_each_harness_yields_a_session_id():
    from office import adapters
    seeds = adapters.load_all()
    claude, codex, agy = (adapters.session_spec(seeds[h]) for h in ("claude", "codex", "agy"))
    assert claude["id"] == "assigned" and claude["assign_arg"] == ["--session-id", "{session_id}"]
    assert codex["id"] == "detected" and set(codex["sources"]) == {"hook", "herdr", "output", "transcript"}
    assert adapters.session_output_pattern(seeds["codex"]).match("session id: 019ff3a4-fbe0-73c0-bf5f-727665d09f20")
    assert adapters.session_output_pattern(seeds["claude"]) is None and adapters.session_output_pattern(seeds["agy"]) is None
    assert agy["id"] == "none"
    assert adapters.assigns_session(seeds["claude"]) and not adapters.assigns_session(seeds["codex"])
    assert not adapters.assigns_session(seeds["agy"]) and adapters.session_spec({})["id"] == "none"


@pytest.mark.parametrize("kind", ["worker", "reviewer", "vision"])
def test_claude_argv_carries_the_assigned_id_in_every_profile(kind):
    from office import adapters
    claude = adapters.load_all()["claude"]
    argv, _ = adapters.build_argv(claude, kind, model="m", effort="high", cwd=Path("."), session_id="S-1")
    assert argv[-2:] == ["--session-id", "S-1"]
    plain, _ = adapters.build_argv(claude, kind, model="m", effort="high", cwd=Path("."))
    assert "--session-id" not in plain
    inter, _ = adapters.interactive_argv(claude, kind, model="m", effort="high", cwd=Path("."), session_id="S-1")
    assert inter[-2:] == ["--session-id", "S-1"]


@pytest.mark.parametrize("harness", ["codex", "agy"])
def test_a_harness_without_an_assign_form_is_never_handed_an_id(harness):
    from office import adapters
    adapter = adapters.load_all()[harness]
    argv, _ = adapters.build_argv(adapter, "worker", model="m", effort="high", cwd=Path("."), session_id="S-1")
    assert "S-1" not in argv and "--session-id" not in argv


def test_a_resume_names_the_recorded_id_and_assigns_none():
    from office import adapters
    seeds = adapters.load_all()
    args, _ = adapters.resume_argv(seeds["claude"], "worker", session_id="S-1", model="m", effort="high", cwd=Path("."))
    assert args[-2:] == ["--resume", "S-1"] and "--session-id" not in args
    args, _ = adapters.resume_argv(seeds["codex"], "worker", session_id="S-1", model="m", effort="high", cwd=Path("."))
    assert args[-2:] == ["resume", "S-1"]
    assert adapters.resume_argv(seeds["agy"], "worker", session_id="S-1", model="m", effort="high", cwd=Path(".")) is None


# ---------------------------------------------------------------- assignment at launch

@pytest.mark.parametrize("role,kind", [("planner", "worker"), ("executor", "worker"), ("code_reviewer", "reviewer"),
                                       ("visual_reviewer", "vision")])
def test_claude_launch_records_an_assigned_id_before_the_agent_starts(ledger, monkeypatch, role, kind):
    did = ledger.dispatch(role=role, kind=role)
    seen = {}
    dispatch_mod = _sync_launch(ledger, monkeypatch, lambda started: seen.setdefault("db", ledger.session(started)))
    d = _row(ledger, did)
    ddir = ledger.tmp / "ddir"
    ddir.mkdir(exist_ok=True)
    dispatch_mod.launch(ledger.run, d, kind, ddir, cwd=ledger.tmp)
    assert UUID.match(seen["db"] or ""), seen
    assert d["session_id"] == seen["db"] == ledger.session(did)


def test_a_relaunch_keeps_the_id_the_dispatch_already_has(ledger):
    from office import dispatch as dispatch_mod
    did = ledger.dispatch(session_id="kept-1")
    d = _row(ledger, did)
    dispatch_mod._assign_session(d)
    assert ledger.session(did) == "kept-1"


@pytest.mark.parametrize("harness", ["codex", "agy"])
def test_other_harnesses_get_no_assigned_id(ledger, harness):
    from office import dispatch as dispatch_mod
    did = ledger.dispatch(harness=harness, adapter_id=harness)
    d = _row(ledger, did)
    dispatch_mod._assign_session(d)
    assert ledger.session(did) is None and not d["session_id"]


def test_a_resumed_dispatch_and_a_user_cli_are_not_assigned_an_id(ledger, monkeypatch):
    dispatch_mod = _sync_launch(ledger, monkeypatch)
    (ledger.tmp / "ddir").mkdir()
    resumed = ledger.dispatch(resumed_from="D0")
    dispatch_mod.launch(ledger.run, _row(ledger, resumed), "worker", ledger.tmp / "ddir", cwd=ledger.tmp,
                        resume={"argv": ["--resume", "S"], "herdr_kind": "claude"})
    own = ledger.dispatch()
    dispatch_mod.launch(ledger.run, _row(ledger, own), "worker", ledger.tmp / "ddir", cwd=ledger.tmp, cli="claude --model x")
    assert ledger.session(resumed) is None and ledger.session(own) is None


def test_headless_supervisor_starts_claude_with_the_recorded_id(ledger, monkeypatch):
    from office import dispatch as dispatch_mod
    bindir = ledger.tmp / "bin"
    bindir.mkdir()
    fake = bindir / "claude"
    fake.write_text(f"#!{sys.executable}\nimport json, sys\nsys.stdin.read()\n"
                    f"open({str(ledger.tmp / 'argv.json')!r}, 'w').write(json.dumps(sys.argv[1:]))\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    did = ledger.dispatch(role="code_reviewer", kind="code_reviewer", worktree=str(ledger.tmp))
    ddir = dispatch_mod.paths.run_dir("R1") / "dispatches" / did
    ddir.mkdir(parents=True)
    (ddir / "brief.md").write_text("review it\n")
    _sync_launch(ledger, monkeypatch)
    dispatch_mod.launch(ledger.run, _row(ledger, did), "reviewer", ddir, cwd=ledger.tmp, output=ddir / "reply.txt")
    monkeypatch.setattr(dispatch_mod.jobs, "kick", lambda *a, **k: None)
    assert dispatch_mod.supervise(did) == 0
    argv = json.loads((ledger.tmp / "argv.json").read_text())
    assert UUID.match(ledger.session(did) or "") and argv[argv.index("--session-id") + 1] == ledger.session(did)


CODEX_HEADER = ("OpenAI Codex v0.160.0 (research preview)\n--------\nworkdir: /w\nmodel: m\nprovider: openai\n"
                "session id: {sid}\n--------\nuser\n")


def _fake_codex(ledger, monkeypatch, stderr: str, stdout: str = "done\n") -> None:
    """A `codex` on PATH that prints `stderr` (where `codex exec` puts its header) and `stdout`."""
    bindir = ledger.tmp / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "codex"
    fake.write_text(f"#!{sys.executable}\nimport sys\nsys.stdin.read()\n"
                    f"sys.stderr.write({stderr!r}); sys.stderr.flush()\nsys.stdout.write({stdout!r})\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")


def _supervise_codex(ledger, monkeypatch, role, kind, **cols) -> str:
    from office import dispatch as dispatch_mod
    did = ledger.dispatch(role=role, kind=role, harness="codex", adapter_id="codex", worktree=str(ledger.tmp), **cols)
    ddir = dispatch_mod.paths.run_dir("R1") / "dispatches" / did
    ddir.mkdir(parents=True)
    (ddir / "brief.md").write_text("do it\n")
    _sync_launch(ledger, monkeypatch)
    dispatch_mod.launch(ledger.run, _row(ledger, did), kind, ddir, cwd=ledger.tmp,
                        output=ddir / "reply.txt" if kind == "reviewer" else None)
    monkeypatch.setattr(dispatch_mod.jobs, "kick", lambda *a, **k: None)
    assert dispatch_mod.supervise(did) == 0
    return did


@pytest.mark.parametrize("role,kind", [("executor", "worker"), ("code_reviewer", "reviewer")])
def test_headless_codex_session_id_is_recorded_from_the_cli_header(ledger, monkeypatch, role, kind):
    """A process-launched codex has no hook and no herdr: the id comes from the stream."""
    _fake_codex(ledger, monkeypatch, CODEX_HEADER.format(sid="019ff3a4-fbe0-73c0-bf5f-727665d09f20"))
    did = _supervise_codex(ledger, monkeypatch, role, kind)
    assert ledger.session(did) == "019ff3a4-fbe0-73c0-bf5f-727665d09f20"


SID = "019ff3a4-fbe0-73c0-bf5f-727665d09f20"
SPOOF = "session id: 00000000-0000-0000-0000-000000000bad\n"


def test_a_session_line_after_the_header_is_not_taken_for_an_id(ledger, monkeypatch):
    """The header's own id wins; a prompt echo and an agent reply that repeat the pattern are ignored."""
    _fake_codex(ledger, monkeypatch, CODEX_HEADER.format(sid=SID) + SPOOF, stdout=SPOOF)
    did = _supervise_codex(ledger, monkeypatch, "executor", "worker")
    assert ledger.session(did) == SID and not ledger.events("session.mismatch")


def test_a_stream_with_no_header_block_records_nothing(ledger, monkeypatch):
    """The stream never opens a header block, so a line that looks like the header id is never an id."""
    _fake_codex(ledger, monkeypatch, "warning one\nwarning two\nwarning three\nwarning four\n--------\n" + SPOOF, stdout=SPOOF)
    did = _supervise_codex(ledger, monkeypatch, "executor", "worker")
    assert ledger.session(did) is None


def test_a_spoof_before_the_header_block_opens_is_not_an_id(ledger, monkeypatch):
    _fake_codex(ledger, monkeypatch, SPOOF + CODEX_HEADER.format(sid=SID))
    did = _supervise_codex(ledger, monkeypatch, "executor", "worker")
    assert ledger.session(did) == SID


def test_a_header_id_never_overwrites_a_recorded_one(ledger, monkeypatch):
    _fake_codex(ledger, monkeypatch, CODEX_HEADER.format(sid=SID))
    did = _supervise_codex(ledger, monkeypatch, "executor", "worker", session_id="recorded-sess")
    assert ledger.session(did) == "recorded-sess"
    (event,) = ledger.events("session.mismatch")
    assert json.loads(event["payload_json"]) == {"recorded": "recorded-sess", "seen": SID, "source": "output"}


def test_a_resumed_dispatch_does_not_read_the_header(ledger, monkeypatch):
    _fake_codex(ledger, monkeypatch, CODEX_HEADER.format(sid=SID))
    did = _supervise_codex(ledger, monkeypatch, "executor", "worker", session_id="parent-sess", resumed_from="D0")
    assert ledger.session(did) == "parent-sess" and not ledger.events("session.mismatch")


def _sniff(monkeypatch, pieces, adapter=None, record=None, log=None):
    from office import adapters, dispatch as dispatch_mod
    seen = []
    monkeypatch.setattr(dispatch_mod, "_record_session",
                        record or (lambda run, dispatch, session, *, source: seen.append((session, source))))
    sniffer = dispatch_mod._SessionSniffer({}, {"id": "D1"}, adapter or adapters.load_all()["codex"], log)
    for piece in pieces:
        sniffer.feed(piece)
    return seen


def test_the_header_is_found_when_it_arrives_in_pieces(monkeypatch):
    seen = _sniff(monkeypatch, [b"--------\nsession", b" id: " + SID[:10].encode(), SID[10:].encode() + b"\r\n--------\n"])
    assert seen == [(SID, "output")]


def test_a_coloured_header_line_is_still_read(monkeypatch):
    assert _sniff(monkeypatch, [b"\x1b[1mCodex\x1b[0m\n--------\n\x1b[2msession id: " + SID.encode() + b"\x1b[0m\n"]) == [(SID, "output")]


@pytest.mark.parametrize("line", ["session id: --dangerously-bypass", "session id: $(touch x)", "session id: abc-123",
                                  "session id: " + "a" * 5000])
def test_a_malformed_header_id_is_not_recorded(monkeypatch, line):
    assert _sniff(monkeypatch, [b"--------\n" + line.encode() + b"\n--------\n"]) == []


@pytest.mark.parametrize("lead,found", [(3, True), (4, False)])
def test_the_banner_before_the_opening_rule_is_limited_to_three_lines(monkeypatch, lead, found):
    stream = b"".join(b"line %d\n" % i for i in range(lead)) + b"--------\nsession id: " + SID.encode() + b"\n"
    assert _sniff(monkeypatch, [stream]) == ([(SID, "output")] if found else [])


@pytest.mark.parametrize("pattern", ["^session id:\\s*\\S+$", "^session id:\\s*(\\S+)(\\S*)$"])
def test_an_output_pattern_without_exactly_one_group_is_unusable(pattern):
    from office import adapters
    adapter = {"session": {"id": "detected", "sources": ["output"], "output_pattern": pattern}}
    assert adapters.session_output_pattern(adapter) is None


@pytest.mark.parametrize("stream,why", [(b"--------\nmodel: m\n--------\n", "closed without a session id"),
                                        (b"a\nb\nc\nd\n", "no header block opened")])
def test_a_stream_that_yields_no_id_says_why_in_the_log(monkeypatch, tmp_path, stream, why):
    log = tmp_path / "output.log"
    assert _sniff(monkeypatch, [stream], log=log) == [] and why in log.read_text()


def test_output_that_never_closes_a_line_stops_being_read(monkeypatch):
    sniffer_seen = _sniff(monkeypatch, [b"--------\n" + b"x" * 20000, b"\nsession id: " + SID.encode() + b"\n"])
    assert sniffer_seen == []


def test_a_failing_record_is_retried_then_noted_in_the_log(monkeypatch, tmp_path):
    calls = []

    def boom(run, dispatch, session, *, source):
        calls.append(session)
        raise RuntimeError("database is locked")

    log = tmp_path / "output.log"
    _sniff(monkeypatch, [b"--------\nsession id: " + SID.encode() + b"\n--------\n"], record=boom, log=log)
    assert calls == [SID] * 2 and "could not record session id" in log.read_text()


def test_an_unusable_output_pattern_is_noted_in_the_log(monkeypatch, tmp_path):
    from office import adapters
    bad = {"session": {"id": "detected", "sources": ["output"], "output_pattern": "(unclosed"}}
    log = tmp_path / "output.log"
    assert _sniff(monkeypatch, [b"--------\nsession id: x\n"], adapter=bad, log=log) == []
    assert adapters.session_output_pattern(bad) is None and "unusable" in log.read_text()


def test_pane_argv_carries_the_assigned_id(ledger):
    from office import dispatch as dispatch_mod
    did = ledger.dispatch()
    d = _row(ledger, did)
    dispatch_mod._assign_session(d)
    args, kind = dispatch_mod._interactive(d, "worker", ledger.tmp)
    assert kind == "claude" and args[-2:] == ["--session-id", ledger.session(did)]


def test_resumed_dispatch_passes_no_assign_arg(ledger):
    from office import dispatch as dispatch_mod
    did = ledger.dispatch(resumed_from="D0", session_id="parent-id")
    d = _row(ledger, did)
    args, _ = dispatch_mod._interactive(d, "worker", ledger.tmp)
    assert "--session-id" not in args


def test_herdr_reported_id_is_recorded_for_a_harness_that_takes_none(ledger):
    from office import dispatch as dispatch_mod
    did = ledger.dispatch(harness="codex", adapter_id="codex")
    d = _row(ledger, did)
    dispatch_mod._record_session(ledger.run, d, "codex-thread-1", source="herdr")
    assert ledger.session(did) == "codex-thread-1"


# ---------------------------------------------------------------- hook backfill

def test_a_worker_hook_backfills_an_empty_session_id(ledger, monkeypatch):
    did = ledger.dispatch()
    assert _hook(ledger, monkeypatch, did, "sess-A") == 0
    assert ledger.session(did) == "sess-A"
    assert not ledger.events("session.mismatch")


def test_a_hook_never_overwrites_a_recorded_id_and_notes_the_mismatch_once(ledger, monkeypatch):
    did = ledger.dispatch(session_id="assigned-1")
    for _ in range(3):
        _hook(ledger, monkeypatch, did, "other-2")
    assert ledger.session(did) == "assigned-1"
    events = ledger.events("session.mismatch")
    assert len(events) == 1 and events[0]["dispatch_id"] == did
    assert json.loads(events[0]["payload_json"]) == {"recorded": "assigned-1", "seen": "other-2", "source": "hook"}
    _hook(ledger, monkeypatch, did, "third-3")
    assert len(ledger.events("session.mismatch")) == 2


def test_a_matching_id_changes_nothing(ledger, monkeypatch):
    did = ledger.dispatch(session_id="same-1")
    _hook(ledger, monkeypatch, did, "same-1")
    assert ledger.session(did) == "same-1" and not ledger.events("session.mismatch")


def test_a_hook_from_another_harness_does_not_backfill(ledger, monkeypatch):
    did = ledger.dispatch(harness="claude")
    _hook(ledger, monkeypatch, did, "codex-thread", harness="codex")
    assert ledger.session(did) is None


def test_a_hook_only_touches_its_own_dispatch(ledger, monkeypatch):
    mine, other = ledger.dispatch(), ledger.dispatch(task_id="T2")
    _hook(ledger, monkeypatch, mine, "sess-A")
    assert ledger.session(mine) == "sess-A" and ledger.session(other) is None
    from office import dispatch as dispatch_mod
    assert dispatch_mod.record_session(ledger.con, ledger.run, "D-nope", "x", source="hook") == "ignored"
    ledger.con.execute("INSERT INTO runs(id, office_version, phase, created_at) VALUES('R2', 'v', 'executing', 'now')")
    foreign = ledger.dispatch(run_id="R2")
    assert dispatch_mod.record_session(ledger.con, ledger.run, foreign, "x", source="hook") == "ignored"
    assert ledger.session(foreign) is None


@pytest.mark.parametrize("bad", ["--yolo", "a b", "x;rm", "", "a" * 200])
def test_a_malformed_session_id_is_never_recorded(ledger, monkeypatch, bad):
    did = ledger.dispatch()
    _hook(ledger, monkeypatch, did, bad)
    assert ledger.session(did) is None


def test_a_hook_without_a_dispatch_identity_or_session_records_nothing(ledger, monkeypatch):
    from office import hooks
    did = ledger.dispatch()
    monkeypatch.setenv("OFFICE_RUN_ID", "R1")
    monkeypatch.delenv("OFFICE_DISPATCH_ID", raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": "sess-A", "cwd": str(ledger.repo)})))
    hooks.main(["PostToolUse", "--harness", "claude"])
    monkeypatch.setenv("OFFICE_DISPATCH_ID", did)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": str(ledger.repo)})))
    hooks.main(["PostToolUse", "--harness", "claude"])
    assert ledger.session(did) is None


def test_a_failing_backfill_does_not_skip_the_write_guard(ledger, monkeypatch, capsys):
    from office import dispatch as dispatch_mod, hooks
    did = ledger.dispatch(worktree=str(ledger.tmp / "wt"))
    monkeypatch.setattr(dispatch_mod, "record_session", lambda *a, **k: 1 / 0)
    monkeypatch.setenv("OFFICE_RUN_ID", "R1")
    monkeypatch.setenv("OFFICE_DISPATCH_ID", did)
    outside = {"session_id": "s", "cwd": str(ledger.repo), "tool_input": {"file_path": str(ledger.repo / "calc.py")}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(outside)))
    assert hooks.main(["PreToolUse", "--harness", "claude"]) == 2
    assert "only write inside its worktree" in capsys.readouterr().err


# ---------------------------------------------------------------- inspect

def test_inspect_task_says_when_no_session_id_could_be_captured(ledger):
    from office import inspect_cmd
    ledger.con.execute("INSERT INTO tasks(run_id, id, title, role, scope_json, depends_json, accept_json, checks_json, status, "
                       "introduced_plan_version, contract_version, acceptance_version, created_at, updated_at) "
                       "VALUES('R1','T1','t','executor','[]','[]','[]','[]','running',1,1,1,'now','now')")
    none = ledger.dispatch(harness="agy", adapter_id="agy")
    have = ledger.dispatch(session_id="sess-9")
    lines = inspect_cmd.inspect(ledger.con, ledger.run, "task", "T1").lines
    mine = {line.split()[1]: line for line in lines if line.startswith("dispatch ")}
    assert "session unavailable: agy exposes none" in mine[none]
    assert "session sess-9" in mine[have] and "unavailable" not in mine[have]


# ---------------------------------------------------------------- resume from the recorded id

@pytest.mark.parametrize("harness,resume_form,reply_flag", [("claude", ["--resume", "rev-sess-1"], "--add-dir"),
                                                           ("codex", ["resume", "rev-sess-1"], "--cd")])
def test_a_resumed_reviewer_launch_still_has_its_reply_dir(ledger, monkeypatch, harness, resume_form, reply_flag):
    did = ledger.dispatch(role="code_reviewer", kind="code_reviewer", harness=harness, adapter_id=harness,
                          resumed_from="D0")
    ddir = ledger.tmp / "ddir"
    ddir.mkdir()
    dispatch_mod = _sync_launch(ledger, monkeypatch)
    monkeypatch.setattr(dispatch_mod, "herdr_usable", lambda: False)
    monkeypatch.setattr(dispatch_mod.subprocess, "Popen", lambda *a, **k: type("P", (), {"pid": 1})())
    # The form gates builds has no output, so no reply dir.
    bare = {"parent": "D0", "session_id": "rev-sess-1", "argv": ["x", *resume_form], "herdr_kind": harness}
    dispatch_mod.launch(ledger.run, _row(ledger, did), "reviewer", ddir, cwd=ledger.tmp, output=ddir / "reply.txt", resume=bare)
    notice = next(e["summary"] for e in ledger.events("launch") if "resume needs a herdr session" in e["summary"])
    args = notice.split("Start it by hand: ", 1)[1].split()[1:]
    assert args[-2:] == resume_form, notice
    assert str(ddir) in args[args.index(reply_flag) + 1:], notice


def test_reviewer_resume_argv_comes_from_the_recorded_id_after_the_pane_closed(ledger, monkeypatch):
    from office import dispatch as dispatch_mod, gates
    herdr = ledger.tmp / "bin" / "herdr"
    herdr.parent.mkdir(exist_ok=True)
    herdr.write_text(f"#!{sys.executable}\nimport sys\nsys.stderr.write('{{\"error\": {{\"code\": \"agent_not_found\"}}}}')\nsys.exit(1)\n")
    herdr.chmod(0o755)
    monkeypatch.setenv("PATH", f"{herdr.parent}:/usr/bin:/bin")
    monkeypatch.setattr(dispatch_mod, "herdr_usable", lambda: True)
    parent = ledger.dispatch(role="code_reviewer", kind="code_reviewer", status="exited", launcher="herdr", pane_id="w1:p5",
                             pane_closed_at="then", session_id="rev-sess-1", triple="claude:m:high")
    spec, route = gates._reviewer_resume(ledger.con, ledger.run, parent, "reviewer", ledger.tmp)
    assert spec["session_id"] == "rev-sess-1" and spec["argv"][-2:] == ["--resume", "rev-sess-1"], spec
    assert "--session-id" not in spec["argv"] and route == "claude:m:high"
    # An ended reviewer whose id was never captured falls back to a fresh session on its route.
    bare = ledger.dispatch(role="code_reviewer", kind="code_reviewer", status="exited", launcher="herdr", triple="claude:m:high")
    assert gates._reviewer_resume(ledger.con, ledger.run, bare, "reviewer", ledger.tmp)[0] is None
    assert "no stored harness session id" in ledger.events("review.resume_fallback")[0]["summary"]


# ---------------------------------------------------------------- pane launch (fake herdr)

@pytest.mark.approved
def test_pane_launch_starts_claude_with_the_assigned_id_and_never_polls_herdr_for_one(env, monkeypatch):
    from test_herdr_agent_launch import _calls, launch_in_herdr
    state_file, run, d, ddir, res = launch_in_herdr(env, monkeypatch, reads=["Working (1s • esc to interrupt)"],
                                                    adapter="claude", model="fake-model", effort="high")
    assert res["launcher"] == "herdr"
    session = env.con().execute("SELECT session_id FROM dispatches WHERE id=?", (d["id"],)).fetchone()[0]
    assert UUID.match(session or ""), session
    calls = _calls(state_file)
    start = next(c for c in calls if c[:2] == ["agent", "start"])
    assert start[-2:] == ["--session-id", session]
    assert not any(c[:2] == ["agent", "get"] for c in calls), "herdr was polled for an id Office assigned"
    ledger_rows = [json.loads(line) for line in (Path(ddir).parents[1] / "panes.jsonl").read_text().splitlines()]
    assert ledger_rows[-1]["session_id"] == session
