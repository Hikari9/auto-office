"""Tests for #625: trust approval by alias lands on the canonical routing identity."""
from __future__ import annotations

import sqlite3
import pytest

from office import authority, db, scoring
from office.state import OfficeError


@pytest.fixture
def test_db():
    con = sqlite3.connect(":memory:")
    db.migrate(con)
    scoring.ensure_trust_schema(con)
    try:
        yield con
    finally:
        con.close()


def test_alias_approval_evaluates_proven_on_routing_triples(test_db):
    """Positive test: approving an alias records trust on the alias triple and its
    concrete resolution target, so evaluate_trust_state returns proven on both."""
    con = test_db
    run = {"id": "run-alias-test-001"}
    quote = "approve luna medium for production"

    # Luna resolves to gpt-6-luna in the shipped catalog
    res = authority.approve(con, run, "trust", quote, ["codex/luna@medium"])

    assert len(res.lines) == 2
    assert "codex@0/luna@medium trust recorded as proven (attributed to the user)" in res.lines[0]
    assert "codex@0/gpt-6-luna@medium trust recorded as proven (attributed to the user)" in res.lines[1]
    assert res.next == "retry the dispatch that needed it"
    assert res.data.get("trust_act_id") is not None
    assert len(res.data.get("trust_act_ids", [])) == 2

    # Both the alias triple and concrete routing identity evaluate as proven
    _, alias_state = scoring.evaluate_trust_state(con, "codex@0/luna@medium")
    assert alias_state == "proven"

    _, concrete_state = scoring.evaluate_trust_state(con, "codex@0/gpt-6-luna@medium")
    assert concrete_state == "proven"


def test_alias_approval_with_version_records_on_canonical_triples(test_db):
    """Approval with explicit harness version resolves through catalog expansion."""
    con = test_db
    run = {"id": "run-alias-test-002"}
    quote = "approve with harness version"

    res = authority.approve(con, run, "trust", quote, ["codex@0/luna@high"])
    assert any("codex@0/luna@high" in l for l in res.lines)
    assert any("codex@0/gpt-6-luna@high" in l for l in res.lines)

    _, concrete_state = scoring.evaluate_trust_state(con, "codex@0/gpt-6-luna@high")
    assert concrete_state == "proven"


def test_approval_keyed_on_different_alias_or_effort_does_not_apply(test_db):
    """Negative test: approval on a different effort or alias does not promote the route."""
    con = test_db
    run = {"id": "run-alias-neg-001"}
    quote = "approve only low effort luna"

    authority.approve(con, run, "trust", quote, ["codex/luna@low"])

    # codex/luna@low should be proven
    _, low_state = scoring.evaluate_trust_state(con, "codex@0/gpt-6-luna@low")
    assert low_state == "proven"

    # codex/luna@medium must NOT be proven
    _, med_state = scoring.evaluate_trust_state(con, "codex@0/gpt-6-luna@medium")
    assert med_state == "valid-unverified"

    _, med_alias_state = scoring.evaluate_trust_state(con, "codex@0/luna@medium")
    assert med_alias_state == "valid-unverified"

    # Approve unrelated claude/sonnet@high
    authority.approve(con, run, "trust", "approve sonnet high", ["claude/sonnet@high"])
    _, med_state2 = scoring.evaluate_trust_state(con, "codex@0/gpt-6-luna@medium")
    assert med_state2 == "valid-unverified"


def test_already_canonical_triple_keeps_recording_as_before(test_db):
    """An already-canonical concrete triple records exactly as before."""
    con = test_db
    run = {"id": "run-canon-001"}
    quote = "approve canonical concrete route"

    res = authority.approve(con, run, "trust", quote, ["codex@0/gpt-6-luna@medium"])
    assert res.lines == ["codex@0/gpt-6-luna@medium trust recorded as proven (attributed to the user)"]
    assert res.next == "retry the dispatch that needed it"
    assert res.data.get("trust_act_id") is not None

    _, state = scoring.evaluate_trust_state(con, "codex@0/gpt-6-luna@medium")
    assert state == "proven"


def test_refusal_path_writes_nothing_and_names_nearest_or_inspect(test_db):
    """Refusal path for unknown harness/model/effort exits non-zero and writes no trust row."""
    con = test_db
    run = {"id": "run-refusal-001"}
    quote = "attempt to approve unknown"

    # 1. Unknown effort
    with pytest.raises(OfficeError) as exc_info:
        authority.approve(con, run, "trust", quote, ["codex/luna@superfast"])
    err = exc_info.value
    assert err.category == "unknown-route"
    assert "nearest canonical routes" in err.message or err.next_step == "office inspect trust"
    if "nearest canonical routes" in err.message:
        assert "codex@0/luna@" in err.message or "codex@0/gpt-6-luna@" in err.message
    count = con.execute("SELECT COUNT(*) FROM adapter_trust_acts").fetchone()[0]
    assert count == 0

    # 2. Unknown model
    with pytest.raises(OfficeError) as exc_info:
        authority.approve(con, run, "trust", quote, ["codex/nonexistent_model@medium"])
    err = exc_info.value
    assert err.category == "unknown-route"
    assert con.execute("SELECT COUNT(*) FROM adapter_trust_acts").fetchone()[0] == 0

    # 3. Unknown harness
    with pytest.raises(OfficeError) as exc_info:
        authority.approve(con, run, "trust", quote, ["unknown_harness/gpt-6-luna@medium"])
    err = exc_info.value
    assert err.category == "unknown-route"
    assert con.execute("SELECT COUNT(*) FROM adapter_trust_acts").fetchone()[0] == 0

    # 4. Malformed spec (missing effort or invalid syntax)
    with pytest.raises(OfficeError) as exc_info:
        authority.approve(con, run, "trust", quote, ["just-a-bare-name"])
    err = exc_info.value
    assert err.category == "unknown-route"
    assert err.next_step == "office inspect trust"
    assert con.execute("SELECT COUNT(*) FROM adapter_trust_acts").fetchone()[0] == 0


def test_gated_route_output_names_remaining_gates_and_remedies(test_db):
    """The output names any gate that still blocks the route after trust and does not
    say the route is usable when it is not."""
    con = test_db
    run = {"id": "run-gated-001"}
    quote = "approve gated discovery route"

    # 1. Non-dispatchable route (claude/sonnet@medium is non-dispatchable in catalog)
    res_nondispatch = authority.approve(con, run, "trust", quote, ["claude/sonnet@medium"])
    assert any("remaining gate: not dispatchable" in l for l in res_nondispatch.lines)
    assert any("remedy:" in l for l in res_nondispatch.lines)
    assert res_nondispatch.next != "retry the dispatch that needed it"
    assert "usable" not in (res_nondispatch.next or "")

    # 2. Discovery-eligible route requiring conformance probe (codex/gpt-6.1-sol@high)
    res_discovery = authority.approve(con, run, "trust", quote, ["codex/gpt-6.1-sol@high"])
    assert any("remaining gate:" in l for l in res_discovery.lines)
    assert any("conformance probe required" in l or "discovery off or blocked" in l for l in res_discovery.lines)
    assert any("remedy:" in l for l in res_discovery.lines)
    assert res_discovery.next != "retry the dispatch that needed it"
    assert "office doctor --probe-route" in "".join(res_discovery.lines) or "office config routing.discovery.enabled" in "".join(res_discovery.lines)
