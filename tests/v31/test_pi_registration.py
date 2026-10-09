"""#479 registration path for the pi harness and the Xiaomi MiMo-V2.6 rows.

What these tests hold to the review's bar: the worker form never grants
project-resource trust (--approve); provider/model are pinned and validated
before launch (missing provider/model or credentials fail clearly, never a
silent wrong-model dispatch); a builder-only pi is not a planner/reviewer route
at routing, at setup or at dispatch; and the deprecated pi/gpt-5-codex row stays
out of candidate selection. Fake `pi` binaries and scenarios stand in for the
harness — no test here calls a real model.
"""
from __future__ import annotations

import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest

import fake_agent
from conftest import GOOD_ADD, approved_run, task_row

from office import adapters, candidates, configcmd, routing
from office.state import Refused

PI = "pi"
MIMO_FLASH = "xiaomi-token-plan-sgp/mimo-v2.6-flash"
MIMO_PRO = "xiaomi-token-plan-sgp/mimo-v2.6-pro"
EXECUTOR_CAPS = {"roles": {"executor": {"required_capabilities": ["builder"]}}}
PLANNER_CAPS = {"roles": {"planner": {"required_capabilities": ["planning"]}}}
REVIEW_CAPS = {"roles": {"code_reviewer": {"required_capabilities": ["review"]}}}


def _adapter() -> dict:
    return adapters.load_all()["pi"]


def _rows() -> list[dict]:
    return [r for r in candidates.catalog_rows() if r.get("invocation_harness") == "pi"]


# ---------------------------------------------------------------- argv and trust flags

def test_pi_worker_argv_never_grants_project_resource_trust():
    """--approve loads project settings, .pi/mcp.json, extensions and skills from
    the worktree: it must never be in a dispatched form. The safe controls stay,
    and AGENTS.md context (--no-context-files is absent) remains available."""
    a = _adapter()
    forms = [a["invocation"]["argv"], *(p["argv"] for p in (a.get("office_profiles") or {}).values())]
    for argv in forms:
        assert "--approve" not in argv, argv
        assert "--no-approve" in argv and "--no-extensions" in argv and "--no-mcp" in argv, argv
        assert "--no-session" in argv, argv
        assert "--no-context-files" not in argv and "-nc" not in argv, argv
    assert a["safe_prompt_passing"]["shell"] is False
    assert a["invocation"]["prompt_transport"] == "stdin"
    worker = a["office_profiles"]["worker"]
    assert "{model}" in worker["argv"] and "{effort}" in worker["argv"]
    assert worker["prompt"] == "stdin" and worker.get("max_minutes"), "headless pi has no timeout of its own"


def test_pi_herdr_form_pins_model_and_keeps_the_trust_posture():
    """Visible delegation (Herdr) needs a pane-hosted form; without one the
    launch silently degrades to headless and the printed start command is bare."""
    worker = _adapter()["office_profiles"]["worker"]
    assert worker.get("herdr_kind") == "pi"
    inter = worker.get("interactive") or {}
    assert "--approve" not in inter.get("argv", []), inter
    for flag in ("--no-approve", "--no-extensions", "--no-mcp"):
        assert flag in inter["argv"], inter
    assert "--print" not in inter["argv"], "the pane form is interactive, not a headless print run"
    args, kind = adapters.interactive_argv(_adapter(), "worker", model=MIMO_FLASH, effort="medium",
                                           cwd=Path("/tmp/wt"))
    assert kind == "pi" and args[0] != "pi"
    assert args[args.index("--model") + 1] == MIMO_FLASH, args
    assert args[args.index("--thinking") + 1] == "medium", args


def test_no_seed_adapter_argv_passes_approve():
    for name, a in adapters.load_all().items():
        forms = [(a.get("invocation") or {}).get("argv") or [],
                 *(p.get("argv") or [] for p in (a.get("office_profiles") or {}).values())]
        for argv in forms:
            assert "--approve" not in argv, f"{name} grants project-resource trust: {argv}"


def test_pi_preflight_validates_the_pinned_slug_and_credentials():
    spec = _adapter().get("preflight") or {}
    assert spec.get("model_check"), "pi must check its model list before launch"
    auth = spec.get("auth_check")
    assert auth and "{model}" in auth, auth


# ---------------------------------------------------------------- catalog rows

def test_mimo_rows_pin_the_provider_and_claim_no_conformance():
    rows = {r["model_id"]: r for r in _rows() if str(r["model_id"]).startswith("mimo")}
    assert set(rows) == {"mimo-v2.6-pro", "mimo-v2.6-flash"}, set(rows)
    for model_id, row in rows.items():
        # pi's documented provider/id syntax: never an ambiguous global default.
        assert row["invocation_model_id"] == f"xiaomi-token-plan-sgp/{model_id}"
        assert row["dispatchable"] is True and row["effort"] == "medium"
        source = row["invocation_source"]
        assert source.startswith("local-evidence:")
        assert f"xiaomi-token-plan-sgp/{model_id}" in source
        # Exit 0 is launch evidence, not model conformance: no readback yet.
        assert "not model conformance" in source
    assert _adapter()["conformance"] == {"deterministic": "pending", "live": "pending"}


def test_deprecated_pi_gpt5codex_row_is_disabled_and_not_a_candidate(monkeypatch):
    row = next(r for r in _rows() if r["model_id"] == "gpt-5-codex")
    assert row["dispatchable"] is False
    monkeypatch.setattr(adapters, "installed", lambda a: True)
    monkeypatch.setattr(adapters, "harness_version", lambda a: "1.1.0")
    cands, _ = candidates.build_candidates(None, "executor", probe=False)
    assert not [c for c in cands if c["model_id"] == "gpt-5-codex"], "deprecated route reached candidate selection"
    assert [c for c in cands if c["model_id"] == "mimo-v2.6-pro"]


# ---------------------------------------------------------------- preflight validation

def _fake_pi_script(directory: Path) -> Path:
    """A pi stand-in serving --version, --list-models, auth check and --print."""
    script = directory / "pi"
    script.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import os, sys
        args = sys.argv[1:]
        if "--version" in args or args[:1] == ["-v"]:
            print("pi 1.1.0"); raise SystemExit(0)
        if "--list-models" in args:
            print("provider model context max-out thinking images")
            for line in os.environ.get("FAKE_PI_MODELS", "{MIMO_PRO} 1.0M 131.1K yes yes\\n"
                                                        "{MIMO_FLASH} 1.0M 131.1K yes yes").replace("/", " ").splitlines():
                print(line)
            raise SystemExit(0)
        if args[:1] == ["auth"]:
            raise SystemExit(int(os.environ.get("FAKE_PI_AUTH_EXIT", "0")))
        os.environ["FAKE_HARNESS"] = "pi"
        import runpy
        sys.argv[0] = {str(script)!r}
        runpy.run_path({str(Path(fake_agent.__file__))!r}, run_name="__main__")
        """))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


@pytest.fixture()
def pi_on_path(tmp_path, monkeypatch):
    _fake_pi_script(tmp_path)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    return tmp_path


def _install_pi(env):
    """Give an Env the fake pi harness, as conftest gives it codex/claude/gemini/agy."""
    script = _fake_pi_script(env.bin)
    env.fakes[script] = script.read_text()
    return script


def test_model_listing_matches_provider_qualified_slugs():
    listed = "provider model ctx\nxiaomi-token-plan-sgp  mimo-v2.6-flash  1.0M\n"
    assert adapters._model_listed(listed, MIMO_FLASH)
    assert adapters._model_listed(f"{MIMO_FLASH}\n", MIMO_FLASH)
    assert adapters._model_listed(listed, "mimo-v2.6-flash")
    assert not adapters._model_listed(listed, "xiaomi-token-plan-sgp/mimo-v2.6-pro")
    assert not adapters._model_listed(listed, "wrong-provider/mimo-v2.6-flash")
    assert not adapters._model_listed("", MIMO_FLASH)


def test_supported_provider_model_and_auth_pass_preflight(pi_on_path):
    ok, detail = adapters.run_preflight(_adapter(), MIMO_FLASH)
    assert ok, detail


def test_missing_model_fails_preflight_clearly(pi_on_path, monkeypatch):
    monkeypatch.setenv("FAKE_PI_MODELS", f"{MIMO_PRO} 1.0M 131.1K yes yes")
    ok, detail = adapters.run_preflight(_adapter(), MIMO_FLASH)
    assert not ok and MIMO_FLASH in detail and "model list" in detail, detail


def test_missing_credentials_fail_preflight_clearly(pi_on_path, monkeypatch):
    monkeypatch.setenv("FAKE_PI_AUTH_EXIT", "3")
    ok, detail = adapters.run_preflight(_adapter(), MIMO_FLASH)
    assert not ok and "auth_check exited 3" in detail, detail


def test_missing_provider_fails_preflight_clearly(pi_on_path, monkeypatch):
    monkeypatch.setenv("FAKE_PI_MODELS", f"{MIMO_FLASH} 1.0M 131.1K yes yes")
    ok, detail = adapters.run_preflight(_adapter(), "wrong-provider/mimo-v2.6-flash")
    assert not ok and "wrong-provider/mimo-v2.6-flash" in detail, detail


# ---------------------------------------------------------------- role and capability gates

def test_a_builder_only_route_is_not_a_reviewer_or_planner_override():
    with pytest.raises(Refused) as exc:
        candidates.declared_decision(f"pi/{MIMO_FLASH}@medium", flag="--review-as",
                                     role="code_reviewer", config=REVIEW_CAPS)
    assert "declares capabilities ['builder']" in str(exc.value) and "['review']" in str(exc.value), exc.value
    with pytest.raises(Refused):
        candidates.declared_decision(f"pi/{MIMO_FLASH}@medium", flag="--as",
                                     role="planner", config=PLANNER_CAPS)
    decision = candidates.declared_decision(f"pi/{MIMO_FLASH}@medium", flag="--as",
                                            role="executor", config=EXECUTOR_CAPS)
    assert decision["candidate"]["invocation_model_id"] == MIMO_FLASH
    assert decision["override"] is True


def test_routed_candidates_fail_the_capability_stage_for_planner(monkeypatch):
    monkeypatch.setattr(adapters, "installed", lambda a: True)
    monkeypatch.setattr(adapters, "harness_version", lambda a: "1.1.0")
    cands, _ = candidates.build_candidates(None, "planner", probe=False)
    pi = [c for c in cands if c["harness"] == "pi"]
    assert pi, "pi rows should be candidates before role filtering"
    result = routing.route({"role": "planner", "required_capabilities": ["planning"],
                           "candidates": pi, "policy": {}})
    assert result["selected"] is None and result["status"] == "no_qualifying_candidate"
    assert any("missing capabilities ['planning']" in r["reason"] for r in result["rejected"]), result["rejected"]


def test_setup_offers_pi_only_for_roles_it_can_serve():
    seeds = [{"model_id": "mimo-v2.6-flash", "harness": "pi", "effort": "medium"}]
    problems = configcmd.validate_seed(seeds, role="planner", config=PLANNER_CAPS)
    assert problems and "cannot serve role planner" in problems[0], problems
    problems = configcmd.validate_seed(seeds, role="code_reviewer", config=REVIEW_CAPS)
    assert problems and "cannot serve role code_reviewer" in problems[0], problems
    assert configcmd.validate_seed(seeds, role="executor", config=EXECUTOR_CAPS) == []
    assert configcmd.validate_seed(seeds) == []
    for role, config in (("planner", PLANNER_CAPS), ("code_reviewer", REVIEW_CAPS)):
        assert not [r for r in configcmd.known_routes(role, config=config) if r.startswith("pi/")], role
    assert [r for r in configcmd.known_routes(None) if r.startswith("pi/mimo-v2.6-flash")]


def test_setup_never_offers_the_deprecated_route():
    assert not [r for r in configcmd.known_routes(None) if "gpt-5-codex" in r]
    problems = configcmd.validate_seed([{"model_id": "gpt-5-codex", "harness": "pi", "effort": "high"}])
    assert problems and "not dispatchable" in problems[0], problems


# ---------------------------------------------------------------- launch behavior

PASS = {"reply": "VERDICT: APPROVED\nNEXT proceed"}  # the lane reviewer (#337)


def _approved(env):
    _install_pi(env)
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], convergence_reviewer=[PASS])


def _dispatch_as_pi(env, **extra):
    return env.office("dispatch", "T1", "--as", f"pi/{MIMO_FLASH}@medium", env={**extra}, check=0)


def _dispatch_row(env):
    con = env.con()
    try:
        return dict(con.execute("SELECT * FROM dispatches WHERE task_id='T1' ORDER BY started_at DESC").fetchone())
    finally:
        con.close()


def _log(env, d) -> str:
    return Path(d["log_path"]).read_text(errors="replace")


def test_supported_provider_model_launches_the_pinned_slug(env):
    _approved(env)
    code, out = _dispatch_as_pi(env)
    assert f"pi@1/mimo-v2.6-flash@medium" in out, out
    d = _dispatch_row(env)
    # The dispatch records the provider-pinned invocation slug, not the bare id.
    assert d["model"] == MIMO_FLASH and d["terminal_classification"] == "success", d


def test_missing_model_stops_the_launch_before_the_harness_runs(env):
    _approved(env)
    code, out = _dispatch_as_pi(env, FAKE_PI_MODELS=f"{MIMO_PRO} 1.0M 131.1K yes yes")
    d = _dispatch_row(env)
    assert d["terminal_classification"] == "preflight_failed" and d["exit_code"] == 127, d
    assert d["status"] == "failed"
    log = _log(env, d)
    assert "launch stopped before the harness started" in log and MIMO_FLASH in log, log


def test_missing_credentials_stop_the_launch_with_the_reason(env):
    _approved(env)
    code, out = _dispatch_as_pi(env, FAKE_PI_AUTH_EXIT="2")
    d = _dispatch_row(env)
    assert d["terminal_classification"] == "preflight_failed", d
    assert "auth_check exited 2" in _log(env, d)


def test_launch_nonzero_is_classified_with_the_harness_message_kept(env):
    _approved(env)
    env.script(executor=[{"exit": 1, "stderr": 'Error: Model "mimo" not found. Use --list-models to see available models.'}])
    code, out = _dispatch_as_pi(env)
    d = _dispatch_row(env)
    assert d["terminal_classification"] == "nonzero" and d["exit_code"] == 1, d
    assert 'Model "mimo" not found' in _log(env, d)


def test_launch_timeout_is_classified_and_stops_the_agent(env):
    _approved(env)
    env.script(executor=[{"sleep": 30}])
    code, out = _dispatch_as_pi(env, OFFICE_WORKER_MAX_MINUTES="0.02")
    d = _dispatch_row(env)
    assert d["terminal_classification"] == "timeout", d
    assert "wall-clock cap" in _log(env, d)


def test_exit_zero_with_no_output_is_flagged_not_passed_off_as_success(env):
    _approved(env)
    env.script(executor=[{"exit": 0}])
    code, out = _dispatch_as_pi(env)
    d = _dispatch_row(env)
    assert d["terminal_classification"] == "success", d
    assert "exited 0 with no output" in _log(env, d)
    assert task_row(env)["status"] != "accepted"


def test_wrong_model_output_is_not_recorded_as_conformance(env):
    """Exit 0 with wrong-model output is a declared failure signature until a
    model identity readback exists; the run never promotes the route for it."""
    sigs = {s["id"] for s in _adapter()["failure_signatures"]}
    assert "custom-model-id-served-as-something-else" in sigs
    assert _adapter()["conformance"] == {"deterministic": "pending", "live": "pending"}
    _approved(env)
    env.script(executor=[{"reply": "I am some other model entirely.", "exit": 0}])
    con = env.con()
    try:
        before = con.execute("SELECT COUNT(*) FROM adapter_trust_acts").fetchone()[0]
    finally:
        con.close()
    code, out = _dispatch_as_pi(env)
    con = env.con()
    try:
        after = con.execute("SELECT COUNT(*) FROM adapter_trust_acts").fetchone()[0]
    finally:
        con.close()
    assert after == before, "no launch outcome may promote route trust"
