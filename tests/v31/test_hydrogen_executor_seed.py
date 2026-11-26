"""Owner-approved advisory XS-XL candidate seeds remain gated pending exact Hydrogen qualification."""
from pathlib import Path

import pytest
from office import adapters, executor_slates, model_lineages, route_policy, routing, shipped_catalog

EXPECTED = {
    "XS": [("claude-haiku-5-5", "high", "claude"), ("gpt-6.1-terra", "low", "codex"), ("gemini-4-hydrogen", "low", "agy")],
    "S": [("claude-sonnet-5-5", "medium", "claude"), ("gpt-6.1-terra", "medium", "codex"), ("gemini-4-hydrogen", "medium", "agy")],
    "M": [("claude-sonnet-5-5", "high", "claude"), ("gemini-4-hydrogen", "high", "agy"), ("gpt-6.1-terra", "high", "codex")],
    "L": [("claude-sonnet-5-5", "high", "claude"), ("claude-sonnet-5-5", "high", "hermes"), ("claude-sonnet-5-5", "high", "agy"),
          ("gpt-6.1-terra", "high", "codex")],
    "XL": [("claude-opus-5-5", "high", "claude"), ("gpt-6.2-sol", "high", "codex"), ("gemini-4-neon", "high", "agy")],
}
ENGINEERING_INDEX = "Artificial Analysis Engineering Index (2026-11)"
# Hydrogen's published Engineering Index score per effort (research/aa-v5.0.1) and the sizes whose floor it clears.
HYDROGEN_SCORE = {"low": (36.29, ["XS"]), "medium": (49.07, ["XS", "S", "M", "L"]),
                  "high": (56.95, ["XS", "S", "M", "L", "XL"]), "xhigh": (60.10, ["XS", "S", "M", "L", "XL"])}


def test_owner_preference_intent_and_non_dispatchable_hydrogen():
    catalog = shipped_catalog.load()
    routes = {shipped_catalog.key(row): row for row in catalog["models"]}
    index = catalog["benchmark_index_version"]
    assert index == ENGINEERING_INDEX
    for effort in ("low", "medium", "high", "xhigh"):
        row = routes[f"agy/gemini-4-hydrogen@{effort}"]
        assert row["dispatchable"] is False
        assert row["discovery"] == "eligible"
        assert (row.get("benchmark_indexes") or {}).get(index) is not None, "AA v5 publishes a score for every effort"
    policy = executor_slates.load_policy()
    assert policy["benchmark_index"] == index
    for size, expected in EXPECTED.items():
        actual = [(r["model_id"], r["effort"], r.get("harness")) for r in policy["executor_preferred"][size]]
        assert actual == expected
        assert not any(item["model_id"] == "gemini-3.8-flash" for item in policy["executor_preferred"][size])
    assert policy["model_restrictions"][0]["allowed_sizes"] == ["XL"]
    # Rico 2026-11-19: Flash is retired by a shipped deny in every role; its catalog rows stay as history.
    assert routes["agy/gemini-3.8-flash@medium"]["dispatchable"] is True

def test_packaged_hydrogen_catalog_parity():
    root = Path(__file__).resolve().parents[2]
    for name in ("routes.tsv", "benchmarks.tsv", "provenance.yaml", "lineages.yaml", "executor-policy.yaml"):
        assert (root/"catalog"/name).read_bytes() == (root/"plugin-dist"/"catalog"/name).read_bytes()
    for name in ("hydrogen-executor-seed-2026-11-14.md",
                 "owner-proposed-executor-pools-2026-11-14.md",
                 "hydrogen-capped-trials-engineering-handoff-2026-11-14.md"):
        assert (root/"docs/benchmarks"/name).read_bytes() == (root/"plugin-dist/docs/benchmarks"/name).read_bytes()


@pytest.mark.parametrize(
    "effort, invocation_slug, aa_slug",
    [
        ("low", "gemini-4-hydrogen-low", "gemini-4-hydrogen-low"),
        ("medium", "gemini-4-hydrogen-medium", "gemini-4-hydrogen-medium"),
        ("high", "gemini-4-hydrogen-high", "gemini-4-hydrogen-high"),
        ("xhigh", "gemini-4-hydrogen-xhigh", "gemini-4-hydrogen"),
    ],
)
def test_hydrogen_exact_effort_registration_is_unproven_not_qualified(effort, invocation_slug, aa_slug):
    """Static catalog/argv evidence only; this is not an authenticated agy conformance receipt."""
    rows = {shipped_catalog.key(r): r for r in shipped_catalog.load()["models"]}
    row = rows[f"agy/gemini-4-hydrogen@{effort}"]
    assert row["invocation_model_id"] == invocation_slug
    assert row["source_identifier"] == aa_slug
    assert row["source_effort"] == effort
    assert row["effort_confidence"] == "exact"
    assert row["invocation_source"].startswith("external-evidence:")
    assert routing.invocation_provenance(row) == "documented", "third-party slug is not local agy proof"
    assert row["dispatchable"] is False and row["discovery"] == "eligible"
    assert row["release_date"] == "2026-11-12"
    scores = row["benchmark_indexes"]
    assert scores["Artificial Analysis Intelligence Index v4.3.2"] is None, "v4.3.2 history is not rescored"
    score, clears = HYDROGEN_SCORE[effort]
    assert scores[ENGINEERING_INDEX] == pytest.approx(score, abs=0.01)
    assert row["source_snapshot_metadata"]["reference_index"] == "Artificial Analysis Intelligence Index v5.0.1"
    assert row["price_fields"]["input_per_mtok"] == 1.5
    assert row["price_fields"]["output_per_mtok"] == 7.5
    assert row["price_fields"]["as_of"] == "2026-11-12"
    for size in ("XS", "S", "M", "L", "XL"):
        assert (executor_slates.benchmark_gate(row, size) is None) is (size in clears), size
    argv, _ = adapters.build_argv(
        adapters.load_all()["agy"], "worker", model=invocation_slug,
        effort=effort, cwd=Path("/w")
    )
    assert argv[argv.index("--model") + 1] == invocation_slug
    assert argv.count(invocation_slug) == 1
    assert "--effort" not in argv


def test_hydrogen_has_no_shipped_role_allow_benchmark_waiver_or_visual_promotion():
    policy = executor_slates.load_policy()
    assert all(rule["model_id"] != "gemini-4-hydrogen" for rule in policy["floor_exceptions"])
    assert all(
        not (rule["state"] == "allow" and "gemini-4-hydrogen" in rule["route"])
        for rule in route_policy.SEED_ROLE_RULES
    )
    for effort in ("low", "medium", "high", "xhigh"):
        candidate = {
            "harness": "agy", "harness_version": "1.4.3",
            "model_id": "gemini-4-hydrogen",
            "invocation_model_id": f"gemini-4-hydrogen-{effort}",
            "effort": effort,
        }
        for size in ("XS", "S", "M", "L", "XL"):
            assert route_policy.effective_role_state(candidate, "executor", [], size)["state"] != "allow"
        for role in ("code_reviewer", "plan_reviewer", "visual_reviewer"):
            assert route_policy.effective_role_state(candidate, role, [])["state"] != "allow"
    # Rico 2026-11-19: Gemini 3.8 Flash is retired; the shipped policy denies it at every effort in every role.
    flash = [rule for rule in route_policy.SEED_ROLE_RULES if "gemini-3.8-flash" in rule["route"]]
    assert flash and all(rule["state"] == "deny" and "sizes" not in rule for rule in flash)
    assert {rule["role"] for rule in flash} == set(route_policy.POLICY_ROLES)


def test_hydrogen_has_distinct_released_lineage_without_aliasing_flash_or_argon():
    lineage = model_lineages.load()["gemini-4-hydrogen"]
    assert lineage["lineage_key"] == "google/gemini-hydrogen"
    assert lineage["generation_version"] == "4"
    assert lineage["release_status"] == "released"
    assert lineage["lineage_key"] != model_lineages.load()["gemini-4-argon"]["lineage_key"]


def test_third_party_invocation_provenance_is_not_falsely_proven():
    assert routing.invocation_provenance({
        "invocation_model_id": "gemini-4-hydrogen-medium",
        "invocation_source": "external-evidence: third-party listing; local agy untested",
    }) == "documented"
    assert routing.invocation_provenance({
        "invocation_model_id": "gemini-4-hydrogen-medium",
        "invocation_source": "local-evidence:agy exact model banner and effort",
    }) == "proven"
