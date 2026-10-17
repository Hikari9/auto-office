"""Regression cases for #625/#627: trust approval and routing share route identity."""
from __future__ import annotations

import pytest

from office import adapters, paths, scoring


def _start(env):
    code, out = env.office("start", "fixture goal", "--planner", "inline")
    assert code == 0, out


def _major(harness):
    return scoring.harness_major(adapters.route_version(harness, adapters.load_all()[harness]))


@pytest.mark.parametrize("model", ["luna", "gpt-6-luna"])
def test_unversioned_approval_records_the_routing_identity(env, model):
    _start(env)
    canonical = f"codex@{_major('codex')}/{model}@medium"
    code, out = env.office("approve", "trust", f"codex/{model}@medium",
                           "--quote", "I explicitly trust this exact route")
    assert code == 0, out
    assert canonical in out and "resolved from codex/" in out, out
    assert "does not bypass conformance probes" in out, out
    assert scoring.evaluate_trust_state(env.con(), canonical)[1] == "proven"
    stored = env.con().execute(
        "SELECT triple FROM adapter_trust_acts ORDER BY recorded_at DESC LIMIT 1"
    ).fetchone()[0]
    assert stored == canonical


def test_explicit_inspect_identity_is_recorded_verbatim(env):
    _start(env)
    canonical = f"codex@{_major('codex')}/luna@low"
    code, out = env.office("approve", "trust", canonical,
                           "--quote", "I explicitly trust this exact route")
    assert code == 0 and canonical in out, out
    assert scoring.evaluate_trust_state(env.con(), canonical)[1] == "proven"
    assert env.con().execute(
        "SELECT COUNT(*) FROM adapter_trust_acts WHERE triple=?", (canonical,)
    ).fetchone()[0] == 1


@pytest.mark.parametrize("route", [
    "codex/no-such-model@medium",
    "codex/luna@unlisted-effort",
    "codex/luna",
    "codex@99/luna@medium",
    "no-such-harness/luna@medium",
])
def test_nonexistent_or_stale_trust_routes_do_not_record_a_false_approval(env, route):
    _start(env)
    code, out = env.office("approve", "trust", route,
                           "--quote", "I explicitly trust this exact route")
    assert code != 0 and "trust recorded as proven" not in out, out
    con = env.con()
    scoring.ensure_trust_schema(con)
    assert con.execute("SELECT COUNT(*) FROM adapter_trust_acts").fetchone()[0] == 0


def test_alias_approval_does_not_grant_a_different_model(env):
    _start(env)
    major = _major("codex")
    exact = f"codex@{major}/gpt-6-luna@medium"
    scoring.record_trust_act(
        paths.runs_db(), exact, "valid-unverified", "test-user",
        "deliberately do not trust this concrete route",
    )
    code, out = env.office("approve", "trust", "codex/luna@medium",
                           "--quote", "I trust only this alias route")
    assert code == 0, out
    assert scoring.evaluate_trust_state(env.con(), f"codex@{major}/luna@medium")[1] == "proven"
    assert scoring.evaluate_trust_state(env.con(), exact)[1] == "valid-unverified"
