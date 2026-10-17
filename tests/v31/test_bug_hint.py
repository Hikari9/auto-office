"""The runtime `bug?:` hint line: which outcomes print one, which never do, and that it only reads."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from hypothesis import example, given, strategies as st

from conftest import GOOD_ADD, Env, _activate, approved_run, start_inline
from office import bughint, cli, guide, state

pytestmark = pytest.mark.approved

HINT = re.compile(r"^bug\?: [^\n]+ \(if this is an Office defect: office self-improve\)$")
EARLY = "2030-01-01T00:00:00+00:00"
LATE = "2030-01-02T00:00:00+00:00"


def _hints(out: str) -> list[str]:
    return [ln for ln in out.splitlines() if ln.startswith("bug?:")]


def _sql(env, sql: str, *params):
    con = env.con()
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def _run_id(env) -> str:
    return _sql(env, "SELECT id FROM runs ORDER BY created_at DESC LIMIT 1")[0][0]


def _counts(env) -> dict:
    con = env.con()
    try:
        names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in names}
    finally:
        con.close()


def seed_job(env, *, kind="run_checks", status="failed", error="KeyError: 'gate'", task="T1", gate=None, at=EARLY,
             attempts=2):
    payload = {"task_id": task, **({"gate_id": gate} if gate else {})}
    _sql(env, "INSERT INTO outbox(id,run_id,kind,dedup_key,payload_json,office_version,status,attempts,max_attempts,error,"
              "finished_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
         "J" + uuid.uuid4().hex[:8], _run_id(env), kind, uuid.uuid4().hex, json.dumps(payload), "3.6.1", status, attempts, 2,
         error, at, at)


def seed_gate(env, gid, *, verdict="UNAVAILABLE", kind="checks", subject="task", task="T1", at=EARLY):
    _sql(env, "INSERT INTO gates(id,run_id,subject,task_id,plan_version,kind,input_key,status,verdict,created_at,finished_at) "
              "VALUES(?,?,?,?,?,?,?,?,?,?,?)", gid, _run_id(env), subject, task, 1, kind, f"{kind}:{gid}", "done", verdict, at, at)


def seed_dispatch(env, how, *, task="T1", status="failed", at=EARLY):
    _sql(env, "INSERT INTO dispatches(id,run_id,role,task_id,status,terminal_classification,started_at,ended_at) "
              "VALUES(?,?,?,?,?,?,?,?)", "D" + uuid.uuid4().hex[:8], _run_id(env), "executor", task, status, how, at, at)


def seed_event(env, kind, *, task=None, at=LATE):
    _sql(env, "INSERT INTO events(run_id,kind,audience,task_id,summary,office_version,created_at) VALUES(?,?,?,?,?,?,?)",
         _run_id(env), kind, "orchestrator", task, kind, "3.6.1", at)


@pytest.fixture
def run(env):
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}])
    return env


def _signals(env) -> list[str]:
    con = env.con()
    try:
        return bughint.lines(bughint.collect(con, _run_id(env)))
    finally:
        con.close()


# ------------------------------------------------------------------ line format and sanitizer

def test_the_line_has_the_exact_format():
    assert bughint.line("boom") == "bug?: boom (if this is an Office defect: office self-improve)"


@pytest.mark.parametrize("raw", [
    "failed in /Users/someone/work/repo/src/app.py line 3",
    "failed in ~/work/app.py",
    "failed in ../secret/app.py",
    "failed in src/office/gates.py",
    r"failed in C:\Users\someone\app.py",
    "mail someone@example.com about it",
    "fetch https://example.test/a?token=abc and ssh://host/x",
    "token=sk-ant-abcdefghijklmnop1234",
    "using ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    "Bearer abcdefghijklmnop",
    "id 3f2c9a1e-4b5d-4c6e-8f70-1a2b3c4d5e6f",
    "sha 0123456789abcdef0123456789abcdef01234567",
])
def test_sanitizer_removes_paths_emails_urls_secrets_and_long_identifiers(raw):
    out = bughint.line(raw)
    assert HINT.match(out), out
    for leak in ("/Users", "someone", "example", "ghp_", "sk-ant", "abcdefghijklmnop", "src/office", "../", "4b5d",
                 "0123456789abcdef", "ssh://", "C:\\"):
        assert leak not in out, (leak, out)
    assert "[redacted-" in out


@given(st.text(max_size=400))
@example("line one\nline two\x00\ttabbed")
@example("a" * 1000)
def test_any_signal_text_becomes_one_bounded_hint_line(raw):
    out = bughint.line(raw)
    assert "\n" not in out and "\r" not in out
    assert out.startswith("bug?: ") and out.endswith(bughint.SUFFIX)
    assert len(out) <= len("bug?:  ") + bughint.MAX_SIGNAL + len(bughint.SUFFIX)


# ------------------------------------------------------------------ (a) a refusal of the command next: names

@pytest.mark.parametrize("next_line, argv, category, hinted", [
    ("office rerun T8", ["rerun", "T8"], "rerun-mode", True),                       # the run 12af1767 shape
    ("office rerun T8 --resume | office rerun T8 --fresh", ["rerun", "T8", "--resume"], "no-pane", True),
    ("office rerun T8 --resume | office rerun T8 --fresh", ["rerun", "t8", "--fresh"], "no-pane", True),
    ("pause: office rerun T8 then office wait", ["wait"], "gate-busy", True),
    ("office rerun T8 --resume | office rerun T8 --fresh", ["rerun", "T8"], "rerun-mode", False),  # not as spelled
    ("office rerun T8 --resume | office rerun T8 --fresh", ["rerun", "T8", "--resume", "--fresh"], "rerun-mode", False),  # superset of either
    ("office dispatch T1", ["dispatch", "T1", "T2"], "dispatch-busy", False),        # an extra id
    ("office dispatch T1", ["dispatch", "T1"], "dispatch-busy", True),
    ("office dispatch T1", ["dispatch", "T1", "--json"], "dispatch-busy", True),      # output format is not a difference
    ("office rerun T8 --fresh", ["rerun", "T8", "--fresh=1"], "no-pane", True),
    ("office rerun T8", ["rerun", "T9"], "rerun-mode", False),                       # another task
    ("office dispatch T1", ["rerun", "T1"], "rerun-mode", False),                    # another command
    ("no action; the planner is working", ["status"], "rerun-mode", False),          # next: names no command
    ("office rerun T8", ["rerun", "T8"], "", False),
    ("office rerun T8", None, "rerun-mode", False),
])
def test_refusal_signal_needs_the_refused_command_to_be_the_one_next_names(next_line, argv, category, hinted):
    command = argv[0] if argv else None
    signal = bughint.refusal_signal(category, command, argv or [], next_line)
    assert (signal is not None) is hinted
    if hinted:
        assert signal.kind == "refusal" and category in signal.text


@pytest.mark.parametrize("category", sorted(bughint.ROUTINE_REFUSALS))
def test_routine_refusal_categories_never_signal(category):
    assert bughint.refusal_signal(category, "rerun", ["rerun", "T8"], "office rerun T8") is None


def test_a_refused_next_command_prints_one_hint_before_next(run, monkeypatch):
    monkeypatch.setattr(guide, "next_action", lambda con, r: "office rerun T1")
    code, out = run.office("rerun", "T1")
    assert code == 2 and "blocked: rerun-mode" in out, out
    hints = _hints(out)
    assert len(hints) == 1 and HINT.match(hints[0]), out
    assert "rerun-mode" in hints[0]
    assert out.splitlines().index(hints[0]) < [i for i, ln in enumerate(out.splitlines()) if ln.startswith("next:")][0]


def test_a_refusal_hint_changes_no_exit_code_and_is_a_json_list(run, monkeypatch):
    code_plain, _ = run.office("rerun", "T1")
    monkeypatch.setattr(guide, "next_action", lambda con, r: "office rerun T1")
    code, data = run.ojson("rerun", "T1")
    assert code == code_plain == 2
    assert data["ok"] is False and data["error"]["category"] == "rerun-mode"
    assert len(data["bug_hints"]) == 1 and HINT.match(data["bug_hints"][0])


def test_a_refusal_of_some_other_command_prints_no_hint(run, monkeypatch):
    monkeypatch.setattr(guide, "next_action", lambda con, r: "office dispatch T1")
    code, out = run.office("rerun", "T1")
    assert code == 2 and "blocked:" in out and not _hints(out), out


@pytest.mark.parametrize("args, category", [
    (("approve", "plan"), "user-quote-required"),
    (("dispatch", "T9"), "unknown-task"),
])
def test_routine_refusals_of_the_next_command_print_no_hint(run, monkeypatch, args, category):
    monkeypatch.setattr(guide, "next_action", lambda con, r: "office " + " ".join(args))
    code, out = run.office(*args)
    assert code != 0 and f"blocked: {category}" in out and not _hints(out), out
    code, data = run.ojson(*args)
    assert data["bug_hints"] == []


def test_no_run_means_no_refusal_hint(env):
    code, out = env.office("rerun", "T1")
    assert code != 0 and not _hints(out), out


# ------------------------------------------------------------------ (b) an UNAVAILABLE check from a runtime error

def test_unavailable_checks_from_a_failed_job_hint(run):
    seed_gate(run, "Gchk1")
    seed_job(run, gate="Gchk1", error="OSError: disk full")
    found = _signals(run)
    assert len(found) == 1 and "T1 checks gate is UNAVAILABLE because its run_checks job failed" in found[0]
    assert HINT.match(found[0])


def test_an_unavailable_check_and_its_failed_job_are_one_hint_not_two(run):
    seed_gate(run, "Gchk1")
    seed_job(run, gate="Gchk1")
    seed_job(run, kind="launch_agent", gate=None)
    found = _signals(run)
    assert len(found) == 2 and sum("UNAVAILABLE" in f for f in found) == 1


def test_an_unavailable_integration_check_from_a_failed_integrate_job_hints(run):
    seed_gate(run, "Ginteg", subject="integration", task=None, at="2030-01-01T00:00:05+00:00")
    _sql(run, "INSERT INTO outbox(id,run_id,kind,dedup_key,payload_json,office_version,status,attempts,max_attempts,error,"
              "claimed_at,finished_at,created_at) VALUES('Jint',?,?,?,?,?,?,?,?,?,?,?,?)",
         _run_id(run), "integrate", "k1", "{}", "3.6.1", "failed", 1, 1, "ValueError: bad base",
         "2030-01-01T00:00:00+00:00", "2030-01-01T00:00:09+00:00", EARLY)
    found = _signals(run)
    assert len(found) == 1 and "integration checks gate is UNAVAILABLE" in found[0]


def test_unavailable_checks_for_a_missing_user_command_print_no_hint(run):
    """The command is the user's: the check ran, exited 127, and no job failed."""
    seed_gate(run, "Gchk1")
    seed_event(run, "gate.unavailable", task="T1", at=EARLY)
    assert _signals(run) == []
    code, out = run.office("status")
    assert code == 0 and not _hints(out), out


@pytest.fixture
def fresh_env(request, tmp_path, monkeypatch):
    """An Env with no run yet: the module is `approved`, and a second run would make `start` ambiguous."""
    e = Env(tmp_path, monkeypatch)
    e.trust_snapshots = request.getfixturevalue("_trust_snapshots")
    return _activate(e, monkeypatch)


def test_a_real_missing_check_command_leaves_no_hint(fresh_env):
    """End to end: a plan check naming a command that does not exist ends UNAVAILABLE and is not a defect."""
    env = fresh_env
    plan = ("# Plan\n\n## Requirements\ndone:\n- add() returns the sum\nblast_radius: repo\n\n## Tasks\n"
            "### T1: Implement add\nscope: calc.py\ndepends: none\nchecks: office-no-such-command-xyz --run\n"
            "accept:\n- calc.add(2, 3) == 5\nvisual: none\n")
    env.trust()
    env.script(executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
               convergence_reviewer=[{"reply": "VERDICT: APPROVED\nNEXT proceed"}])
    start_inline(env, plan=plan)
    env.office("approve", "plan", "--quote", "approved", check=0)
    env.office("dispatch", "T1", check=0)
    env.office("wait", "--timeout", "30", "--poll", "0.5")
    rows = _sql(env, "SELECT verdict, review_status FROM gates WHERE kind='checks'")
    if not any("UNAVAILABLE" in (v or "") + (s or "") for v, s in rows):
        pytest.skip("the scripted executor did not reach its checks in this environment")
    code, out = env.office("status")
    assert not _hints(out), out


def test_an_ordinary_check_failure_prints_no_hint(run):
    for verdict in ("CHANGES_REQUIRED", "RECHECK"):
        seed_gate(run, "G" + verdict[:4], verdict=verdict)
    assert _signals(run) == []


# ------------------------------------------------------------------ (c) an outbox job failed with an error

def test_a_failed_outbox_job_with_an_error_hints_with_a_sanitized_error(run):
    seed_job(run, kind="launch_agent", error="FileNotFoundError: /Users/someone/work/app/missing.py\nTraceback...")
    found = _signals(run)
    assert len(found) == 1 and HINT.match(found[0]), found
    assert "launch_agent job failed after 2 attempts: FileNotFoundError (if" in found[0]
    assert "someone" not in found[0] and "Traceback" not in found[0]


def test_a_failed_job_hint_quotes_only_the_errors_head_never_its_message(run):
    seed_job(run, kind="launch_agent", error="RuntimeError: password hunter2 and the customer Jane Doe leaked")
    seed_job(run, kind="review", error="x" * 500)
    found = _signals(run)
    assert len(found) == 2 and all(HINT.match(f) for f in found), found
    assert not any(w in " ".join(found) for w in ("hunter2", "Jane", "customer", "leaked"))
    assert any("launch_agent job failed after 2 attempts: RuntimeError (if" in f for f in found)


def test_a_failed_job_without_a_finish_time_is_dated_by_its_creation(run):
    seed_job(run, kind="launch_agent", at=LATE)
    _sql(run, "UPDATE outbox SET finished_at=NULL")
    seed_event(run, "task.unblocked", task="T1", at=EARLY)  # recovered before the job was even created
    assert len(_signals(run)) == 1
    seed_event(run, "task.unblocked", task="T1", at="2030-01-03T00:00:00+00:00")
    assert _signals(run) == []


@pytest.mark.parametrize("status, error", [("queued", "retrying"), ("claimed", "x"), ("done", None), ("cancelled", "x"),
                                            ("failed", ""), ("failed", None)])
def test_only_a_failed_job_with_an_error_signals(run, status, error):
    seed_job(run, status=status, error=error)
    assert _signals(run) == []


# ------------------------------------------------------------------ (d) a dispatch that ended lost or crashed

@pytest.mark.parametrize("how", ["lost", "crashed"])
def test_a_lost_or_crashed_dispatch_hints(run, how):
    seed_dispatch(run, how)
    found = _signals(run)
    assert len(found) == 1 and f"T1 executor dispatch ended {how}" in found[0]


@pytest.mark.parametrize("how, status", [("success", "exited"), ("nonzero", "exited"), ("signal", "exited"),
                                         ("revoked", "cancelled"), ("lost", "cancelled")])
def test_other_dispatch_endings_print_no_hint(run, how, status):
    seed_dispatch(run, how, status=status)
    assert _signals(run) == []


# ------------------------------------------------------------------ recovery events never signal, and clear

@pytest.mark.parametrize("kind", bughint.RECOVERY_EVENTS)
def test_a_recovery_event_alone_prints_no_hint(run, kind):
    seed_event(run, kind, task="T1")
    assert _signals(run) == []
    code, out = run.office("status")
    assert code == 0 and not _hints(out), out


def test_task_unblocked_after_a_failure_clears_it_and_before_it_does_not(run):
    seed_dispatch(run, "lost", at=EARLY)
    seed_job(run, kind="launch_agent", at=EARLY)
    assert len(_signals(run)) == 2
    seed_event(run, "task.unblocked", task="T1", at=LATE)
    assert _signals(run) == []
    seed_dispatch(run, "crashed", at="2030-01-03T00:00:00+00:00")
    found = _signals(run)
    assert len(found) == 1 and "ended crashed" in found[0]


def test_a_recovery_of_another_task_clears_nothing(run):
    seed_dispatch(run, "lost", at=EARLY)
    seed_event(run, "task.unblocked", task="T2", at=LATE)
    assert len(_signals(run)) == 1


# ------------------------------------------------------------------ where hints appear

def test_status_prints_hints_before_next_and_json_carries_a_list(run):
    seed_dispatch(run, "lost")
    code, out = run.office("status")
    assert code == 0
    lines = out.splitlines()
    assert len(_hints(out)) == 1 and lines[-1].startswith("next: "), out
    code, data = run.ojson("status")
    assert code == 0 and data["bug_hints"] == _hints(out)
    assert "bug_hints" not in data["data"] and not any(n.startswith("bug?:") for n in data["notices"])


def test_status_json_has_an_empty_list_when_nothing_is_wrong(run):
    code, data = run.ojson("status")
    assert code == 0 and data["bug_hints"] == []


def test_wait_prints_the_hints(run):
    seed_job(run, kind="launch_agent")
    code, out = run.office("wait", "--timeout", "2", "--poll", "0.1")
    assert len(_hints(out)) == 1, out
    code_json, data = run.ojson("wait", "--timeout", "2", "--poll", "0.1")
    assert code_json == code and len(data["bug_hints"]) == 1


def test_hints_do_not_change_the_exit_code(run, monkeypatch):
    seed_job(run, kind="launch_agent")
    seed_dispatch(run, "lost")
    with monkeypatch.context() as m:
        m.setattr(guide, "add_bug_hints", lambda con, r, res: res)
        plain_status, plain_out = run.office("status")
        plain_wait, _ = run.office("wait", "--timeout", "2", "--poll", "0.1")
    assert not _hints(plain_out)
    code, out = run.office("status")
    assert _hints(out) and code == plain_status
    code, out = run.office("wait", "--timeout", "2", "--poll", "0.1")
    assert _hints(out) and code == plain_wait


def test_a_worker_is_not_shown_hints(run):
    seed_job(run, kind="launch_agent")
    res = guide.add_bug_hints(run.con(), state.get_run(run.con(), _run_id(run)), guide.Result())
    assert len(res.data["bug_hints"]) == 1
    from unittest import mock
    with mock.patch.dict("os.environ", {"OFFICE_DISPATCH_ID": "Dx"}):
        res = guide.add_bug_hints(run.con(), state.get_run(run.con(), _run_id(run)), guide.Result())
    assert "bug_hints" not in res.data


def test_the_hook_status_path_stays_hint_free(run):
    seed_job(run, kind="launch_agent")
    con = run.con()
    try:
        res = guide.status(con, state.get_run(con, _run_id(run)))
    finally:
        con.close()
    assert "bug_hints" not in res.data and not _hints("\n".join(res.lines))


# ------------------------------------------------------------------ office self-improve

def test_self_improve_lists_the_runs_current_signals(run):
    seed_dispatch(run, "lost")
    seed_job(run, kind="launch_agent")
    code, out = run.office("self-improve")
    assert code == 0 and len(_hints(out)) == 2, out
    assert out.splitlines()[-1].startswith("next: ")
    code, data = run.ojson("self-improve")
    assert code == 0 and data["bug_hints"] == _hints(out)
    assert "\n".join(data["lines"]) == cli.SELF_IMPROVE_STEPS and out.startswith(cli.SELF_IMPROVE_STEPS)


def test_self_improve_lists_the_same_signals_as_status(run):
    seed_dispatch(run, "crashed")
    seed_job(run, kind="integrate", task=None)
    _, status = run.ojson("status")
    _, improve = run.ojson("self-improve")
    assert improve["bug_hints"] == status["bug_hints"] and len(status["bug_hints"]) == 2


def test_self_improve_without_signals_or_a_run_lists_none(env, run):
    code, out = run.office("self-improve")
    assert code == 0 and not _hints(out)
    code, data = env.ojson("self-improve", cwd=env.tmp)
    assert code == 0 and "bug_hints" not in data


# ------------------------------------------------------------------ (e) an internal error

def test_an_uncaught_exception_prints_a_hint_on_stderr_and_still_raises(run, monkeypatch, capsys):

    def boom(*_a, **_k):
        raise RuntimeError("/Users/someone/secret.py exploded")

    monkeypatch.setattr(cli, "_run", boom)
    with pytest.raises(RuntimeError):
        cli.main(["status"])
    err = capsys.readouterr().err
    assert _hints(err) == ["bug?: internal error: uncaught RuntimeError (if this is an Office defect: office self-improve)"]
    assert "someone" not in err


def test_a_refusal_is_not_an_internal_error(run, capsys):
    assert cli.main(["rerun", "T1"]) == 2
    assert not _hints(capsys.readouterr().err)


def test_a_keyboard_interrupt_is_not_an_internal_error(run, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_run", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert cli.main(["status"]) == 130
    assert not _hints(capsys.readouterr().err)


# ------------------------------------------------------------------ purity

def test_the_module_imports_no_process_network_or_model_facility():
    source = Path(bughint.__file__).read_text()
    for name in ("subprocess", "socket", "urllib", "http", "requests", "os.system", "Popen"):
        assert not re.search(rf"^\s*(?:import|from)\s+{re.escape(name)}\b|\b{re.escape(name)}\.", source, re.M), name
    for word in ("INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER", "executescript", "commit"):
        assert word not in source.replace("COALESCE", ""), word


def test_detection_spawns_nothing_and_writes_nothing(run, monkeypatch):
    seed_job(run, kind="launch_agent")
    seed_dispatch(run, "lost")
    seed_gate(run, "Gchk1")
    seed_job(run, gate="Gchk1")

    def refuse(*a, **k):
        raise AssertionError("bughint must not start a process")

    for name in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, refuse)
    monkeypatch.setattr("os.system", refuse)
    before = _counts(run)
    con = run.con()
    try:
        found = bughint.collect(con, _run_id(run))
        assert len(bughint.lines(found)) == 3
        assert bughint.refusal_signal("rerun-mode", "rerun", ["rerun", "T1"], "office rerun T1")
    finally:
        con.close()
    assert _counts(run) == before


def test_refusal_hints_write_no_rows(run, monkeypatch):
    monkeypatch.setattr(guide, "next_action", lambda con, r: "office rerun T1")
    before = _counts(run)
    code, out = run.office("rerun", "T1")
    assert code == 2 and _hints(out)
    assert _counts(run) == before
