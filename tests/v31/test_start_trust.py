"""F3 (reworked by #837): `office start` reports role policy, and trust history is audit-only evidence."""
from __future__ import annotations

import re

AUDIT = "legacy trust evidence (audit only) "


def _audit_lines(out):
    return [l for l in out.splitlines() if l.startswith(AUDIT)]


def test_start_prints_one_policy_line_and_no_trust_prompt(env):
    code, out = env.office("start", "fixture goal", "--planner", "inline")
    assert code == 0, out
    lines = [l for l in out.splitlines() if l.startswith("routing policy:")]
    assert len(lines) == 1 and "office route policy --effective" in lines[0], out
    assert "approve trust" not in out and not _audit_lines(out), out
    assert not [l for l in out.splitlines() if l.startswith("trust ")], out


def test_inspect_trust_lists_audit_evidence_and_never_an_approve_command(env):
    code, out = env.office("start", "fixture goal", "--planner", "inline")
    assert code == 0, out
    code, out = env.office("inspect", "trust")
    assert code == 0, out
    lines = _audit_lines(out)
    assert any(l.startswith(AUDIT + "executor ") for l in lines), out
    assert "approve trust" not in out and "quote" not in out.lower(), out
    for l in lines:
        assert re.match(re.escape(AUDIT) + r"\S+ \S+: \S+", l), l


def test_derived_state_is_reported_for_a_trusted_install_without_gating(env):
    env.trust()
    code, out = env.office("start", "fixture goal", "--planner", "inline")
    assert code == 0, out
    code, out = env.office("inspect", "trust")
    executor = [l for l in _audit_lines(out) if l.startswith(AUDIT + "executor ")]
    assert executor and all(l.endswith(": proven") for l in executor), out


def test_visual_route_without_a_current_vision_proof_says_so(env):
    """#211: a vision proof on an older harness version no longer counts, and the audit line says why."""
    from office import candidates
    env.trust()
    con = env.con()
    cands, _ = candidates.build_candidates(con, "visual_reviewer", probe=False)
    assert cands
    c = cands[0]
    con.execute("INSERT INTO capability_proofs(key, harness, harness_version, model, effort, adapter_hash, capability, "
                "result, details, proved_at) VALUES('stale', ?, '0.0.1', ?, ?, 'sha256:old', 'vision', 'pass', '', "
                "'2026-09-27T00:00:00+00:00')", (c["harness"], c["invocation_model_id"], c["effort"]))
    code, out = env.office("start", "fixture goal", "--planner", "inline")
    assert code == 0, out
    code, out = env.office("inspect", "trust")
    assert code == 0, out
    visual = [l for l in _audit_lines(out) if l.startswith(AUDIT + "visual_reviewer ")]
    assert visual and all("vision" in l and "office doctor --probe-vision" in l for l in visual), out
    assert any("last pass on 0.0.1, 2026-09-27" in l for l in visual), out
    assert any("never probed" in l for l in visual) == (len(visual) > 1), out


def test_inspect_trust_is_listed_in_help_and_unknown_view_hint(env):
    code, out = env.office("start", "fixture goal", "--planner", "inline")
    assert code == 0, out
    code, out = env.office("inspect", "nope")
    assert code != 0 and "learner|trust|convergence" in out, out
    from office import cli
    assert "learner|trust|convergence" in cli.PRIMARY
