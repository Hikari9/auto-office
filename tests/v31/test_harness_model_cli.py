"""#499: `office harness scaffold|validate|smoke|list` and `office model add|list|disable`.

A fake `kilo` binary stands in for a new harness; no test here calls a real
harness or model. What these hold: registering a harness needs no Python edit,
user adapters never shadow a seed unless asked, model rows go to the user
overlay and never to the packaged seed, validate's semantic checks catch the
#479 mistakes, and nothing in the flow (smoke included) records trust.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
import yaml

from office import adapters, candidates, config, configcmd, discovery, harnesscmd, paths, scoring, user_catalog

FAKE_KILO = f"""#!{sys.executable}
import json, os, sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("kilo 2.4.1"); sys.exit(0)
if args[:1] == ["--help"]:
    print("usage: kilo [--print] [--model M] [--effort E] [--yolo] [--auto-approve]"); sys.exit(0)
if args[:1] == ["models"]:
    print("acme/fast-1"); print("acme/deep-2"); sys.exit(0)
log = os.environ.get("KILO_LOG")
prompt = sys.stdin.read()
if log:
    with open(log, "a") as f:
        f.write(json.dumps({{"argv": args, "cwd": os.getcwd(), "prompt": prompt,
                            "run": os.environ.get("OFFICE_RUN_ID")}}) + "\\n")
print("READY")
"""

GOOD = {
    "id": "kilo",
    "verified_state": "valid-unverified",
    "version_fingerprint": {"command": ["kilo", "--version"], "parser": "first-line"},
    "model_source": {"type": "cli", "command": ["kilo", "models"]},
    "preflight": {"model_check": ["kilo", "models"]},
    "effort_mapping": {"low": "low", "medium": "medium", "high": "high"},
    "benchmark_slug_mapping": {},
    "invocation": {"executable": "kilo", "argv": ["--print", "--model", "{model}", "--effort", "{effort}"],
                   "prompt_transport": "stdin", "cwd_transport": "process-cwd"},
    "safe_prompt_passing": {"shell": False, "supports_prompt_file": False, "notes": "stdin only"},
    "dispatch_forms": ["cli"],
    "trusted_for": ["discovery"],
    "quota_probe": {"type": "external-primitive", "command": None, "unknown_is_unlimited": False},
    "shallow_review": {"supported": False, "minimum_effort": None},
    "agentic_capability": {"default": "unknown"},
    "failure_signatures": [],
    "conformance": {"deterministic": "pending", "live": "pending"},
    "source_notes": "kilo 2.4.1 fake",
    "session": {"id": "none"},
    "capabilities": ["builder"],
    "office_profiles": {"worker": {"argv": ["--print", "--model", "{model}", "--effort", "{effort}"],
                                   "prompt": "stdin", "output": "stdout"}},
}


@pytest.fixture
def unit(tmp_path, monkeypatch):
    """Unit-tier isolation: a temp user config dir and a fake kilo on PATH, no runs.db."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "kilo"
    exe.write_text(FAKE_KILO)
    exe.chmod(0o755)
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(tmp_path / "user-config.yaml"))
    monkeypatch.setenv("OFFICE_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")]))
    return tmp_path


def _kilo(env) -> Path:
    exe = env.bin / "kilo"
    exe.write_text(FAKE_KILO)
    exe.chmod(0o755)
    return exe


def _write_adapter(data: dict, name: str = "kilo") -> Path:
    d = adapters.user_adapter_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.yaml"
    p.write_text(yaml.safe_dump(data, sort_keys=False))
    return p


def _with(**changes) -> dict:
    data = json.loads(json.dumps(GOOD))
    for dotted, value in changes.items():
        node = data
        *head, last = dotted.split("__")
        for k in head:
            node = node[k]
        node[last] = value
    return data


def _trust_acts(env) -> int:
    con = env.con()
    try:
        scoring.ensure_trust_schema(con)
        return con.execute("SELECT COUNT(*) FROM adapter_trust_acts").fetchone()[0]
    finally:
        con.close()


def test_scaffold_drafts_a_todo_adapter_and_stub_that_validate_refuses(env):
    _kilo(env)
    code, out = env.office("harness", "scaffold", "kilo", "--binary", "kilo")
    assert code == 0, out
    draft = adapters.user_adapter_dir() / "kilo.yaml"
    text = draft.read_text()
    assert "TODO" in text and "verified_state: valid-unverified" in text
    assert "--yolo" in text  # the risky flag seen in --help is called out
    assert (adapters.user_adapter_dir() / "tests" / "test_kilo_adapter.py").is_file()
    code, out = env.office("harness", "validate", "kilo")
    assert code == 1 and "unfilled TODO" in out and "trust: unchanged" in out
    # A second scaffold does not overwrite the user's edits.
    assert env.office("harness", "scaffold", "kilo", "--binary", "kilo")[0] == 1


def test_scaffold_refuses_a_seed_id_unless_overriding(env):
    code, out = env.office("harness", "scaffold", "codex", "--binary", "codex")
    assert code == 1 and "seed" in out
    assert not (adapters.user_adapter_dir() / "codex.yaml").exists()


def test_registering_a_harness_needs_no_python_edit(env):
    _kilo(env)
    _write_adapter(GOOD)
    code, out = env.office("harness", "validate", "kilo")
    assert code == 0, out
    assert "kilo" in discovery.harness_names()
    assert "kilo" not in configcmd._installed()  # no catalog row routes to it yet

    seed = paths.resources_root() / "catalog" / "seed.yaml"
    before = seed.read_bytes()
    code, out = env.office("model", "add", "kilo/acme/fast-1", "--effort", "medium,high")
    assert code == 0, out
    assert seed.read_bytes() == before  # the packaged seed is never written
    overlay = yaml.safe_load(user_catalog.path().read_text())
    assert [(r["model_id"], r["effort"]) for r in overlay["models"]] == [("fast-1", "medium"), ("fast-1", "high")]
    rows = [r for r in candidates.catalog_rows() if r.get("invocation_harness") == "kilo"]
    assert {r["invocation_model_id"] for r in rows} == {"acme/fast-1"}
    assert configcmd._installed()["kilo"] is True

    code, out = env.office("model", "add", "kilo/acme/fast-1", "--effort", "medium")
    assert code == 1 and "already a catalog row" in out
    code, out = env.office("model", "add", "kilo/acme/fast-1", "--effort", "xhigh")
    assert code == 1 and "effort_mapping" in out

    code, out = env.office("model", "list", "kilo")
    assert code == 0 and "kilo/fast-1@medium" in out and "user" in out

    code, out = env.office("model", "disable", "kilo/fast-1@high", "--reason", "flaky")
    assert code == 0, out
    off = {r["effort"]: r.get("dispatchable") for r in candidates.catalog_rows() if r.get("invocation_harness") == "kilo"}
    assert off == {"medium": True, "high": False}
    assert "already disabled" in env.office("model", "disable", "kilo/fast-1@high")[1]

    code, out = env.office("harness", "list")
    assert code == 0 and "kilo" in out and "user" in out and "valid-unverified" in out


def test_model_disable_turns_off_a_seed_row_through_the_overlay(env):
    seed = paths.resources_root() / "catalog" / "seed.yaml"
    before = seed.read_bytes()
    code, out = env.office("model", "disable", "codex/luna")
    assert code == 0, out
    assert seed.read_bytes() == before
    assert all(r.get("dispatchable") is False for r in candidates.catalog_rows()
               if r.get("invocation_harness") == "codex" and r.get("model_id") == "luna")
    assert env.office("model", "disable", "codex/no-such-model")[0] == 2


def test_validate_catches_the_semantic_mistakes(unit):
    cases = {
        "permission/trust flag --yolo": _with(office_profiles__worker__argv=["--yolo", "--model", "{model}"]),
        "no interactive.argv": _with(office_profiles__worker__herdr_kind="kilo"),
        "not a safe transport": _with(office_profiles__worker__prompt="shell"),
        "is a shell": _with(invocation__executable="bash"),
        "not resolvable": _with(preflight={"auth_check": ["kilo-auth-helper", "status"]}),
        "unknown placeholders": _with(office_profiles__worker__argv=["--model", "{model}", "--label", "{label}"]),
        "carries {prompt}": _with(office_profiles__worker__argv=["--model", "{model}", "{prompt}"]),
        "never declared in a file": _with(verified_state="proven"),
    }
    for needle, data in cases.items():
        problems = harnesscmd.check(data, rows=[])
        assert any(needle in p for p in problems), (needle, problems)
    # A justified flag passes; the effort map must cover the catalog's efforts.
    justified = _with(office_profiles__worker__argv=["--yolo", "--model", "{model}"],
                      trust_justifications={"--yolo": "headless kilo cannot answer approval prompts"})
    assert harnesscmd.check(justified, rows=[]) == []
    row = {"invocation_harness": "kilo", "model_id": "fast-1", "effort": "xhigh"}
    assert any("'xhigh'" in p for p in harnesscmd.check(GOOD, rows=[row]))
    interactive = _with(office_profiles__worker__herdr_kind="kilo",
                        office_profiles__worker__interactive={"argv": ["--model", "{model}"]})
    assert harnesscmd.check(interactive, rows=[]) == []


def test_shipped_seed_adapters_pass_the_semantic_checks(unit):
    for aid in adapters.harness_ids():
        problems = harnesscmd.check(adapters.load_all()[aid])
        assert problems == [], (aid, problems)


def test_seed_wins_on_id_collision_unless_overridden(unit):
    shadow = dict(GOOD, id="codex")
    _write_adapter(shadow, "codex")
    assert adapters.load_sources()["codex"][1] == "seed"
    assert adapters.load_all()["codex"]["invocation"]["executable"] == "codex"
    _write_adapter({**shadow, "override_seed": True}, "codex")
    assert adapters.load_sources()["codex"][1] == "user-override"
    assert adapters.load_all()["codex"]["invocation"]["executable"] == "kilo"


def test_editing_a_loaded_adapter_never_changes_the_next_load(unit):
    # A caller that edits what it loaded (a test pointing codex at a fake binary) must not
    # leave the per-process cache pointing every later caller at it.
    adapters.load_all()["codex"]["invocation"]["executable"] = "/nowhere/codex"
    adapters.load_sources()["codex"][0]["invocation"]["executable"] = "/nowhere/codex"
    assert adapters.load_all()["codex"]["invocation"]["executable"] == "codex"


def test_a_broken_user_adapter_never_breaks_the_seed_set(env):
    d = adapters.user_adapter_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "broken.yaml").write_text("id: [unclosed\n")
    assert "codex" in adapters.load_all() and "broken" not in adapters.load_all()
    code, out = env.office("harness", "validate", "broken")
    assert code == 1 and "not a readable YAML" in out


def test_smoke_records_launch_evidence_in_a_throwaway_repo_and_never_trust(env, tmp_path):
    _kilo(env)
    _write_adapter(GOOD)
    assert env.office("model", "add", "kilo/acme/fast-1", "--effort", "medium")[0] == 0
    acts = _trust_acts(env)
    log = tmp_path / "kilo.log"
    code, out = env.office("harness", "smoke", "kilo", "--model", "fast-1",
                           env={"KILO_LOG": str(log), "OFFICE_RUN_ID": "deadbeef"})
    assert code == 0, out
    assert "launched" in out and "trust and conformance unchanged" in out
    call = json.loads(log.read_text().splitlines()[-1])
    assert call["argv"][:3] == ["--print", "--model", "acme/fast-1"]  # the catalog slug, not the id
    assert "office-smoke-" in call["cwd"] and Path(call["cwd"]).resolve() != env.repo.resolve()
    assert not Path(call["cwd"]).exists()  # the throwaway repo is gone
    assert call["run"] is None  # the smoke agent inherits no run identity
    assert "READY" in call["prompt"]
    rec = json.loads(harnesscmd.smoke_log().read_text().splitlines()[-1])
    assert rec["evidence"] == "launch" and rec["trust"] == "unchanged" and rec["result"] == "launched"
    assert "READY" not in json.dumps(rec)  # the prompt is never persisted
    assert _trust_acts(env) == acts
    con = env.con()
    try:
        assert scoring.evaluate_trust_state(con, "kilo@2/fast-1@medium")[1] == "valid-unverified"
    finally:
        con.close()
    assert "smoke launched" in env.office("harness", "list")[1]


def test_smoke_refuses_an_invalid_adapter_and_reports_a_failed_launch(env):
    _kilo(env)
    _write_adapter(_with(office_profiles__worker__argv=["--yolo", "--model", "{model}"]))
    code, out = env.office("harness", "smoke", "kilo", "--model", "fast-1")
    assert code == 1 and "does not validate" in out
    _write_adapter(_with(preflight={"model_check": ["kilo", "models"]}))
    code, out = env.office("harness", "smoke", "kilo", "--model", "acme/unknown")
    assert code == 1 and "preflight" in out


def test_run_pins_include_user_overlays_only_when_present(env):
    base = config.snapshot_hashes()
    _write_adapter(GOOD)
    user_catalog.save({"models": [], "disabled": [{"harness": "codex", "model_id": "luna"}]})
    pinned = config.snapshot_hashes()
    assert pinned["adapter_hash"] != base["adapter_hash"] and pinned["catalog_hash"] != base["catalog_hash"]
    assert pinned["policy_hash"] == base["policy_hash"]


def test_a_missing_harness_binary_warns_but_a_malformed_command_fails(unit):
    absent = _with(invocation__executable="kilo-absent", version_fingerprint={"command": ["kilo-absent", "--version"]},
                   model_source={"type": "cli", "command": ["kilo-absent", "models"]},
                   preflight={"model_check": ["kilo-absent", "models"]})
    problems, warnings = harnesscmd.check_full(absent, rows=[])
    assert problems == [] and len(warnings) == 3
    assert any("not resolvable" in p for p in harnesscmd.check(_with(preflight={"model_check": []}), rows=[]))
    assert any("not resolvable" in p for p in harnesscmd.check(_with(preflight={"auth_check": ["other-tool"]}), rows=[]))


def test_an_overridden_seed_never_inherits_the_seeds_trust(env):
    proven = next(t for t, s in scoring.trust_baseline().items() if s == "proven" and t.startswith("codex@"))
    rest = proven.split("/", 1)[1]
    con = env.con()
    try:
        assert scoring.evaluate_trust_state(con, proven)[1] == "proven"
        _write_adapter({**adapters.load_all()["codex"], "override_seed": True}, "codex")
        label = adapters.route_version("codex", adapters.load_all()["codex"])
        assert label.startswith("user-override-")
        assert scoring.evaluate_trust_state(con, f"codex@{label}/{rest}")[1] == "valid-unverified"
    finally:
        con.close()
    built, _ = candidates.build_candidates(env.con(), "executor", probe=False)
    assert all(c["harness_version"].startswith("user-override-") for c in built if c["harness"] == "codex")


def test_smoke_refuses_a_permission_flag_form_unless_allowed(env, tmp_path):
    _kilo(env)
    _write_adapter(_with(office_profiles__worker__argv=["--yolo", "--model", "{model}"],
                         trust_justifications={"--yolo": "headless kilo cannot answer prompts"}))
    code, out = env.office("harness", "smoke", "kilo", "--model", "acme/fast-1")
    assert code == 1 and "--allow-unsafe-flags" in out
    code, out = env.office("harness", "smoke", "kilo", "--model", "acme/fast-1", "--allow-unsafe-flags")
    assert code == 0, out
    assert json.loads(harnesscmd.smoke_log().read_text().splitlines()[-1])["unsafe_flags"] == ["--yolo"]


def test_smoke_timeout_kills_a_grandchild_holding_the_pipes(unit):
    import time
    hang = unit / "bin" / "kilo"
    hang.write_text(f"#!{sys.executable}\nimport subprocess, sys, time\n"
                    "if sys.argv[1:2] == ['--version']: print('kilo 2.4.1'); sys.exit(0)\n"
                    "if sys.argv[1:2] == ['models']: print('acme/fast-1'); sys.exit(0)\n"
                    "subprocess.Popen(['sleep', '60'])\ntime.sleep(60)\n")
    _write_adapter(GOOD)
    started = time.time()
    res = harnesscmd.smoke("kilo", "acme/fast-1", timeout=1)
    assert res.exit_code == 1 and res.data["result"] == "timed_out"
    assert time.time() - started < 30


def test_overlay_rows_never_duplicate_an_existing_row(unit):
    seed = [r for r in candidates.catalog_rows() if not r.get("alias_family")][0]
    key = {k: seed[k] for k in ("invocation_harness", "model_id", "effort")}
    user_catalog.save({"models": [{**key, "invocation_model_id": "evil"}], "disabled": []})
    same = [r for r in candidates.catalog_rows() if all(r.get(k) == v for k, v in key.items())]
    assert len(same) == 1 and same[0].get("invocation_model_id") != "evil"
    assert any("duplicates" in line for line in harnesscmd.model_list().lines)
    user_catalog.save({"models": [{**key, "dispatchable": False}], "disabled": []})
    same = [r for r in candidates.catalog_rows() if all(r.get(k) == v for k, v in key.items())]
    assert len(same) == 1 and same[0]["dispatchable"] is False


def test_status_warns_and_model_commands_name_runs_whose_routing_inputs_drifted(env):
    from conftest import approved_run
    run_id = approved_run(env)
    code, out = env.office("status")
    assert "routing catalog" not in out, out
    code, out = env.office("model", "disable", "codex/luna")
    assert code == 0 and "active runs whose pinned routing inputs now differ" in out, out
    code, out = env.office("status")
    assert "routing catalog changed since run" in out, out


def test_seed_justifications_live_outside_the_seed_adapters(unit):
    assert "trust_justifications" not in adapters.load_all()["codex"]
    assert "--yolo" in harnesscmd.packaged_justifications("codex")


def test_snapshot_hashes_never_raise_on_a_malformed_user_adapter(unit):
    base = config.snapshot_hashes()
    d = adapters.user_adapter_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "broken.yaml").write_text("id: [unclosed\n")
    assert config.snapshot_hashes() == base
    user_catalog.path().write_text("models: [unclosed\n")
    assert config.snapshot_hashes()["catalog_hash"] != base["catalog_hash"]  # hashed by bytes, no raise


def test_a_corrupt_overlay_is_never_overwritten(unit):
    _write_adapter(GOOD)
    user_catalog.path().parent.mkdir(parents=True, exist_ok=True)
    user_catalog.path().write_text("models: [unclosed\n")
    import pytest as _pytest
    from office.state import OfficeError
    for call in (lambda: harnesscmd.model_add("kilo/acme/fast-1", ["medium"]),
                 lambda: harnesscmd.model_disable("codex/luna")):
        with _pytest.raises(OfficeError) as err:
            call()
        assert str(user_catalog.path()) in err.value.message
    assert user_catalog.path().read_text() == "models: [unclosed\n"


def test_drafts_and_shadowed_files_do_not_change_the_adapter_hash(unit):
    base = config.snapshot_hashes()["adapter_hash"]
    _write_adapter(_with(source_notes="TODO fill"), "kilo")
    _write_adapter(dict(GOOD, id="codex"), "codex")  # a seed id without override_seed: ignored
    assert config.snapshot_hashes()["adapter_hash"] == base
    assert "kilo" not in adapters.load_all()
    assert harnesscmd.validate("kilo").exit_code == 1  # the draft is still validated directly
    _write_adapter(GOOD, "kilo")
    assert config.snapshot_hashes()["adapter_hash"] != base
