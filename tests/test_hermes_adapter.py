"""The Hermes harness: launch argv, launch environment, readback, quota attribution, access and Herdr detection.

Fake binaries and fixtures stand in for Hermes; nothing here calls a model.
"""
from __future__ import annotations

import json
import os
import shlex
import sqlite3
import stat
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from office import adapters, dispatch, install, review_submit, transcripts
from office import hermes_access

CWD = Path("/w")
REPLY = Path("/d/reply.txt")
USAGE = Path("/d/usage.json")
SLUG = "anthropic:claude-sonnet-5-5"


@pytest.fixture(autouse=True)
def _seed_adapters_only(tmp_path, monkeypatch):
    """The seed hermes adapter, not whatever override the operator keeps in their config."""
    monkeypatch.setenv("OFFICE_USER_ADAPTERS", str(tmp_path / "no-user-adapters"))


@pytest.fixture()
def hermes():
    return adapters.load_all()["hermes"]


def worker(hermes, model=SLUG, effort="high", **kw):
    return adapters.build_argv(hermes, "worker", model=model, effort=effort, cwd=CWD, **kw)


# ---------------------------------------------------------------- argv (pinned)

def test_worker_headless_argv_is_pinned(hermes):
    argv, prof = worker(hermes, usage_file=USAGE)
    assert argv == ["hermes", "--ignore-user-config", "--provider", "anthropic", "--model", "claude-sonnet-5-5",
                    "--reasoning", "high", "--yolo", "--usage-file", str(USAGE)]
    # The prompt is the value of --oneshot (hermes -z takes no stdin), added by the supervisor as one argv element.
    assert prof["prompt"] == "argv-bound" and prof["prompt_flag"] == "--oneshot="


def test_a_bare_model_id_pins_the_model_and_drops_the_provider_flag(hermes):
    argv, _ = worker(hermes, model="claude-sonnet-5-5", usage_file=USAGE)
    assert "--provider" not in argv and argv[argv.index("--model") + 1] == "claude-sonnet-5-5"


def test_worker_interactive_argv_is_pinned(hermes):
    argv, kind = adapters.interactive_argv(hermes, "worker", model=SLUG, effort="high", cwd=CWD)
    assert kind == "hermes"
    assert argv == ["--cli", "--ignore-user-config", "--provider", "anthropic", "--model", "claude-sonnet-5-5",
                    "--reasoning", "high", "--yolo", "--in", str(CWD)]


def test_reviewer_is_restored_with_the_accepted_risk(hermes):
    """Requirement r3: Hermes is a default reviewer with full tools. --yolo and the terminal toolset can write the
    reviewed worktree; the post-run fingerprint detects that, and the source note records the accepted risk."""
    assert "review" in hermes["capabilities"]
    prof = adapters.profile(hermes, "reviewer")
    assert prof["submit"] == "shell" and prof["herdr_kind"] == "hermes" and prof["verify_worktree_unchanged"] is True
    assert "--in" not in prof["argv"] and "--toolsets" not in prof["argv"]
    assert "accepted risk" in hermes["source_notes"] and "cannot prevent" in hermes["source_notes"]
    assert "--accept-hooks" in hermes["trust_justifications"]


def test_reviewer_headless_argv_keeps_the_usage_readback(hermes):
    argv, prof = adapters.build_argv(hermes, "reviewer", model=SLUG, effort="high", cwd=CWD,
                                     usage_file=USAGE)
    assert argv == ["hermes", "--ignore-user-config", "--provider", "anthropic", "--model", "claude-sonnet-5-5",
                    "--reasoning", "high", "--yolo", "--usage-file", str(USAGE)]
    assert prof["readback"] == "hermes-usage" and prof["prompt_flag"] == "--oneshot="


def test_reviewer_interactive_argv_is_pinned(hermes):
    argv, kind = adapters.interactive_argv(hermes, "reviewer", model=SLUG, effort="high", cwd=CWD)
    assert kind == "hermes"
    assert argv == ["--cli", "--ignore-user-config", "--provider", "anthropic", "--model", "claude-sonnet-5-5",
                    "--reasoning", "high", "--yolo", "--accept-hooks", "--in", str(CWD)]


def test_reviewer_interactive_argv_pins_the_provider_and_ignores_user_config(hermes):
    """The pane reviewer must pin the route's provider and skip config.yaml's fallback_providers, as the executor does."""
    argv, _ = adapters.interactive_argv(hermes, "reviewer", model=SLUG, effort="high", cwd=CWD)
    assert "--ignore-user-config" in argv
    assert argv[argv.index("--provider") + 1] == "anthropic"
    assert argv[argv.index("--in") + 1] == str(CWD)  # the pane starts in the reviewed worktree, as the worker's does


@pytest.mark.parametrize("kind", ["worker", "reviewer", "vision"])
def test_every_profile_has_headless_and_interactive_forms_pinning_the_provider(hermes, kind):
    """Requirement r3: worker, reviewer and vision each launch headless and in a Herdr pane, both pinned to the route's
    provider and skipping config.yaml's fallback_providers."""
    prof = adapters.profile(hermes, kind)
    assert prof and prof["herdr_kind"] == "hermes" and prof["interactive"]["argv"]
    headless, _ = adapters.build_argv(hermes, kind, model=SLUG, effort="high", cwd=CWD, output=REPLY,
                                      usage_file=USAGE)
    pane, kind_ = adapters.interactive_argv(hermes, kind, model=SLUG, effort="high", cwd=CWD, output=REPLY)
    assert kind_ == "hermes"
    for argv in (headless, pane):
        assert "--ignore-user-config" in argv
        assert argv[argv.index("--provider") + 1] == "anthropic"


def test_vision_profile_is_restored_with_the_vision_toolset_and_dispatch_directory(hermes):
    """Restored from 7b2a3ffc^: images reach Hermes as paths in the prompt and are read with its vision tool; the
    agent works from the dispatch directory, and the worktree fingerprint still applies. Capability stays
    [builder, review]: vision is earned only by the image-sensitive conformance proof."""
    assert hermes["capabilities"] == ["builder", "review"]
    prof = adapters.profile(hermes, "vision")
    assert prof["image_transport"] == "prompt-path" and prof["verify_worktree_unchanged"] is True
    assert prof["submit"] == "shell" and prof["prompt_flag"] == "--oneshot=" and prof["readback"] == "hermes-usage"
    argv, _ = adapters.build_argv(hermes, "vision", model=SLUG, effort="medium", cwd=CWD, output=REPLY,
                                  usage_file=USAGE)
    assert argv == ["hermes", "--ignore-user-config", "--provider", "anthropic", "--model", "claude-sonnet-5-5",
                    "--reasoning", "medium", "--yolo", "--toolsets", "file,terminal,vision", "--in", str(REPLY.parent),
                    "--usage-file", str(USAGE)]
    pane, kind = adapters.interactive_argv(hermes, "vision", model=SLUG, effort="medium", cwd=CWD, output=REPLY)
    assert kind == "hermes"
    assert pane == ["--cli", "--ignore-user-config", "--provider", "anthropic", "--model", "claude-sonnet-5-5",
                    "--reasoning", "medium", "--yolo", "--accept-hooks", "--toolsets", "file,terminal,vision",
                    "--in", str(REPLY.parent)]


def test_vision_profile_drops_the_provider_flag_for_a_bare_model_id(hermes):
    """A bare model id pins only the model; the vision toolset stays on the argv."""
    argv, _ = adapters.build_argv(hermes, "vision", model="claude-sonnet-5-5", effort="medium", cwd=CWD, output=REPLY)
    assert "--provider" not in argv and "file,terminal,vision" in argv


@pytest.mark.parametrize("effort,flag", [("none", "none"), ("low", "low"), ("medium", "medium"), ("high", "high"),
                                         ("xhigh", "xhigh"), ("max", "max")])
def test_every_office_effort_maps_to_a_reasoning_value_hermes_accepts(hermes, effort, flag):
    accepted = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}  # hermes --reasoning, 0.23.3
    assert hermes["effort_mapping"][effort] == flag and flag in accepted
    argv, _ = worker(hermes, effort=effort)
    assert argv[argv.index("--reasoning") + 1] == flag


# ---------------------------------------------------------------- launch env, fallback providers

def test_launch_env_drops_every_hermes_variable_but_hermes_home(hermes):
    env = {"PATH": "/bin", "HERMES_HOME": "/h", "HERMES_INFERENCE_MODEL": "other", "HERMES_MODEL": "x",
           "HERMES_ACCEPT_HOOKS": "1", "OFFICE_RUN_ID": "r", "NOT_HERMES_X": "1"}
    assert adapters.launch_env(hermes, env) == {"PATH": "/bin", "HERMES_HOME": "/h", "OFFICE_RUN_ID": "r", "NOT_HERMES_X": "1"}


def test_other_harnesses_keep_their_env():
    env = {"HERMES_MODEL": "x"}
    assert adapters.launch_env(adapters.load_all()["codex"], env) == env
    assert adapters.shell_env_prelude(adapters.load_all()["codex"]) is None


def test_fallback_providers_cannot_change_the_model(hermes, tmp_path):
    """--ignore-user-config makes hermes read no config.yaml, so its fallback_providers chain is empty; --model and
    --provider are always explicit, and no env var can override them (HERMES_INFERENCE_MODEL is scrubbed)."""
    for kind in ("worker",):
        argv, _ = adapters.build_argv(hermes, kind, model=SLUG, effort="low", cwd=CWD)
        assert "--ignore-user-config" in argv and "--model" in argv and "--provider" in argv
        assert not {"--fallback", "--base-url", "--api-key"} & set(argv)
    env = adapters.launch_env(hermes, {"HERMES_INFERENCE_MODEL": "swapped", "HERMES_HOME": "/h"})
    assert "HERMES_INFERENCE_MODEL" not in env


def test_the_pane_shell_is_scrubbed_the_same_way(hermes, tmp_path):
    prelude = adapters.shell_env_prelude(hermes)
    out = subprocess.run(["sh", "-c", f"{prelude}\nenv"], capture_output=True, text=True,
                         env={"PATH": os.environ["PATH"], "HERMES_HOME": "/h", "HERMES_INFERENCE_MODEL": "x",
                              "HERMES_PROVIDER": "y", "KEEP": "1"}).stdout.splitlines()
    assert "HERMES_HOME=/h" in out and "KEEP=1" in out
    assert not [line for line in out if line.startswith(("HERMES_INFERENCE_MODEL", "HERMES_PROVIDER"))]


def test_agent_env_carries_the_scrub_for_a_hermes_pane(hermes, tmp_path, monkeypatch):
    monkeypatch.setattr(dispatch, "worker_env", lambda run, d, role: {"OFFICE_RUN_ID": "r", "PATH": "/bin"})
    d = {"id": "D1", "adapter_id": "hermes", "role": "executor"}
    env_file = dispatch.write_agent_env({"id": "r"}, d, tmp_path)
    assert env_file.read_text().startswith(adapters.shell_env_prelude(hermes))
    codex_file = dispatch.write_agent_env({"id": "r"}, {**d, "adapter_id": "codex"}, tmp_path)
    assert "HERMES_" not in codex_file.read_text()


# ---------------------------------------------------------------- quota/account attribution

@pytest.mark.parametrize("text,sig_id", [
    ("Error: your subscription usage is exhausted for this period", "subscription-usage-exhausted"),
    ("Anthropic: Your credit balance is too low to access the API", "credit-balance-too-low"),
    ("HTTP 429: rate limit reached for requests", "rate-limit"),
])
def test_exit_2_with_a_quota_text_is_the_account_not_the_model(hermes, tmp_path, text, sig_id):
    sig = adapters.classify_failure(hermes, f"working...\n{text}\n")
    assert sig["id"] == sig_id and sig["attribution"] == "quota/account"
    log = tmp_path / "agent.log"
    log.write_text(f"hermes\n{text}\n")
    usage = tmp_path / "usage.json"
    usage.write_text(json.dumps({"model": "claude-sonnet-5-5", "provider": "anthropic", "completed": False}))
    prof = adapters.profile(hermes, "worker")
    assert dispatch._audit_exit(hermes, prof, SLUG, usage, log, "nonzero", 2) == "nonzero"
    assert "attributed to quota/account" in log.read_text()


def test_a_quota_signature_in_hermes_text_is_a_quota_wall_for_the_cooldown(hermes, monkeypatch, tmp_path):
    """The existing quota-wall path (route cooldown, block instead of relaunch) takes Hermes' own phrases."""
    from office import gates
    log = tmp_path / "agent.log"
    log.write_text("narration\nYour subscription usage is exhausted\n[office] exit 2 attributed to quota/account\n")
    monkeypatch.setattr(gates, "_log_text", lambda d, ddir: log.read_text())
    monkeypatch.setattr(adapters, "load_all", lambda: {"hermes": hermes})
    wall = dispatch._quota_wall({"id": "r"}, {"terminal_classification": "nonzero", "id": "D", "adapter_id": "hermes"})
    assert wall and "subscription usage is exhausted" in wall


def test_an_ordinary_failure_matches_no_signature(hermes):
    assert adapters.classify_failure(hermes, "Traceback: KeyError: 'x'") is None


# ---------------------------------------------------------------- model/provider readback

def _usage(tmp_path, **fields) -> Path:
    path = tmp_path / "usage.json"
    path.write_text(json.dumps({"model": "claude-sonnet-5-5", "provider": "anthropic", "completed": True, **fields}))
    return path


def _audit(hermes, tmp_path, usage, slug=SLUG, classification="success", code=0):
    log = tmp_path / "agent.log"
    log.write_text("")
    return dispatch._audit_exit(hermes, adapters.profile(hermes, "worker"), slug, usage, log, classification, code), log


def test_a_matching_usage_report_keeps_the_run_successful(hermes, tmp_path):
    assert _audit(hermes, tmp_path, _usage(tmp_path))[0] == "success"


@pytest.mark.parametrize("fields", [{"model": "gemini-4-neon"}, {"provider": "openrouter"}, {"model": None}])
def test_a_usage_report_naming_another_route_fails_the_dispatch_as_wrong_model(hermes, tmp_path, fields):
    classification, log = _audit(hermes, tmp_path, _usage(tmp_path, **fields))
    assert classification == "wrong_model" and "wrong model" in log.read_text()


@pytest.mark.parametrize("fields", [{"completed": False}, {"completed": None}, {"completed": "true"}, {"completed": 1}])
def test_a_clean_exit_must_report_completed_true(hermes, tmp_path, fields):
    classification, log = _audit(hermes, tmp_path, _usage(tmp_path, **fields))
    assert classification == "wrong_model" and "completed" in log.read_text()


def test_a_clean_exit_with_no_completed_field_fails_closed(hermes, tmp_path):
    usage = tmp_path / "usage.json"
    usage.write_text(json.dumps({"model": "claude-sonnet-5-5", "provider": "anthropic"}))
    assert _audit(hermes, tmp_path, usage)[0] == "wrong_model"


def test_a_failed_run_is_not_judged_on_completed(hermes, tmp_path):
    assert _audit(hermes, tmp_path, _usage(tmp_path, completed=False), classification="nonzero", code=2)[0] == "nonzero"


def test_a_clean_exit_with_no_usage_report_is_wrong_model_but_a_crash_is_not(hermes, tmp_path):
    missing = tmp_path / "absent.json"
    assert _audit(hermes, tmp_path, missing)[0] == "wrong_model"
    assert _audit(hermes, tmp_path, missing, classification="nonzero", code=1)[0] == "nonzero"


def test_a_timeout_is_never_reclassified(hermes, tmp_path):
    assert _audit(hermes, tmp_path, _usage(tmp_path, model="other"), classification="timeout", code=-15)[0] == "timeout"


def test_a_vendor_qualified_model_id_matches_its_bare_report(hermes, tmp_path):
    assert hermes_access.readback_problem("anthropic/claude-sonnet-5-5", {"model": "claude-sonnet-5-5"}) is None
    assert hermes_access.readback_problem("claude-sonnet-5-5", {"model": "anthropic/claude-sonnet-5-5"}) is None
    assert hermes_access.readback_problem("claude-sonnet-5-5", {"model": "claude-sonnet-5-5-mini"})


def test_the_headless_wall_clock_is_bounded(hermes):
    prof = adapters.profile(hermes, "worker")
    assert dispatch._wall_cap_seconds(prof) == 60 * 60  # `hermes -z` has no --run-budget; Office's cap stops it


# ---------------------------------------------------------------- hermes auth list

AUTH_LIST = textwrap.dedent("""\
    anthropic (3 credentials):
      #1  anthropic-oauth-1    oauth   id=216f00 priority=0 hermes_pkce ←
      #2  claude_code          oauth   id=74da60 priority=1 claude_code
      #3  ANTHROPIC_API_KEY    api_key id=b9420f priority=2 env:ANTHROPIC_API_KEY exhausted invalid_request_error (400) (24m 48s left)

    gemini (1 credentials):
      #1  GOOGLE_API_KEY       api_key id=dcfef9 priority=0 env:GOOGLE_API_KEY ←

    nous: Nous · free tier · nous/welcome
      Run `hermes auth upgrade` to sign in with a Nous account, or /login inside a chat.

    openai-api (1 credentials):
      #1  OPENAI_API_KEY       api_key id=5064ff priority=0 env:OPENAI_API_KEY exhausted rate_limit (429)

    """)


def test_auth_list_parses_to_names_and_status_only():
    parsed = hermes_access.parse_auth_list(AUTH_LIST)
    assert parsed == {"anthropic": {"credentials": 3, "usable": 2, "exhausted": 1},
                      "gemini": {"credentials": 1, "usable": 1, "exhausted": 0},
                      "openai-api": {"credentials": 1, "usable": 0, "exhausted": 1}}
    blob = json.dumps(parsed)
    assert not any(token in blob for token in ("216f00", "GOOGLE_API_KEY", "oauth", "env:"))


def test_reachable_providers_skips_exhausted_and_unauthenticated(tmp_path):
    fake = tmp_path / "hermes"
    fake.write_text(f"#!/bin/sh\ncat <<'EOF'\n{AUTH_LIST}EOF\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    assert hermes_access.reachable_providers(hermes=str(fake)) == ["anthropic", "gemini"]


def test_reachable_providers_is_empty_when_hermes_is_absent_or_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert hermes_access.reachable_providers() == []
    broken = tmp_path / "hermes"
    broken.write_text("#!/bin/sh\necho boom >&2\nexit 3\n")
    broken.chmod(broken.stat().st_mode | stat.S_IEXEC)
    assert hermes_access.reachable_providers(hermes=str(broken)) == []


def test_only_hermes_auth_list_is_run_and_with_a_scrubbed_env(tmp_path, monkeypatch):
    fake = tmp_path / "hermes"
    record = tmp_path / "record"
    out = shlex.quote(str(record))  # quoted: sh would eat the backslashes of a Windows path
    fake.write_text(f"#!/bin/sh\necho \"$@\" > {out}\nenv | grep -c '^HERMES_' >> {out}\nenv | grep '^HERMES_HOME=' >> {out}\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("HERMES_MODEL", "x")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    hermes_access.reachable_providers(hermes=str(fake))
    assert record.read_text().splitlines() == ["auth list", "1", f"HERMES_HOME={tmp_path}"]


# ---------------------------------------------------------------- Herdr detection of a git-installed Hermes

posix_wrapper = pytest.mark.skipif(sys.platform == "win32", reason="a git-installed Hermes is a `#!/bin/sh` wrapper; Windows resolves a .exe or .cmd launcher from PATH, which is used as is")


def _git_install(tmp_path) -> tuple[Path, Path, Path]:
    """What a git install leaves on PATH: a shell wrapper that execs the managed python with an inline script."""
    root, tools = tmp_path / "hermes-agent", tmp_path / "tools"
    root.mkdir(), tools.mkdir()
    launcher = root / "hermes"
    launcher.write_text("#!/usr/bin/env python3\nif __name__ == '__main__':\n    print('hermes')\n")
    interpreter = tools / "python3"
    interpreter.write_text("#!/bin/sh\nexit 0\n")
    interpreter.chmod(0o755)
    wrapper = tmp_path / "bin" / "hermes"
    wrapper.parent.mkdir()
    wrapper.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        exec {interpreter} -I -c 'import os, re, sys
        os.environ.pop('"'"'PYTHONHOME'"'"', None)
        sys.path.insert(0, '"'"'{root}'"'"')
        from hermes_cli.main import main
        sys.exit(main())
        ' "$@"
        """))
    wrapper.chmod(0o755)
    return wrapper, interpreter, launcher


def test_the_launcher_script_and_its_interpreter_are_resolved_from_the_shell_wrapper(tmp_path):
    wrapper, interpreter, launcher = _git_install(tmp_path)
    assert hermes_access.resolve_launcher(str(wrapper)) == [str(interpreter), str(launcher)]


def test_a_forwarding_wrapper_in_front_of_the_git_wrapper_is_followed(tmp_path):
    # #1321: the installer's ~/.local/bin/hermes is `exec <checkout>/.hermes/bin/hermes "$@"`; stopping at it left
    # no shim, so Herdr never detected the pane's Hermes and every pane start timed out.
    wrapper, interpreter, launcher = _git_install(tmp_path)
    outer = tmp_path / "local-bin" / "hermes"
    outer.parent.mkdir()
    outer.write_text(f'#!/bin/sh\nexec {wrapper} "$@"\n')
    outer.chmod(0o755)
    assert hermes_access.resolve_launcher(str(outer)) == [str(interpreter), str(launcher)]
    quoted = tmp_path / "local-bin" / "hermes-quoted"
    quoted.write_text(f"#!/bin/sh\nexec '{outer}' \"$@\"\n")
    assert hermes_access.resolve_launcher(str(quoted)) == [str(interpreter), str(launcher)]


def test_a_forwarding_loop_or_dead_end_returns_the_path_hermes(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_text(f'#!/bin/sh\nexec {b} "$@"\n')
    b.write_text(f'#!/bin/sh\nexec {a} "$@"\n')
    assert hermes_access.resolve_launcher(str(a)) == [str(a)]
    dead = tmp_path / "dead"
    dead.write_text(f'#!/bin/sh\nexec {tmp_path / "missing"} "$@"\n')
    assert hermes_access.resolve_launcher(str(dead)) == [str(dead)]
    extra = tmp_path / "extra"  # an exec that adds its own arguments is not a pure forward
    extra.write_text(f'#!/bin/sh\nexec {a} --profile x "$@"\n')
    assert hermes_access.resolve_launcher(str(extra)) == [str(extra)]


def test_the_real_hermes_wrapper_shape_resolves_when_installed():
    exe = hermes_access.shutil.which("hermes")
    if not exe or not (Path(exe).read_bytes().startswith(b"#!/bin/sh") and b" -I -c " in Path(exe).read_bytes()):
        pytest.skip("no git-installed hermes wrapper on this host")
    launcher = hermes_access.resolve_launcher(exe)
    assert len(launcher) == 2 and Path(launcher[1]).name == "hermes" and Path(launcher[0]).is_file()


def test_a_binary_or_console_script_hermes_is_used_as_is(tmp_path):
    script = tmp_path / "hermes"
    script.write_text("#!/usr/bin/env python3\nprint('x')\n")
    assert hermes_access.resolve_launcher(str(script)) == [str(script)]
    assert hermes_access.resolve_launcher(str(tmp_path / "nowhere")) == [str(tmp_path / "nowhere")]


@posix_wrapper
def test_the_pane_shim_execs_the_resolved_launcher_so_herdr_sees_a_hermes_script(tmp_path, monkeypatch, hermes):
    wrapper, interpreter, launcher = _git_install(tmp_path)
    monkeypatch.setenv("PATH", f"{wrapper.parent}{os.pathsep}/usr/bin")
    env = adapters.pane_launch_env(hermes, tmp_path / "dispatch")
    shim = tmp_path / "dispatch" / "bin" / "hermes"
    assert env["PATH"].split(os.pathsep)[0] == str(shim.parent)
    assert shim.read_text() == f"#!/bin/sh\nexec '{interpreter}' '{launcher}' \"$@\"\n"
    assert shim.stat().st_mode & stat.S_IXUSR


def test_no_shim_when_the_path_hermes_is_already_detectable(tmp_path, monkeypatch, hermes):
    script = tmp_path / "bin" / "hermes"
    script.parent.mkdir()
    script.write_text("#!/usr/bin/env python3\nprint('x')\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{script.parent}{os.pathsep}/usr/bin")
    assert adapters.pane_launch_env(hermes, tmp_path / "dispatch") == {}
    assert adapters.pane_launch_env(adapters.load_all()["codex"], tmp_path / "dispatch") == {}


@posix_wrapper
def test_a_pane_dispatch_gets_the_shim_in_its_agent_env(tmp_path, monkeypatch, hermes):
    wrapper, interpreter, launcher = _git_install(tmp_path)
    monkeypatch.setenv("PATH", f"{wrapper.parent}{os.pathsep}/usr/bin")
    monkeypatch.setattr(dispatch.paths, "run_dir", lambda run_id: tmp_path / "run")
    monkeypatch.setattr(adapters, "load_all", lambda: {"hermes": hermes})
    env = dispatch._harness_env({"id": "r"}, {"id": "D1", "adapter_id": "hermes"}, {"kind": "worker", "cwd": str(tmp_path)})
    assert env["PATH"].startswith(str(tmp_path / "run" / "dispatches" / "D1" / "bin"))


# ---------------------------------------------------------------- Herdr integration as an install item

HERDR_STATUS = {"current": "hermes: current (v5) (/h/plugins/herdr-agent-state/__init__.py)\n",
                "missing": "pi: current (v9) (/p)\nhermes: not installed (/h/plugins/herdr-agent-state/__init__.py)\n",
                "stale": "hermes: outdated (v3, latest v5) (/h/plugins/herdr-agent-state/__init__.py)\n"}


@pytest.fixture()
def herdr(monkeypatch):
    calls: list[tuple] = []
    state = {"status": HERDR_STATUS["missing"], "install_rc": 0}

    def fake(*args):
        calls.append(args)
        if args[0] == "status":
            return SimpleNamespace(returncode=0, stdout=state["status"], stderr="")
        return SimpleNamespace(returncode=state["install_rc"], stdout="", stderr="no write access" if state["install_rc"] else "")

    monkeypatch.setattr(install, "_herdr_integration", fake)
    return SimpleNamespace(calls=calls, state=state)


@pytest.mark.parametrize("key,expected", [("current", "current"), ("missing", "missing"), ("stale", "stale")])
def test_the_herdr_hermes_integration_is_a_reviewable_item(herdr, key, expected):
    herdr.state["status"] = HERDR_STATUS[key]
    status = install.integration_status("hermes")
    assert install.install_items("hermes") == [install.HERDR_HERMES]
    assert [(i["id"], i["state"]) for i in status["items"]] == [(install.HERDR_HERMES, expected)]
    if expected != "current":
        assert "herdr integration install hermes" in status["items"][0]["change"]
    assert [c for c in herdr.calls if c[0] != "status"] == []  # reviewing writes nothing


def test_a_plain_install_never_installs_the_herdr_integration(herdr, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    install.install(hooks=True, dry_run=True)
    install.install(only=["claude"], hooks=True, dry_run=True)
    assert [c for c in herdr.calls if c[0] == "install"] == []


def test_naming_the_item_runs_herdr_install_once(herdr, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    res = install.install(items=[install.HERDR_HERMES], hooks=True)
    assert [c for c in herdr.calls if c[0] == "install"] == [("install", "hermes")]
    assert any("Herdr integration installed" in line for line in res.lines)


def test_dry_run_and_current_state_install_nothing(herdr, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    res = install.install(only=["hermes"], dry_run=True, hooks=True)
    assert any("would run `herdr integration install hermes`" in line for line in res.lines)
    herdr.state["status"] = HERDR_STATUS["current"]
    res = install.install(only=["hermes"], hooks=True)
    assert any("already current" in line for line in res.lines)
    assert [c for c in herdr.calls if c[0] == "install"] == []


def test_a_failed_herdr_install_is_reported_not_raised(herdr, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    herdr.state["install_rc"] = 1
    res = install.install(items=[install.HERDR_HERMES], hooks=True)
    assert any("failed (no write access)" in line for line in res.lines)


def test_a_missing_herdr_leaves_hermes_unsupported_with_the_reason(monkeypatch):
    monkeypatch.setattr(install, "_herdr_integration", lambda *a: None)
    status = install.integration_status("hermes")
    assert status["state"] == "unsupported" and "herdr is not installed" in status["reason"] and status["items"] == []


# ---------------------------------------------------------------- transcripts (Hermes state.db)

@pytest.fixture()
def state_db(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    con = sqlite3.connect(home / "state.db")
    con.executescript("""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT, started_at REAL);
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT, timestamp REAL);
    """)
    return con


def test_a_hermes_pane_prompt_and_reply_are_read_from_the_session_store(state_db, tmp_path):
    marker = "/runs/D1/brief.md"
    state_db.executemany("INSERT INTO sessions VALUES (?,?,?)", [("s1", str(tmp_path), 100.0), ("s2", "/elsewhere", 100.0)])
    state_db.executemany("INSERT INTO messages(session_id, role, content, timestamp) VALUES (?,?,?,?)", [
        ("s1", "user", f"Read the brief at {marker}", 101.0), ("s1", "assistant", "working", 102.0),
        ("s1", "assistant", "VERDICT: APPROVED", 103.0), ("s1", "assistant", "", 104.0),
        ("s2", "user", f"also {marker}", 101.0), ("s2", "assistant", "wrong session", 105.0)])
    state_db.commit()
    assert transcripts.prompt_seen("hermes", marker=marker, cwd=tmp_path, since=100)
    assert transcripts.session_id("hermes", marker=marker, cwd=tmp_path, since=100) == "s1"
    assert transcripts.final_reply("hermes", marker=marker, cwd=tmp_path, since=100) == "VERDICT: APPROVED"


def test_a_hermes_prompt_before_the_dispatch_or_in_another_role_is_not_seen(state_db, tmp_path):
    state_db.execute("INSERT INTO sessions VALUES ('old', ?, 1.0)", (str(tmp_path),))
    state_db.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES ('old','user','see /b.md', 1.0)")
    state_db.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES ('old','tool','/c.md', 9999999999.0)")
    state_db.commit()
    assert not transcripts.prompt_seen("hermes", marker="/b.md", cwd=tmp_path, since=10_000)
    assert not transcripts.prompt_seen("hermes", marker="/c.md", cwd=tmp_path, since=0)
    assert not transcripts.prompt_seen("hermes", marker="/x.md", cwd=tmp_path, since=0)


def test_a_missing_session_store_reads_as_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "none"))
    assert not transcripts.prompt_seen("hermes", marker="/b.md")
    assert transcripts.final_reply("hermes", marker="/b.md") is None
