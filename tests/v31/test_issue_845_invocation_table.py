"""#845: Office owns the runtime command for each harness at each model/effort, right by construction.

A user allow dispatches a route with no proof, so the command it launches must already be correct. These pin
the exact worker command per harness and sweep every catalog route, allowed or not, for a well-formed one.
"""
from pathlib import Path

import pytest

from office import adapters, candidates

CWD = Path("/w")

# (harness, invocation slug, effort) -> the exact worker argv. A change here is a change to what Office launches.
GOLDEN = {
    ("claude", "claude-sonnet-5-5", "high"):
        ["claude", "-p", "--model", "claude-sonnet-5-5", "--effort", "high", "--permission-mode", "bypassPermissions",
         "--output-format", "text"],
    ("codex", "gpt-6-astra", "high"):
        ["codex", "exec", "--yolo", "--skip-git-repo-check", "-m", "gpt-6-astra", "-c", 'model_reasoning_effort="high"',
         "--cd", "/w", "-"],
    ("agy", "Claude Sonnet 5.5 (High)", "high"):
        ["agy", "--dangerously-skip-permissions", "--print-timeout", "45m", "--model", "Claude Sonnet 5.5 (High)",
         "--add-dir", "/w"],
    ("pi", "anthropic/claude-sonnet-5-5", "high"):
        ["pi", "--print", "--no-approve", "--no-extensions", "--no-mcp", "--model", "anthropic/claude-sonnet-5-5",
         "--thinking", "high", "--no-session"],
}


def slug(row):
    return row.get("invocation_model_id") or row["model_id"]


def argv_for(row):
    adapter = adapters.load_all()[row["invocation_harness"]]
    return adapters.build_argv(adapter, "worker", model=slug(row), effort=row["effort"], cwd=CWD)[0]


@pytest.mark.parametrize("key", sorted(GOLDEN))
def test_the_worker_command_is_pinned_per_harness(key):
    harness, model, effort = key
    argv, _ = adapters.build_argv(adapters.load_all()[harness], "worker", model=model, effort=effort, cwd=CWD)
    assert argv == GOLDEN[key]


def test_opencode_carries_the_effort_in_the_model_variant():
    argv, _ = adapters.build_argv(adapters.load_all()["opencode"], "worker", model="opencode/muse-spark-1.3-contributor-free",
                                  effort="xhigh", cwd=CWD)
    assert argv == ["opencode", "run", "--model", "opencode/muse-spark-1.3-contributor-free#xhigh"]


def test_every_catalog_route_has_a_well_formed_command_even_when_not_proven():
    """A non-dispatchable row is only unproven. If the user allows it, the command it launches is still the slug
    Office derives from the row (its invocation id, else its model id) and the harness's own effort knob."""
    checked = 0
    for row in candidates.catalog_rows():
        harness = row.get("invocation_harness")
        if harness not in adapters.load_all() or not row.get("effort") or not adapters.profile(adapters.load_all()[harness], "worker"):
            continue
        assert slug(row).strip(), f"{harness}/{row['model_id']}@{row['effort']} has no slug"
        argv = argv_for(row)
        assert argv[0] and any(slug(row) in a or row["model_id"] in a for a in argv), (row["model_id"], argv)
        assert not any(a in ("None", "") for a in argv), (row["model_id"], argv)
        checked += 1
    assert checked >= 40
