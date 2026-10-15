"""Opencode seed adapter registration: `office harness validate opencode`.

A fake `opencode` binary stands in for the harness; no test here calls the
real binary or a real model. What these hold: the shipped seed adapter
validates with no TODO, its worker pins the model and effort (`--model
provider/model#variant`, the documented per-run variant selector) with the
prompt as one argv element and no shell, no dispatched form carries `--auto`,
the Herdr pane form pins the same model/effort, preflight gates the pinned
slug without treating auth-list exit 0 as sign-in evidence, capabilities stay
builder-only, onboarding recognizes a listed free model as a credential-free
usable path, and install records the hook posture as unsupported.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from office import adapters, candidates, harnesscmd, install, onboarding, route_policy
from office.state import Refused

SLUG = "opencode/muse-spark-1.3-contributor-free"
MODEL_ID = "muse-spark-1.3-contributor-free"

FAKE_OPENCODE = f"""#!{sys.executable}
import os, sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("opencode v2.0.29"); sys.exit(0)
if args[:1] == ["models"]:
    state = os.environ.get("FAKE_OPENCODE_MODELS", "listed")
    if state == "fail":
        print("model listing failed", file=sys.stderr); sys.exit(2)
    if state == "empty":
        sys.exit(0)
    if state == "unlisted":
        print("opencode/big-pickle"); sys.exit(0)
    print("{SLUG}"); print("opencode/big-pickle"); sys.exit(0)
if args[:2] == ["auth", "list"]:
    state = os.environ.get("FAKE_OPENCODE_AUTH", "ok")
    if state == "fail":
        print("boom", file=sys.stderr); sys.exit(1)
    if state == "crash":
        raise RuntimeError("auth list crashed")
    if state == "missing":
        print("No authenticated integrations"); sys.exit(0)
    print("opencode  authenticated (oauth)"); sys.exit(0)
if args[:1] == ["run"]:
    model = None
    for i, a in enumerate(args):
        if a == "--model" and i + 1 < len(args):
            model = args[i + 1]
    prompt = args[-1] if len(args) > 3 else ""
    print(f"MODEL={{model}}")
    print(f"PROMPT={{prompt}}")
    print("READY"); sys.exit(0)
if args[:2] == ["mini", "--model"] or (args[:1] == ["--model"]):
    print("interactive ready"); sys.exit(0)
print("unknown", file=sys.stderr); sys.exit(1)
"""


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """A fake `opencode` first on PATH. FAKE_OPENCODE_AUTH controls `auth list`:
    ok (signed in), missing (signed out, exit 0), fail (exit 1)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "opencode"
    exe.write_text(FAKE_OPENCODE)
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"]))
    monkeypatch.setenv("FAKE_OPENCODE_AUTH", "ok")
    for key in ("OFFICE_AUTH_FIXTURE", "OFFICE_USER_ADAPTERS"):
        monkeypatch.delenv(key, raising=False)
    for mod in (adapters, harnesscmd):
        for cache in ("_SOURCES_CACHE",):
            getattr(mod, cache, {}).clear()
    adapters._RESOLVED_EXE.clear()
    adapters._VERSION_MEMO.clear()
    return exe


def _adapter():
    return adapters.load_all()["opencode"]


def test_seed_adapter_has_no_todo_and_validates(fake):
    text = (adapters.adapter_dir() / "opencode.yaml").read_text()
    assert "TODO" not in text
    assert harnesscmd.check(_adapter(), rows=[]) == []
    problems, warnings = harnesscmd.check_full(_adapter(), rows=[])
    assert problems == [] and warnings == []


def test_validate_command_exits_zero(fake, capsys):
    from office import cli
    assert cli.main(["harness", "validate", "opencode"]) == 0
    assert "valid" in capsys.readouterr().out
    seed = str(adapters.adapter_dir() / "opencode.yaml")
    assert cli.main(["harness", "validate", "--file", seed]) == 0
    out = capsys.readouterr().out
    assert "valid" in out and "TODO" not in out


def test_worker_argv_pins_model_and_effort_with_argv_prompt(fake, tmp_path):
    adapter = _adapter()
    assert adapter["invocation"]["prompt_transport"] in ("stdin", "argv")
    assert adapter["safe_prompt_passing"]["shell"] is False
    worker = adapter["office_profiles"]["worker"]
    assert worker["prompt"] in ("stdin", "argv")
    assert "{model}" in " ".join(worker["argv"]) and "{effort}" in " ".join(worker["argv"])
    for effort in ("low", "medium", "high"):
        argv, prof = adapters.build_argv(adapter, "worker", model=SLUG, effort=effort, cwd=tmp_path)
        assert argv[0] == "opencode" or argv[0].endswith("/opencode")
        assert f"{SLUG}#{effort}" in argv
        assert "--auto" not in argv
    # The prompt travels as one argv element, never through a shell.
    argv, _ = adapters.build_argv(adapter, "worker", model=SLUG, effort="medium", cwd=tmp_path)
    proc = subprocess.run([*argv, "hello"], capture_output=True, text=True, timeout=30,
                          stdin=subprocess.DEVNULL, cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert f"MODEL={SLUG}#medium" in proc.stdout
    assert "PROMPT=hello" in proc.stdout


def test_no_dispatched_form_contains_auto(fake):
    adapter = _adapter()
    forms = []
    for kind, prof in adapter["office_profiles"].items():
        forms.append((f"office_profiles.{kind}.argv", prof.get("argv") or []))
        inter = prof.get("interactive") or {}
        if isinstance(inter.get("argv"), list):
            forms.append((f"office_profiles.{kind}.interactive.argv", inter["argv"]))
        for key in ("headless_resume_argv",):
            if isinstance(prof.get(key), list):
                forms.append((f"office_profiles.{kind}.{key}", prof[key]))
        if isinstance(inter.get("resume_argv"), list):
            forms.append((f"office_profiles.{kind}.interactive.resume_argv", inter["resume_argv"]))
    assert forms
    for name, form in forms:
        assert not any(a == "--auto" or str(a).startswith("--auto=") for a in form), name


def test_herdr_pane_form_pins_the_same_model_and_effort(fake, tmp_path):
    worker = _adapter()["office_profiles"]["worker"]
    assert worker["herdr_kind"] == "opencode"
    argv = (worker.get("interactive") or {}).get("argv")
    assert isinstance(argv, list) and argv
    assert "{model}" in " ".join(argv) and "{effort}" in " ".join(argv)
    assert not any(str(a) == "--auto" for a in argv)
    rendered, kind = adapters.interactive_argv(_adapter(), "worker", model=SLUG, effort="medium",
                                               cwd=tmp_path)
    assert kind == "opencode"
    assert f"{SLUG}#medium" in rendered
    assert "--auto" not in rendered


def test_preflight_uses_model_listing_without_auth_list_gate(fake, monkeypatch):
    monkeypatch.setenv("FAKE_OPENCODE_AUTH", "missing")
    ok, _ = adapters.run_preflight(_adapter(), SLUG)
    assert ok is True
    ok, detail = adapters.run_preflight(_adapter(), "opencode/no-such-model")
    assert ok is False and "model_check" in detail
    monkeypatch.setenv("FAKE_OPENCODE_AUTH", "fail")
    ok, detail = adapters.run_preflight(_adapter(), SLUG)
    assert ok is True and detail == ""


def test_preflight_signed_out_unlisted_model_fails_model_check(fake, monkeypatch):
    monkeypatch.setenv("FAKE_OPENCODE_AUTH", "missing")
    monkeypatch.setenv("FAKE_OPENCODE_MODELS", "unlisted")
    ok, detail = adapters.run_preflight(_adapter(), SLUG)
    assert ok is False and "model_check" in detail and SLUG in detail


def test_capabilities_are_builder_only(fake):
    adapter = _adapter()
    assert adapter["capabilities"] == ["builder"]
    assert set(adapter["office_profiles"]) == {"worker"}


def test_free_model_discovery_does_not_change_auth_or_enable_candidates(fake, monkeypatch, tmp_path):
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp_path / "user-config.yaml"))
    monkeypatch.setenv("FAKE_OPENCODE_AUTH", "missing")
    row = {"model_id": MODEL_ID, "invocation_model_id": SLUG, "invocation_harness": "opencode",
           "effort": "xhigh", "dispatchable": True, "free_model": True}
    monkeypatch.setattr(candidates, "catalog_rows", lambda: [row])

    assert onboarding.credentials("opencode")[0] == "missing"
    assert onboarding._opencode_free_model()[0] == SLUG
    assert route_policy.is_free_model(row)
    built, skipped = candidates.build_candidates(sqlite3.connect(":memory:"), "worker", probe=False,
                                                  user_policies=[])
    assert built == []
    assert skipped[0]["category"] == "free-model-opt-in"
    with pytest.raises(Refused, match="requires explicit opt-in"):
        candidates.declared_candidate("opencode", MODEL_ID, "xhigh")

    (tmp_path / "user-config.yaml").write_text("onboarding:\n  free_models_enabled: true\n")
    built, skipped = candidates.build_candidates(sqlite3.connect(":memory:"), "worker", probe=False,
                                                  user_policies=[])
    assert [c["invocation_model_id"] for c in built] == [SLUG]
    assert built[0]["free_model"] is True
    assert skipped == []
    assert candidates.declared_candidate("opencode", MODEL_ID, "xhigh")["free_model"] is True


def _declare(monkeypatch, rows, harness, model, effort=None):
    monkeypatch.setattr(candidates, "catalog_rows", lambda: rows)
    return candidates.declared_candidate(harness, model, effort)


def test_uncatalogued_free_slug_requires_opt_in_like_a_marked_row(monkeypatch, tmp_path):
    cfg = tmp_path / "user-config.yaml"
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(cfg))
    for name in ("some-new-model-free", "opencode/some-new-model-free"):
        with pytest.raises(Refused, match="names itself free and requires explicit opt-in") as exc:
            _declare(monkeypatch, [], "opencode", name, "high")
        assert exc.value.category == "free-model-opt-in"
    cfg.write_text("onboarding:\n  free_models_enabled: true\n")
    cand = _declare(monkeypatch, [], "opencode", "some-new-model-free", "high")
    assert cand["free_model"] is True and cand["override"] is True


def test_uncatalogued_non_free_routes_are_unchanged(monkeypatch, tmp_path):
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp_path / "user-config.yaml"))
    assert _declare(monkeypatch, [], "opencode", "opencode/big-pickle", "high")["free_model"] is False
    # The suffix convention is opencode's; another harness's slug is not read as free.
    assert _declare(monkeypatch, [], "agy", "gemini-free", "high")["free_model"] is False


def test_ambiguous_catalog_match_keeps_the_catalog_answer(monkeypatch, tmp_path):
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp_path / "user-config.yaml"))
    base = {"model_id": "mystery-free", "invocation_harness": "opencode", "dispatchable": True}
    rows = [{**base, "effort": "low", "free_model": True}, {**base, "effort": "high"}]
    # Catalogued but ambiguous (several rows, no effort): behaves as before, slug and free row unread.
    assert _declare(monkeypatch, rows, "opencode", "mystery-free")["free_model"] is False


def test_catalogued_rows_ignore_the_slug_convention(monkeypatch, tmp_path):
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp_path / "user-config.yaml"))
    row = {"model_id": "named-free", "invocation_harness": "opencode", "effort": "high", "dispatchable": True}
    assert _declare(monkeypatch, [row], "opencode", "named-free", "high")["free_model"] is False
    marked = {**row, "model_id": "plain", "free_model": True}
    with pytest.raises(Refused, match="plain is marked free and requires explicit opt-in"):
        _declare(monkeypatch, [marked], "opencode", "plain", "high")


def test_opted_in_free_model_passes_preflight_without_login(fake, monkeypatch):
    monkeypatch.setenv("FAKE_OPENCODE_AUTH", "missing")
    assert onboarding.credentials("opencode")[0] == "missing"
    assert onboarding._opencode_free_model()[0] == SLUG
    ok, detail = adapters.run_preflight(_adapter(), SLUG)
    assert ok is True and detail == ""


@pytest.mark.parametrize("auth_state, expected", [
    ("missing", "no authenticated integrations"),
    ("fail", "exited 1"),
    ("crash", "auth list crashed"),
])
def test_onboarding_auth_list_failures_are_explicit_when_no_model_is_usable(fake, monkeypatch, auth_state, expected):
    monkeypatch.setenv("FAKE_OPENCODE_AUTH", auth_state)
    monkeypatch.setenv("FAKE_OPENCODE_MODELS", "empty")
    state, where = onboarding.credentials("opencode")
    assert state == "missing" and expected in where
    assert "no explicitly free model" not in where
    model, detail = onboarding._opencode_free_model()
    assert model is None and "no explicitly free model" in detail


def test_onboarding_auth_fixture_and_unknown_harness(fake, monkeypatch):
    monkeypatch.setenv("OFFICE_AUTH_FIXTURE", json.dumps({"opencode": "found"}))
    assert onboarding.credentials("opencode") == ("found", "fixture")
    assert onboarding.credentials("hermes")[0] == "unknown"


def test_install_records_unsupported_hook_posture():
    status = install.integration_status("opencode")
    assert status["state"] == "unsupported" and status["reason"]
