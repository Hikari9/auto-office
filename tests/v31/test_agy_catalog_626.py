"""#626: catalog agy's Claude display names without granting route trust."""
from __future__ import annotations

import yaml

from conftest import approved_run
from office import adapters, candidates, paths, scoring


DISPLAY = {
    ("claude-sonnet-5-5", "low"): "Claude Sonnet 5.5 (Low)",
    ("claude-sonnet-5-5", "medium"): "Claude Sonnet 5.5 (Medium)",
    ("claude-sonnet-5-5", "high"): "Claude Sonnet 5.5 (High)",
    ("claude-opus-5-5", "low"): "Claude Opus 5.5 (Low)",
    ("claude-opus-5-5", "medium"): "Claude Opus 5.5 (Medium)",
    ("claude-opus-5-5", "high"): "Claude Opus 5.5 (High)",
}


# Launch evidence is recorded per row; a row whose banner never named it stays gated.
UNVERIFIED_LAUNCH = {("claude-opus-5-5", "low")}


def _seed():
    return yaml.safe_load((paths.resources_root() / "catalog/seed.yaml").read_text())


def test_agy_claude_rows_use_local_display_names_and_match_benchmarks():
    rows = _seed()["models"]
    agy = {(r["model_id"], r["effort"]): r for r in rows
           if r.get("invocation_harness") == "agy" and r.get("model_id") in {
               "claude-sonnet-5-5", "claude-opus-5-5"}}
    claude = {(r["model_id"], r["effort"]): r for r in rows
              if r.get("invocation_harness") == "claude" and r.get("model_id") in {
                  "claude-sonnet-5-5", "claude-opus-5-5"}}

    assert set(agy) == set(DISPLAY)
    for key, display_name in DISPLAY.items():
        row = agy[key]
        assert row["invocation_model_id"] == display_name
        if key in UNVERIFIED_LAUNCH:
            assert row["invocation_source"] == "local-evidence:agy models"
            assert row["dispatchable"] is False
            assert row["discovery"] == "eligible"
        else:  # dispatchable only with a launch whose banner named this exact model (see the row comment)
            assert row["invocation_source"].startswith("local-evidence:agy 1.4.3 startup banner")
            assert row["dispatchable"] is True
        if key == ("claude-sonnet-5-5", "low"):
            assert row["benchmark_indexes"] == {}
            assert "no published" in row["source_snapshot_metadata"]["notes"].lower()
            assert row["source_identifier"] is None
        else:
            source = claude[key]
            for field in ("source_identifier", "source_name", "source_effort", "effort_confidence",
                          "benchmark_indexes", "price_fields", "speed_fields", "release_date",
                          "source_snapshot_metadata"):
                assert row[field] == source[field], (key, field)

    trusted = yaml.safe_load((paths.resources_root() / "catalog/trust-baseline.yaml").read_text())
    assert not any("claude-" in route["triple"] and route["triple"].startswith("agy@")
                   for route in trusted["routes"])


def test_agy_build_argv_keeps_display_name_as_one_model_argument(tmp_path):
    candidate = candidates.declared_candidate("agy", "claude-sonnet-5-5", "medium")
    assert candidate["invocation_model_id"] == "Claude Sonnet 5.5 (Medium)"
    argv, _ = adapters.build_argv(adapters.load_all()["agy"], "worker",
                                  model=candidate["invocation_model_id"], effort="medium", cwd=tmp_path)
    model_index = argv.index("--model")
    assert argv[model_index + 1] == "Claude Sonnet 5.5 (Medium)"
    assert argv.count("Claude Sonnet 5.5 (Medium)") == 1


def test_amend_route_accepts_cataloged_agy_sonnet_through_isolated_home(request):
    env = request.getfixturevalue("env")
    approved_run(env, executor=[{"write": {"calc.py": "def add(a, b):\n    return a + b\n"},
                                  "submit": False}] * 3)
    code, output = env.office("amend", "route", "T1", "--as", "agy/claude-sonnet-5-5@medium",
                              "--quote", "Use the listed Agy Claude model")
    assert code == 0, output
    assert "route" in output and "agy@1/claude-sonnet-5-5@medium" in output, output
    task = env.con().execute("SELECT route_json FROM tasks WHERE id='T1'").fetchone()
    assert task and "Claude Sonnet 5.5 (Medium)" in task["route_json"]


def test_unknown_model_refusal_lists_catalog_add_remediation(request):
    env = request.getfixturevalue("env")
    approved_run(env, executor=[{"write": {"calc.py": "def add(a, b):\n    return a + b\n"},
                                  "submit": False}] * 3)
    code, output = env.office("amend", "route", "T1", "--as", "agy/not-in-catalog@medium",
                              "--quote", "Try a model that is not cataloged")
    assert code == 2
    assert "office model add <harness>/<model> --effort <efforts>" in output
    assert "names reported by <harness> models need a catalog row" in output


def test_approve_trust_resolves_agy_alias_and_refuses_unknown_without_writing(request):
    # claude-opus-5-5@low is the one Agy Claude row still gated, so its trust act reports the remaining gate.
    env = request.getfixturevalue("env")
    approved_run(env, executor=[{"write": {"calc.py": "def add(a, b):\n    return a + b\n"},
                                  "submit": False}] * 3)

    route = "agy@1/claude-opus-5-5@low"
    alias = "agy/claude-opus-5-5@low"
    con = env.con()
    canonical_before = con.execute("SELECT COUNT(*) FROM adapter_trust_acts WHERE triple=?", (route,)).fetchone()[0]
    alias_before = con.execute("SELECT COUNT(*) FROM adapter_trust_acts WHERE triple=?", (alias,)).fetchone()[0]
    code, output = env.office("approve", "trust", "agy/claude-opus-5-5@low",
                              "--quote", "Approve this cataloged Agy route")
    assert code == 0, output
    assert f"{route} trust recorded as proven" in output
    assert "remaining gate: conformance probe required" in output
    assert "remedy: office doctor --probe-route agy/claude-opus-5-5@low" in output
    canonical_after = con.execute("SELECT COUNT(*) FROM adapter_trust_acts WHERE triple=?", (route,)).fetchone()[0]
    alias_after = con.execute("SELECT COUNT(*) FROM adapter_trust_acts WHERE triple=?", (alias,)).fetchone()[0]
    assert canonical_after == canonical_before + 1
    assert alias_after == alias_before
    _, trust = scoring.evaluate_trust_state(con, route)
    assert trust == "proven"
    recorded = con.execute("SELECT COUNT(*) FROM adapter_trust_acts").fetchone()[0]

    code, output = env.office("approve", "trust", "agy/not-in-catalog@medium",
                              "--quote", "Refuse this unknown route")
    assert code != 0
    assert "unknown route" in output.lower()
    assert con.execute("SELECT COUNT(*) FROM adapter_trust_acts").fetchone()[0] == recorded
