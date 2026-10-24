"""Registration and launch contracts for the seeded Kilo adapter.

Every harness interaction uses a local fake binary; no test makes a model call.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from conftest import GOOD_ADD, approved_run, task_row
from retired_mimo_fixture import isolated_retired_mimo_catalog
from office import adapters, candidates, configcmd, onboarding, routing, scoring
from office.state import Refused

MODEL_ID = "mimo-v2.6-flash"
MODEL = "kilo/xiaomi/mimo-v2.6-flash"
ROUTE = f"kilo/{MODEL_ID}@medium"
EXECUTOR_CAPS = {"roles": {"executor": {"required_capabilities": ["builder"]}}}
PLANNER_CAPS = {"roles": {"planner": {"required_capabilities": ["planning"]}}}
REVIEW_CAPS = {"roles": {"code_reviewer": {"required_capabilities": ["review"]}}}
PASS = {"reply": "VERDICT: APPROVED\nNEXT proceed"}

FAKE_KILO = f'''#!{sys.executable}
import json, os, sys, time
args = sys.argv[1:]
if args == ["--version"]:
    print("Kilo 7.8.8")
    raise SystemExit(0)
if args == ["models"]:
    models = os.environ.get("FAKE_KILO_MODELS", "kilo/xiaomi/mimo-v2.6-flash\\nkilo/xiaomi/mimo-v2.6-pro\\nkilo/stepfun/step-5-preview-free")
    print(models.replace("\\\\n", "\\n"))
    raise SystemExit(0)
if args == ["auth", "list"]:
    log = os.environ.get("KILO_AUTH_LOG")
    if log:
        with open(log, "a") as f:
            f.write("auth list\\n")
    state = os.environ.get("FAKE_KILO_AUTH", "count")
    if state == "nonzero":
        print("auth list failed", file=sys.stderr)
        raise SystemExit(7)
    if state == "empty":
        raise SystemExit(0)
    if state == "bad-utf8-stdout":
        sys.stdout.buffer.write(b"1 credentials in \\xff\\xfe/auth.json\\n")
        raise SystemExit(0)
    if state == "bad-utf8-stderr":
        sys.stderr.buffer.write(b"warn \\xff\\xfe\\n")
        print("1 credentials in /home/test/auth.json")
        raise SystemExit(0)
    if state == "bad-utf8-stderr-nonzero":
        sys.stderr.buffer.write(b"fail \\xff\\xfe\\n")
        raise SystemExit(7)
    if state == "junk":
        print("Kilo account status unknown")
        raise SystemExit(0)
    if state == "long-count":
        print("9" * 5000 + " credentials in /home/test/auth.json")
        raise SystemExit(0)
    if state == "entry":
        print("OpenAI | API key")
        raise SystemExit(0)
    print(os.environ.get("FAKE_KILO_AUTH_OUTPUT", "0 credentials in /home/test/.local/share/kilo/auth.json"))
    raise SystemExit(0)
if args[:1] == ["run"]:
    log = os.environ.get("KILO_RUN_LOG")
    if log:
        with open(log, "a") as f:
            f.write(json.dumps(args) + "\\n")
    if "--auto" not in args:
        print("Error: command auto-rejected because automatic execution is disabled", file=sys.stderr)
        raise SystemExit(1)
    mode = os.environ.get("FAKE_KILO_MODE", "events")
    if mode == "sleep":
        time.sleep(5)
    if mode == "nonzero":
        print("Error: You need to sign in to use this model.", file=sys.stderr)
        raise SystemExit(23)
    if mode == "empty":
        raise SystemExit(0)
    print(json.dumps({{"type": "session", "session_id": "ses_fake_kilo_123"}}))
    if mode == "wrong-model":
        print(json.dumps({{"type": "text", "text": "I am a different model."}}))
    else:
        print(json.dumps({{"type": "text", "text": "READY"}}))
    raise SystemExit(0)
print("unexpected fake kilo arguments: " + repr(args), file=sys.stderr)
raise SystemExit(2)
'''


@pytest.fixture(autouse=True)
def _isolate_user_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICE_USER_CATALOG",
                       str(isolated_retired_mimo_catalog(tmp_path, "kilo")))


def _adapter() -> dict:
    return adapters.load_all()["kilo"]


def _make_kilo(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "kilo"
    script.write_text(textwrap.dedent(FAKE_KILO))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


@pytest.fixture
def kilo_on_path(tmp_path, monkeypatch):
    _make_kilo(tmp_path)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    return tmp_path


def _install_kilo(env) -> Path:
    # Deliberately keep this out of Env.fakes so launch tests exercise the real
    # subprocess boundary and the fake binary's actual argv/output behavior.
    return _make_kilo(env.bin)


def _approved(env):
    _install_kilo(env)
    os.environ["OFFICE_USER_CATALOG"] = str(isolated_retired_mimo_catalog(env.tmp, "kilo"))
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}],
                 convergence_reviewer=[PASS])


def _dispatch_as_kilo(env, **extra):
    return env.office("dispatch", "T1", "--as", ROUTE, env={**extra}, check=0)


def _dispatch_row(env) -> dict:
    con = env.con()
    try:
        return dict(con.execute("SELECT * FROM dispatches WHERE task_id='T1' ORDER BY started_at DESC").fetchone())
    finally:
        con.close()


def _log(env, row) -> str:
    return Path(row["log_path"]).read_text(errors="replace")


def _run_log(env, mode="events") -> Path:
    return env.tmp / f"kilo-{mode}.jsonl"


def test_kilo_auth_list_uses_a_positive_credential_count(kilo_on_path, monkeypatch):
    monkeypatch.delenv("OFFICE_AUTH_FIXTURE", raising=False)
    monkeypatch.setenv("FAKE_KILO_AUTH", "count")
    monkeypatch.setenv("FAKE_KILO_AUTH_OUTPUT", "3 credentials in /home/test/.local/share/kilo/auth.json")
    state, reason = onboarding.credentials("kilo")
    assert state == "found" and "3 credentials" in reason


def test_kilo_auth_list_zero_count_overrides_empty_auth_file(kilo_on_path, monkeypatch, tmp_path):
    monkeypatch.delenv("OFFICE_AUTH_FIXTURE", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("FAKE_KILO_AUTH", "count")
    auth_file = tmp_path / ".local" / "share" / "kilo" / "auth.json"
    auth_file.parent.mkdir(parents=True)
    auth_file.write_text("")
    monkeypatch.setenv("FAKE_KILO_AUTH_OUTPUT", f"0 credentials in {auth_file}")
    state, reason = onboarding.credentials("kilo")
    assert state == "missing" and "zero credentials" in reason


def test_kilo_auth_list_positive_count_with_extra_junk_is_missing(kilo_on_path, monkeypatch):
    monkeypatch.delenv("OFFICE_AUTH_FIXTURE", raising=False)
    monkeypatch.setenv("FAKE_KILO_AUTH", "count")
    monkeypatch.setenv("FAKE_KILO_AUTH_OUTPUT",
                       "1 credentials in /home/test/auth.json\nKilo account status unknown")
    state, reason = onboarding.credentials("kilo")
    assert state == "missing" and "no recognizable credential entry" in reason


@pytest.mark.parametrize("auth_state", ["bad-utf8-stdout", "bad-utf8-stderr", "bad-utf8-stderr-nonzero"])
def test_kilo_auth_list_invalid_utf8_is_missing_not_a_crash(kilo_on_path, monkeypatch, auth_state):
    monkeypatch.delenv("OFFICE_AUTH_FIXTURE", raising=False)
    monkeypatch.setenv("FAKE_KILO_AUTH", auth_state)
    state, reason = onboarding.credentials("kilo")
    assert state == "missing" and "unparseable output" in reason


def test_kilo_auth_list_recognizes_credential_entry(kilo_on_path, monkeypatch):
    monkeypatch.delenv("OFFICE_AUTH_FIXTURE", raising=False)
    monkeypatch.setenv("FAKE_KILO_AUTH", "entry")
    state, reason = onboarding.credentials("kilo")
    assert state == "found" and "1 credential entry" in reason


@pytest.mark.parametrize(("auth_state", "expected"), [
    ("nonzero", "exited 7"),
    ("empty", "printed nothing"),
    ("junk", "no recognizable credential entry"),
    ("long-count", "no recognizable credential entry"),
])
def test_kilo_auth_list_missing_reasons_are_distinct(kilo_on_path, monkeypatch, auth_state, expected):
    monkeypatch.delenv("OFFICE_AUTH_FIXTURE", raising=False)
    monkeypatch.setenv("FAKE_KILO_AUTH", auth_state)
    state, reason = onboarding.credentials("kilo")
    assert state == "missing" and expected in reason


def test_kilo_auth_list_not_installed_has_distinct_reason(monkeypatch):
    monkeypatch.delenv("OFFICE_AUTH_FIXTURE", raising=False)
    monkeypatch.setattr(onboarding.shutil, "which", lambda _: None)
    assert onboarding.credentials("kilo") == ("missing", "kilo is not installed")


def test_kilo_auth_list_timeout_uses_argv_and_bounded_no_shell_run(kilo_on_path, monkeypatch):
    monkeypatch.delenv("OFFICE_AUTH_FIXTURE", raising=False)
    calls = []

    def timeout(argv, **kwargs):
        calls.append((argv, kwargs))
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(onboarding.subprocess, "run", timeout)
    assert onboarding.credentials("kilo") == ("missing", "kilo auth list timed out")
    assert calls[0][0] == ["kilo", "auth", "list"]
    assert calls[0][1]["timeout"] == 10 and calls[0][1]["stdin"] is subprocess.DEVNULL
    assert "shell" not in calls[0][1]


def test_kilo_auth_fixture_short_circuits_binary(kilo_on_path, monkeypatch, tmp_path):
    log = tmp_path / "invoked"
    monkeypatch.setenv("KILO_AUTH_LOG", str(log))
    monkeypatch.setenv("OFFICE_AUTH_FIXTURE", json.dumps({"kilo": "found"}))
    assert onboarding.credentials("kilo") == ("found", "fixture")
    assert not log.exists()


def test_kilo_auth_list_missing_reasons_are_pairwise_distinct(kilo_on_path, monkeypatch):
    monkeypatch.delenv("OFFICE_AUTH_FIXTURE", raising=False)
    monkeypatch.setenv("FAKE_KILO_AUTH", "count")
    monkeypatch.setenv("FAKE_KILO_AUTH_OUTPUT", "0 credentials in /home/test/auth.json")
    original_which = onboarding.shutil.which
    original_run = onboarding.subprocess.run
    reasons = [onboarding.credentials("kilo")[1]]
    monkeypatch.setattr(onboarding.shutil, "which", lambda _: None)
    reasons.append(onboarding.credentials("kilo")[1])
    monkeypatch.setattr(onboarding.shutil, "which", lambda _: "/fake/kilo")

    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(onboarding.subprocess, "run", timeout)
    reasons.append(onboarding.credentials("kilo")[1])
    monkeypatch.setattr(onboarding.shutil, "which", original_which)
    monkeypatch.setattr(onboarding.subprocess, "run", original_run)
    for auth_state in ("nonzero", "empty", "junk"):
        monkeypatch.setenv("FAKE_KILO_AUTH", auth_state)
        reasons.append(onboarding.credentials("kilo")[1])
    assert len(set(reasons)) == len(reasons)


def _trust_acts(env) -> int:
    con = env.con()
    try:
        scoring.ensure_trust_schema(con)
        return con.execute("SELECT COUNT(*) FROM adapter_trust_acts").fetchone()[0]
    finally:
        con.close()


def test_forms_keep_pure_auto_and_prompt_transport_scoped(tmp_path):
    adapter = _adapter()
    worker = adapter["office_profiles"]["worker"]
    pane = worker["interactive"]["argv"]
    forms = [adapter["invocation"]["argv"], worker["argv"], pane]

    assert all("--pure" in form for form in forms)
    assert "--auto" in worker["argv"] and "--auto" in pane
    assert "--auto" in adapter["invocation"]["argv"]  # CLI execution is also autonomous.
    trust_justifications = (Path(__file__).resolve().parents[2] / "adapters" / "trust_justifications.yaml").read_text()
    kilo_justification = trust_justifications.split("kilo:", 1)[1]
    assert "--auto:" in kilo_justification and "Worker and pane forms only" in kilo_justification
    assert all("{prompt}" not in token for form in forms for token in form)
    assert worker["prompt"] == "argv"

    argv, _ = adapters.build_argv(adapter, "worker", model=MODEL, effort="medium", cwd=tmp_path)
    prompt = "Use spaces; keep $(echo safe) as text.\nSecond line."
    argv.append(prompt)
    assert argv[-1] == prompt and argv.count(prompt) == 1


def test_pane_form_renders_pinned_model_without_variant(tmp_path):
    args, kind = adapters.interactive_argv(_adapter(), "worker", model=MODEL, effort="medium", cwd=tmp_path)
    assert kind == "kilo"
    assert args[args.index("--model") + 1] == MODEL
    assert "--pure" in args and "--auto" in args
    assert "--variant" not in args


def test_fake_binary_serves_version_models_json_session_and_auto_reject(kilo_on_path):
    exe = kilo_on_path / "kilo"
    version = subprocess.run([str(exe), "--version"], text=True, capture_output=True, check=True)
    models = subprocess.run([str(exe), "models"], text=True, capture_output=True, check=True)
    events = subprocess.run([str(exe), "run", "--format", "json", "--pure", "--auto"],
                            text=True, capture_output=True, check=True)
    assert "7.8.8" in version.stdout and MODEL in models.stdout
    event_data = [json.loads(line) for line in events.stdout.splitlines()]
    pattern = adapters.session_spec(_adapter())["output_pattern"]
    assert event_data[0]["session_id"] == re.search(pattern, events.stdout).group(1) == "ses_fake_kilo_123"
    rejected = subprocess.run([str(exe), "run", "--format", "json", "--pure"], text=True, capture_output=True)
    assert rejected.returncode == 1 and "auto-rejected" in rejected.stderr


def test_model_listing_passes_and_missing_model_fails_preflight(kilo_on_path, monkeypatch):
    ok, detail = adapters.run_preflight(_adapter(), MODEL)
    assert ok, detail
    monkeypatch.setenv("FAKE_KILO_MODELS", "kilo/xiaomi/mimo-v2.6-pro")
    ok, detail = adapters.run_preflight(_adapter(), MODEL)
    assert not ok and MODEL in detail and "model_check" in detail, detail


def test_builder_only_kilo_is_refused_as_planner_and_reviewer_at_routing_and_setup(monkeypatch):
    monkeypatch.setattr(adapters, "installed", lambda a: True)
    monkeypatch.setattr(adapters, "harness_version", lambda a: "7.8.8")
    for role, config, capability in (("planner", PLANNER_CAPS, "planning"),
                                     ("code_reviewer", REVIEW_CAPS, "review")):
        with pytest.raises(Refused) as exc:
            candidates.declared_decision(ROUTE, flag="--as", role=role, config=config)
        assert f"['{capability}']" in str(exc.value), exc.value
        seeds = [{"model_id": MODEL_ID, "harness": "kilo", "effort": "medium"}]
        problems = configcmd.validate_seed(seeds, role=role, config=config)
        assert problems and f"cannot serve role {role}" in problems[0], problems
        assert not [r for r in configcmd.known_routes(role, config=config) if r.startswith("kilo/")]

    cands, _ = candidates.build_candidates(None, "planner", probe=False)
    kilo = [c for c in cands if c["harness"] == "kilo"]
    assert kilo
    for role, capability in (("planner", "planning"), ("code_reviewer", "review")):
        c = kilo[-1]
        request = {"role": role, "required_capabilities": [capability], "candidates": [c], "policy": {}}
        if role == "code_reviewer":
            request["declared_route"] = routing.candidate_id(c)
        result = routing.route(request)
        assert result["selected"] is None and result["status"] == "no_qualifying_candidate"
        assert any(f"missing capabilities ['{capability}']" in r["reason"] for r in result["rejected"])


def test_builder_only_kilo_is_refused_as_reviewer_at_dispatch(env):
    _approved(env)
    code, out = env.office("dispatch", "T1", "--review-cli", "agent --flag", "--review-as", ROUTE)
    assert code == 4 and "declares capabilities ['builder']" in out and "['review']" in out, out
    con = env.con()
    try:
        assert not con.execute("SELECT * FROM dispatches WHERE harness='kilo'").fetchall()
    finally:
        con.close()


def test_supported_row_launches_pinned_slug_and_appends_prompt_as_one_argument(env):
    _approved(env)
    log = _run_log(env)
    _, out = _dispatch_as_kilo(env, KILO_RUN_LOG=str(log))
    row = _dispatch_row(env)
    assert row["model"] == MODEL and row["terminal_classification"] == "success", (row, out)
    args = json.loads(log.read_text().splitlines()[0])
    assert args[args.index("--model") + 1] == MODEL
    assert "--pure" in args and "--auto" in args
    assert args[-1].startswith("ROLE executor\nTASK T1 ") and "SCOPE calc.py" in args[-1], args[-1]
    assert row["terminal_classification"] == "success"


def test_missing_model_fails_preflight_before_fake_run(env):
    _approved(env)
    log = _run_log(env, "missing")
    _, out = _dispatch_as_kilo(env, FAKE_KILO_MODELS="kilo/xiaomi/mimo-v2.6-pro", KILO_RUN_LOG=str(log))
    row = _dispatch_row(env)
    assert row["terminal_classification"] == "preflight_failed" and row["exit_code"] == 127, (row, out)
    assert "launch stopped before the harness started" in _log(env, row) and MODEL in _log(env, row)
    assert not log.exists(), "preflight must stop before invoking kilo run"


def test_nonzero_exit_keeps_harness_message(env):
    _approved(env)
    _, out = _dispatch_as_kilo(env, FAKE_KILO_MODE="nonzero")
    row = _dispatch_row(env)
    assert row["terminal_classification"] == "nonzero" and row["exit_code"] == 23, (row, out)
    assert "You need to sign in to use this model" in _log(env, row)


def test_wall_cap_classifies_timeout(env):
    _approved(env)
    _, out = _dispatch_as_kilo(env, FAKE_KILO_MODE="sleep", OFFICE_WORKER_MAX_MINUTES="0.02")
    row = _dispatch_row(env)
    assert row["terminal_classification"] == "timeout", (row, out)
    assert "wall-clock cap" in _log(env, row)


def test_exit_zero_without_output_is_flagged_and_not_accepted(env):
    _approved(env)
    _, out = _dispatch_as_kilo(env, FAKE_KILO_MODE="empty")
    row = _dispatch_row(env)
    assert row["terminal_classification"] == "success", (row, out)
    assert "exited 0 with no output" in _log(env, row)
    assert task_row(env)["status"] != "accepted"


def test_wrong_model_output_does_not_promote_adapter_trust(env):
    _approved(env)
    before = _trust_acts(env)
    _, out = _dispatch_as_kilo(env, FAKE_KILO_MODE="wrong-model")
    row = _dispatch_row(env)
    assert row["terminal_classification"] == "success", (row, out)
    assert _trust_acts(env) == before, "a launch outcome cannot promote adapter trust"
