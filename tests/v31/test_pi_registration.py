"""#479 registration path for the pi harness and the Xiaomi MiMo-V2.6 rows.

What these tests hold to the review's bar: the worker form never grants
project-resource trust (--approve); provider/model are pinned and validated
before launch (missing provider/model or credentials fail clearly, never a
silent wrong-model dispatch); pi is not a planner or visual route at routing,
at setup or at dispatch, and serves code/plan review only through a read-only
tool allowlist plus Office's worktree-unchanged check; and the deprecated pi/gpt-5-codex row stays
out of candidate selection. Fake `pi` binaries and scenarios stand in for the
harness — no test here calls a real model.
"""
from __future__ import annotations

import copy
import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest

import fake_agent
from conftest import GOOD_ADD, approved_run, enable_decommissioned_mimo, retired_mimo_rows, task_row

from office import adapters, candidates, configcmd, routing
from office.state import Refused

PI = "pi"
MIMO_FLASH = "xiaomi-token-plan-sgp/mimo-v2.6-flash"
LAUNCH_EFFORT = "low"  # the MiMo rows are retired (#786); launch tests use a user-catalog copy of the archived row at this effort
MIMO_PRO = "xiaomi-token-plan-sgp/mimo-v2.6-pro"
EXECUTOR_CAPS = {"roles": {"executor": {"required_capabilities": ["builder"]}}}
PLANNER_CAPS = {"roles": {"planner": {"required_capabilities": ["planning"]}}}
REVIEW_CAPS = {"roles": {"code_reviewer": {"required_capabilities": ["review"]}}}


def _adapter() -> dict:
    return adapters.load_all()["pi"]


def _rows() -> list[dict]:
    return [r for r in candidates.catalog_rows() if r.get("invocation_harness") == "pi"]


@pytest.fixture(autouse=True)
def _isolate_user_catalog(tmp_path, monkeypatch):
    """Keep operator-specific user config and catalog choices out of seed tests."""
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("OFFICE_USER_CATALOG", str(tmp_path / "catalog.yaml"))


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

def test_archived_mimo_rows_pin_the_provider_and_claim_no_conformance():
    """The retired (#786) pi MiMo rows live only in the archive; the shipped catalog has none."""
    assert not [r for r in _rows() if str(r["model_id"]).startswith("mimo")]
    rows = {r["model_id"]: r for r in retired_mimo_rows("pi")}
    assert set(rows) == {"mimo-v2.6-pro", "mimo-v2.6-flash"}, set(rows)
    for model_id, row in rows.items():
        # pi's documented provider/id syntax: never an ambiguous global default.
        assert row["invocation_model_id"] == f"xiaomi-token-plan-sgp/{model_id}"
        assert row["dispatchable"] is False and row["effort"] == "medium"
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
    assert not [c for c in cands if c["model_id"].startswith("mimo")], "decommissioned MiMo reached candidate selection"
    # Positive control: a dispatchable user-catalog copy of the same row does reach selection.
    enable_decommissioned_mimo("pi", "mimo-v2.6-flash")
    cands, _ = candidates.build_candidates(None, "executor", probe=False)
    assert [c for c in cands if c["harness"] == "pi" and c["model_id"] == "mimo-v2.6-flash"]


def test_mimo_is_excluded_from_every_routing_pool(monkeypatch):
    """#786: the retired MiMo rows are not in the shipped catalog, so no role can route to them."""
    monkeypatch.setattr(adapters, "installed", lambda a: True)
    monkeypatch.setattr(adapters, "harness_version", lambda a: "9.9.9")
    assert not [r for r in candidates.catalog_rows() if str(r["model_id"]).startswith("mimo")]
    for role in ("executor", "worker", "planner", "code_reviewer"):
        cands, _ = candidates.build_candidates(None, role, probe=False)
        assert not [c for c in cands if str(c["model_id"]).startswith("mimo")], role
    assert not [r for r in configcmd.known_routes("executor") if "mimo" in r]


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

def test_pi_is_a_reviewer_override_but_not_a_planner_override():
    decision = candidates.declared_decision(f"pi/{MIMO_FLASH}@medium", flag="--review-as",
                                            role="code_reviewer", config=REVIEW_CAPS)
    assert decision["candidate"]["invocation_model_id"] == MIMO_FLASH
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
    # The only pi row is the deprecated gpt-5-codex: not a candidate.
    assert not [c for c in cands if c["harness"] == "pi"]
    # The capability stage itself is still enforced on a dispatchable user-catalog copy of the MiMo row.
    enable_decommissioned_mimo("pi", "mimo-v2.6-flash")
    cands, _ = candidates.build_candidates(None, "planner", probe=False)
    pi = [c for c in cands if c["harness"] == "pi"]
    assert pi, "the user-catalog copy should be a candidate before role filtering"
    result = routing.route({"role": "planner", "required_capabilities": ["planning"],
                           "candidates": pi, "policy": {}})
    assert result["selected"] is None and result["status"] == "no_qualifying_candidate"
    assert any("missing capabilities ['planning']" in r["reason"] for r in result["rejected"]), result["rejected"]


def test_setup_offers_pi_only_for_roles_it_can_serve():
    seeds = [{"model_id": "mimo-v2.6-flash", "harness": "pi", "effort": "medium"}]
    # MiMo is retired (#786): setup refuses it for every role and never offers it.
    for role, config in (("planner", PLANNER_CAPS), ("code_reviewer", REVIEW_CAPS), ("executor", EXECUTOR_CAPS),
                         (None, None)):
        problems = configcmd.validate_seed(seeds, role=role, config=config)
        assert problems and "unknown model" in problems[0], (role, problems)
        assert not [r for r in configcmd.known_routes(role, config=config) if r.startswith("pi/")], role
    # With a dispatchable user-catalog copy of the archived row the role gate applies again: builder-only pi never serves planner or reviewer.
    enable_decommissioned_mimo("pi", "mimo-v2.6-flash", "medium")
    problems = configcmd.validate_seed(seeds, role="planner", config=PLANNER_CAPS)
    assert problems and "cannot serve role planner" in problems[0], problems
    assert configcmd.validate_seed(seeds, role="code_reviewer", config=REVIEW_CAPS) == []
    assert configcmd.validate_seed(seeds, role="executor", config=EXECUTOR_CAPS) == []
    assert not [r for r in configcmd.known_routes("planner", config=PLANNER_CAPS) if r.startswith("pi/")]
    assert [r for r in configcmd.known_routes("executor", config=EXECUTOR_CAPS) if r.startswith("pi/mimo-v2.6-flash")]


def test_setup_never_offers_the_deprecated_route():
    assert not [r for r in configcmd.known_routes(None) if "gpt-5-codex" in r]
    problems = configcmd.validate_seed([{"model_id": "gpt-5-codex", "harness": "pi", "effort": "high"}])
    assert problems and "not dispatchable" in problems[0], problems


# ---------------------------------------------------------------- launch behavior

PASS = {"reply": "VERDICT: APPROVED\nNEXT proceed"}  # the lane reviewer (#337)


def _approved(env):
    _install_pi(env)
    enable_decommissioned_mimo("pi", "mimo-v2.6-flash", LAUNCH_EFFORT)
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], convergence_reviewer=[PASS])


def _dispatch_as_pi(env, **extra):
    return env.office("dispatch", "T1", "--as", f"pi/{MIMO_FLASH}@{LAUNCH_EFFORT}", env={**extra}, check=0)


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
    assert f"pi@1/mimo-v2.6-flash@{LAUNCH_EFFORT}" in out, out
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


# ---------------------------------------------------------------- reviewer profile (read-only posture)

def test_pi_reviewer_profile_is_a_read_only_allowlist_for_code_and_plan_review_only():
    adapter = _adapter()
    assert adapters.profile(adapter, "vision") is None, "pi is never a visual reviewer"
    prof = adapters.profile(adapter, "reviewer")
    argv = prof["argv"]
    assert argv[argv.index("--tools") + 1] == "read,grep,find,ls"
    assert not {"edit", "write", "bash"} & set(argv[argv.index("--tools") + 1].split(","))
    for flag in ("--print", "--no-approve", "--no-extensions", "--no-mcp", "--no-session"):
        assert flag in argv, flag
    assert "--approve" not in argv
    assert prof["prompt"] == "stdin" and prof["output"] == "stdout"
    assert prof["verify_worktree_unchanged"] is True
    assert "herdr_kind" not in prof and "interactive" not in prof, "no pane form: the headless allowlist is the only launch"
    assert argv[len(argv) - len(_adapter()["office_profiles"]["worker"]["argv"]):] == _adapter()["office_profiles"]["worker"]["argv"]
    assert "review" in adapter["capabilities"] and "planning" not in adapter["capabilities"]


def _git_repo(tmp_path):
    import subprocess
    repo = tmp_path / "wt"
    repo.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    (repo / "a.txt").write_text("one\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
    return repo


@pytest.mark.parametrize("mutate,expected", [
    (lambda r: None, None),
    (lambda r: (r / "a.txt").write_text("two\n"), "tracked diff"),
    (lambda r: (r / "new.txt").write_text("x"), "untracked files"),
    (lambda r: __import__("subprocess").run(["git", "-C", str(r), "commit", "-qam", "x", "--allow-empty"], check=True)
     or None, "HEAD"),
])
def test_worktree_guard_reports_head_diff_and_untracked_changes(tmp_path, mutate, expected):
    from office import gates
    repo = _git_repo(tmp_path)
    guard = gates._worktree_guard({"harness": "pi"}, "reviewer", repo)
    before = guard(None)
    mutate(repo)
    changed = guard(before)
    if expected is None:
        assert changed is None
    else:
        assert expected in changed, changed


def test_worktree_guard_sees_an_edit_to_an_already_untracked_file(tmp_path):
    from office import gates
    repo = _git_repo(tmp_path)
    (repo / "scratch.txt").write_text("a")
    guard = gates._worktree_guard({"harness": "pi"}, "reviewer", repo)
    before = guard(None)
    (repo / "scratch.txt").write_text("b")
    assert "untracked files" in guard(before)


def test_worktree_guard_is_off_for_harnesses_with_their_own_sandbox(tmp_path):
    from office import gates
    repo = _git_repo(tmp_path)
    assert gates._worktree_guard({"harness": "codex"}, "reviewer", repo) is None
    assert gates._worktree_guard({"harness": "pi"}, "worker", repo) is None


# ---------------------------------------------------------------- registering a reviewer is as easy as an executor

def _effective(role, monkeypatch):
    from office import routecmd
    monkeypatch.setattr(adapters, "installed", lambda a: True)
    monkeypatch.setattr(adapters, "harness_version", lambda a: "1.1.0")
    return "\n".join(routecmd.policy(role=role, effective=True).lines)


def test_one_command_registers_pi_luna_high_as_code_and_plan_reviewer(monkeypatch):
    """The worked example: `office model add pi/openai/gpt-6-luna --effort high --role reviewer`, no adapter edit."""
    from office import harnesscmd
    result = harnesscmd.model_add("pi/openai/gpt-6-luna", ["high"], model_id="gpt-6-luna", roles=["reviewer"])
    assert result.data["added"] == ["pi/gpt-6-luna@high"]
    assert result.data["allowed_roles"] == ["code_reviewer", "plan_reviewer"]
    assert "read-only tools only" in "\n".join(result.lines)
    for role in ("code_reviewer", "plan_reviewer"):
        assert "pi/gpt-6-luna@high: allow (user) -> dispatches (ready)" in _effective(role, monkeypatch), role
    for role in ("executor", "integration_reviewer", "closeout_verifier"):
        assert "pi/gpt-6-luna@high: allow" not in _effective(role, monkeypatch), role
    cands, _ = candidates.build_candidates(None, "code_reviewer", probe=False)
    assert [c for c in cands if c["harness"] == "pi" and "gpt-6-luna" in str(c)], "the new route is a review candidate"


def test_role_executor_allows_the_same_rows_for_executor_only(monkeypatch):
    from office import harnesscmd
    harnesscmd.model_add("pi/openai/gpt-6-luna", ["high"], model_id="gpt-6-luna", roles=["executor"])
    assert "pi/gpt-6-luna@high: allow (user) -> dispatches (ready)" in _effective("executor", monkeypatch)
    assert "pi/gpt-6-luna@high: allow" not in _effective("code_reviewer", monkeypatch)


def _user_adapter(tmp_path, monkeypatch, *, exe, args, caps=("builder",), worker_argv=None, extra=None, worker_extra=None):
    import yaml
    adir = tmp_path / "adapters"
    adir.mkdir(exist_ok=True)
    (adir / "acme.yaml").write_text(yaml.safe_dump({
        "id": "acme", "invocation": {"executable": exe}, "capabilities": list(caps),
        "effort_mapping": {"high": "high"}, "reviewer_readonly_args": args, **(extra or {}),
        "office_profiles": {"worker": {"argv": worker_argv or ["--print", "--no-approve", "--model", "{model}", "--thinking", "{effort}"],
                                       "prompt": "stdin", "output": "stdout", "herdr_kind": "acme",
                                       "interactive": {"argv": ["--model", "{model}"]}, **(worker_extra or {})}}}))
    monkeypatch.setattr(adapters, "user_adapter_files", lambda: sorted(adir.glob("*.yaml")))
    return adapters.load_all()["acme"]


def test_a_user_adapter_for_a_vetted_harness_gets_a_derived_read_only_reviewer(tmp_path, monkeypatch):
    from office import harnesscmd
    acme = _user_adapter(tmp_path, monkeypatch, exe="pi", args=["--tools", "read,grep"])
    prof = adapters.profile(acme, "reviewer")
    assert prof["argv"] == ["--tools", "read,grep", "--print", "--no-approve", "--model", "{model}", "--thinking", "{effort}"]
    assert prof["verify_worktree_unchanged"] is True and "interactive" not in prof and "herdr_kind" not in prof
    assert "review" in acme["capabilities"] and adapters.profile(acme, "vision") is None
    result = harnesscmd.model_add("acme/vendor/m1", ["high"], roles=["reviewer"])
    assert result.data["allowed_roles"] == ["code_reviewer", "plan_reviewer"]


@pytest.mark.parametrize("exe,args", [
    ("acme", ["--read-only"]),                      # an unvetted harness: its own text is never trusted
    ("pi", ["--tools", "read,edit"]),               # a write tool
    ("pi", ["--tools", "read,bash"]),               # bash cannot be limited to read-only
    ("pi", ["--tools", "read", "--approve"]),       # extra flags ride along
    ("pi", ["--yolo", "--tools", "read"]),
    ("pi", ["--tools", ""]),
])
def test_unvetted_or_write_capable_reviewer_args_derive_nothing_and_fail_validation(tmp_path, monkeypatch, exe, args):
    from office import harnesscmd
    acme = _user_adapter(tmp_path, monkeypatch, exe=exe, args=args)
    assert adapters.profile(acme, "reviewer") is None, "no reviewer profile is derived"
    assert "review" not in acme["capabilities"], "the review capability is not granted"
    assert adapters.readonly_args_problem(acme)
    with pytest.raises(Exception) as exc:
        harnesscmd.model_add("acme/vendor/m1", ["high"], roles=["reviewer"])
    assert "no reviewer launch profile" in str(exc.value)


def test_an_explicit_reviewer_profile_is_never_overridden_by_the_derived_form():
    adapter = {"office_profiles": {"worker": {"argv": ["w"]}, "reviewer": {"argv": ["mine"]}},
               "reviewer_readonly_args": ["--ro"]}
    assert adapters.derive_reviewer_profile(adapter) is None


def test_role_reviewer_is_refused_for_a_harness_without_read_only_args_or_profile():
    from office import harnesscmd
    from office.state import Refused  # noqa: F401
    with pytest.raises(Exception) as exc:
        harnesscmd.model_add("kilo/vendor/m1", ["high"], roles=["reviewer"])
    assert "no reviewer launch profile" in str(exc.value) and "reviewer_readonly_args" in str(exc.value)
    assert not [r for r in _rows() if r.get("invocation_harness") == "kilo" and r.get("model_id") == "m1"], \
        "a refused registration writes nothing"
    with pytest.raises(Exception) as exc:
        harnesscmd.model_add("pi/openai/gpt-6-luna", ["high"], roles=["planner"])
    assert "--role takes" in str(exc.value)


def test_worktree_guard_fails_closed_outside_a_git_checkout(tmp_path):
    from office import gates
    plain = tmp_path / "plain"
    plain.mkdir()
    guard = gates._worktree_guard({"harness": "pi"}, "reviewer", plain)
    assert "could not be fingerprinted" in guard(guard(None))


def test_a_failed_policy_write_rolls_the_registration_back(monkeypatch):
    from office import harnesscmd, routecmd
    real = routecmd.change
    calls = []

    def flaky(action, **kw):
        calls.append((action, kw["role"]))
        if action == "allow" and kw["role"] == "plan_reviewer":
            raise RuntimeError("user routing config must be a mapping")
        return real(action, **kw)

    monkeypatch.setattr(routecmd, "change", flaky)
    with pytest.raises(RuntimeError):
        harnesscmd.model_add("pi/openai/gpt-6-luna", ["high"], model_id="gpt-6-luna", roles=["reviewer"])
    assert ("clear", "code_reviewer") in calls
    assert not [r for r in _rows() if r.get("model_id") == "gpt-6-luna"], "the catalog rows were rolled back"
    monkeypatch.setattr(routecmd, "change", real)
    assert "pi/gpt-6-luna@high: allow" not in "\n".join(routecmd.policy(role="code_reviewer", effective=True).lines)


# ---------------------------------------------------------------- end to end: a pi reviewer through gates.run_reviewer

LUNA = "pi/openai/gpt-6-luna@high"
PI_LUNA_ENV = {"FAKE_PI_MODELS": "openai/gpt-6-luna 1.0M 131.1K yes yes"}


def _pi_review(env, reviewer, *, review_as=True):
    """A codex executor, then a code review pinned to the registered pi/gpt-6-luna@high reviewer."""
    from office import harnesscmd
    _install_pi(env)
    harnesscmd.model_add("pi/openai/gpt-6-luna", ["high"], model_id="gpt-6-luna", roles=["reviewer"])
    approved_run(env, executor=[{"write": {"calc.py": GOOD_ADD}, "submit": True}], convergence_reviewer=reviewer)
    args = ["dispatch", "T1", "--as", "codex/gpt-6-luna@high"]
    if review_as:
        args += ["--review-as", LUNA]
    return env.office(*args, env=PI_LUNA_ENV, check=0)


def _events(env, name):
    con = env.con()
    try:
        return [dict(r) for r in con.execute("SELECT * FROM events WHERE kind=?", (name,)).fetchall()]
    finally:
        con.close()


def _pi_reviews(env):
    return [c for c in env.calls() if c.get("role") == "convergence_reviewer" and c.get("harness") == "pi"]


@pytest.mark.approved
def test_a_pi_reviewer_launches_with_the_read_only_tool_allowlist_and_its_reply_is_ingested(env):
    _pi_review(env, [PASS])
    calls = _pi_reviews(env)
    assert calls, env.calls()
    argv = calls[0]["argv"]
    assert argv[argv.index("--tools") + 1] == "read,grep,find,ls", argv
    assert "--print" in argv and "--no-approve" in argv and "--approve" not in argv
    assert not _events(env, "review.worktree_changed")
    con = env.con()
    gate = dict(con.execute("SELECT * FROM gates WHERE kind='convergence_review' ORDER BY created_at DESC").fetchone())
    assert gate["verdict"] == "APPROVED" and "pi" in (gate["route"] or ""), gate


@pytest.mark.approved
def test_a_pi_reviewer_that_changes_the_worktree_is_degraded_and_its_verdict_is_not_used(env):
    _pi_review(env, [{"write": {"planted.txt": "the reviewer wrote this"}, "reply": "VERDICT: APPROVED\nNEXT proceed"}])
    assert _events(env, "review.worktree_changed"), "the change is recorded"
    con = env.con()
    gate = dict(con.execute("SELECT * FROM gates WHERE kind='convergence_review' ORDER BY created_at DESC").fetchone())
    assert gate["verdict"] != "APPROVED", gate
    d = dict(con.execute("SELECT * FROM dispatches WHERE role='code_reviewer' ORDER BY started_at DESC").fetchone())
    assert d["outcome"] == "environment_failure", d


def _gate_and_events(env):
    con = env.con()
    try:
        gate = dict(con.execute("SELECT * FROM gates WHERE kind='convergence_review' ORDER BY created_at DESC").fetchone())
        kinds = [r[0] for r in con.execute("SELECT kind FROM events").fetchall()]
    finally:
        con.close()
    return gate, kinds


@pytest.mark.approved
@pytest.mark.parametrize("scripted", [
    pytest.param([{"exit": 0}, {"exit": 0}, {"exit": 0}], id="empty-reply"),
    pytest.param([{"reply": "looks fine to me"}, {"reply": "still fine"}, {"reply": "ok"}], id="malformed-reply"),
])
def test_a_pi_reviewer_with_no_usable_reply_ends_the_review_unavailable_not_approved(env, scripted):
    """pi has no resumable session, so there is no reprompt: the one pi review is read, found unusable, and the
    gate records the review as unavailable instead of approving or inventing a verdict."""
    _pi_review(env, scripted)
    assert len(_pi_reviews(env)) == 1, "one pi review ran, with no silent retry on another route"
    gate, kinds = _gate_and_events(env)
    assert gate["verdict"] != "APPROVED", gate
    assert "convergence.unavailable" in kinds and "gate.approved" in kinds[:kinds.index("convergence.unavailable")], kinds
    assert not any(k == "review.worktree_changed" for k in kinds)
    d = _dispatch_for(env, "code_reviewer")
    assert d["harness"] == "pi" and d["status"] == "exited", d


def _dispatch_for(env, role):
    con = env.con()
    try:
        return dict(con.execute("SELECT * FROM dispatches WHERE role=? ORDER BY started_at DESC", (role,)).fetchone())
    finally:
        con.close()


def _pi_plan_review(env, plan_reviewer):
    """A full-gear run whose plan review is routed to the registered pi/gpt-6-luna@high reviewer."""
    from office import harnesscmd
    from test_compat_hooks_install import PLAN_ONE
    _install_pi(env)
    env.trust()
    harnesscmd.model_add("pi/openai/gpt-6-luna", ["high"], model_id="gpt-6-luna", roles=["reviewer"])
    code, out = env.office("config", "roles.plan_reviewer.preferred_seed", "pi/gpt-6-luna@high", "--force", check=0)
    assert code == 0, out
    env.script(planner=[{"plan": PLAN_ONE, "submit": True}], plan_reviewer=plan_reviewer)
    return env.office("start", "add numbers", "--gear", "full", env=PI_LUNA_ENV, check=0)


@pytest.mark.approved
def test_a_pi_plan_review_runs_with_the_read_only_allowlist_and_its_verdict_is_ingested(env):
    code, out = _pi_plan_review(env, [PASS])
    calls = [c for c in env.calls() if c.get("role") == "plan_reviewer"]
    assert calls and calls[0]["harness"] == "pi", env.calls()
    argv = calls[0]["argv"]
    assert argv[argv.index("--tools") + 1] == "read,grep,find,ls", argv
    code, data = env.ojson("status")
    assert data["data"]["plan_review"]["ended"] is True, data
    assert "review.worktree_changed" not in _event_kinds(env)


@pytest.mark.approved
def test_a_pi_plan_reviewer_that_changes_the_checkout_is_degraded(env):
    _pi_plan_review(env, [{"write": {"planted.txt": "the plan reviewer wrote this"}, "reply": "VERDICT: APPROVED\nNEXT proceed"}])
    assert "review.worktree_changed" in _event_kinds(env), _event_kinds(env)


def _event_kinds(env):
    con = env.con()
    try:
        return [r[0] for r in con.execute("SELECT kind FROM events").fetchall()]
    finally:
        con.close()


# F1 regression: the whole derived reviewer argv is checked, not only the vetted prefix.
@pytest.mark.parametrize("worker_argv", [
    ["--print", "--tools", "edit", "--model", "{model}", "--thinking", "{effort}"],
    ["--print", "--tools=edit", "--model", "{model}", "--thinking", "{effort}"],
    ["--print", "-t", "write", "--model", "{model}", "--thinking", "{effort}"],
    ["--print", "--exclude-tools", "read", "--model", "{model}", "--thinking", "{effort}"],
    ["--print", "--approve", "--model", "{model}", "--thinking", "{effort}"],
])
def test_a_worker_argv_that_could_override_the_allowlist_derives_no_reviewer(tmp_path, monkeypatch, worker_argv):
    acme = _user_adapter(tmp_path, monkeypatch, exe="pi", args=["--tools", "read,grep"], worker_argv=worker_argv)
    assert adapters.profile(acme, "reviewer") is None
    assert "review" not in acme["capabilities"]
    assert "worker argv carries" in adapters.readonly_args_problem(acme)


# F1 round 3: assigned-session args, resume args and other worker fields can never widen a derived reviewer.

def test_an_adapter_that_assigns_session_ids_derives_no_reviewer(tmp_path, monkeypatch):
    acme = _user_adapter(tmp_path, monkeypatch, exe="pi", args=["--tools", "read,grep"],
                         extra={"session": {"id": "assigned", "assign_arg": ["--tools", "edit"]}})
    assert adapters.profile(acme, "reviewer") is None and "review" not in acme["capabilities"]
    assert "session.assign_arg" in adapters.readonly_args_problem(acme)


def test_worker_fields_are_never_copied_into_a_derived_reviewer(tmp_path, monkeypatch):
    acme = _user_adapter(tmp_path, monkeypatch, exe="pi", args=["--tools", "read,grep"],
                         worker_extra={"headless_resume_argv": ["--tools", "edit", "--resume", "{session_id}"],
                                       "include_arg": ["--tools", "edit"], "agent_file": {"format": "opencode", "name": "x"}})
    prof = adapters.profile(acme, "reviewer")
    assert set(prof) == {"argv", "prompt", "output", "verify_worktree_unchanged", "derived_readonly", "max_minutes"} \
        or set(prof) == {"argv", "prompt", "output", "verify_worktree_unchanged", "derived_readonly"}, prof
    assert adapters.headless_resume_args(acme, "reviewer", "S1") is None


@pytest.mark.parametrize("resume", [["--tools", "edit"], ["--tools=edit"], ["-t", "write"], ["--yolo"]])
def test_the_complete_rendered_reviewer_argv_is_validated_including_resume_args(tmp_path, resume):
    adapter = _adapter()
    ok, _ = adapters.build_argv(adapter, "reviewer", model="openai/gpt-6-luna", effort="high", cwd=tmp_path,
                                output=tmp_path / "reply.md")
    assert ok[1:3] == ["--tools", "read,grep,find,ls"]
    with pytest.raises(adapters.AdapterError):
        adapters.build_argv(adapter, "reviewer", model="openai/gpt-6-luna", effort="high", cwd=tmp_path,
                            output=tmp_path / "reply.md", resume_args=resume)


def test_a_session_assign_arg_appended_at_launch_is_refused_for_a_derived_reviewer(tmp_path):
    adapter = copy.deepcopy(_adapter())
    adapter["session"] = {"id": "assigned", "assign_arg": ["--tools", "edit"]}
    with pytest.raises(adapters.AdapterError):
        adapters.build_argv(adapter, "reviewer", model="openai/gpt-6-luna", effort="high", cwd=tmp_path,
                            output=tmp_path / "reply.md", session_id="S1")


def test_the_worker_form_is_unaffected_by_the_reviewer_argv_check(tmp_path):
    argv, _ = adapters.build_argv(_adapter(), "worker", model="openai/gpt-6-luna", effort="high", cwd=tmp_path)
    assert "--tools" not in argv and "--print" in argv
