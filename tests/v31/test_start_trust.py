"""F3: office start reports derived trust for each trust-gated role's routes."""
from __future__ import annotations

import re


def _trust_lines(out):
    return [l for l in out.splitlines() if l.startswith("trust ")]


def _summary(out):
    return [l for l in _trust_lines(out) if re.match(r"trust \S+: ", l)]


def _detail(out):
    return [l for l in _trust_lines(out) if not re.match(r"trust \S+: ", l)]


def test_default_start_prints_one_summary_line_per_role(env):
    code, out = env.office("start", "fixture goal", "--planner", "inline")
    assert code == 0, out
    lines = _trust_lines(out)
    assert lines and lines == _summary(out), out
    roles = [l.split()[1].rstrip(":") for l in lines]
    assert len(roles) == len(set(roles)), out
    assert "office approve trust" not in out, out
    assert "office start" not in "".join(lines), out
    for l in lines:
        if "no candidate routes" in l:
            continue
        m = re.match(r"trust \S+: (\d+) routes \((.+)\) \| per-route detail: office inspect trust$", l)
        assert m, l
        assert sum(int(p.split()[0]) for p in m.group(2).split(", ")) == int(m.group(1)), l


def test_fresh_install_lists_routes_with_approve_command(env):
    code, out = env.office("start", "fixture goal", "--planner", "inline")
    assert code == 0, out
    code, out = env.office("inspect", "trust")
    assert code == 0, out
    lines = _detail(out)
    assert any(l.startswith("trust executor ") for l in lines), out
    routed = [l for l in lines if "no candidate routes" not in l]
    assert routed, out
    for l in routed:
        m = re.match(r"trust \S+ (\S+): (\S+)", l)
        assert m, l
        triple, state = m.groups()
        # A shipped trust baseline may make some routes proven on a fresh install;
        # every route that is not proven must name the exact approval command.
        if state == "proven":
            assert "office approve trust" not in l, l
        else:
            assert f"office approve trust {triple} --quote" in l, l
    assert any(": proven" not in l for l in routed), out


def test_proven_route_has_no_approve_command(env):
    env.trust()
    code, out = env.office("start", "fixture goal", "--planner", "inline")
    assert code == 0, out
    code, out = env.office("inspect", "trust")
    assert code == 0, out
    executor = [l for l in _detail(out) if l.startswith("trust executor ")]
    assert executor and all(l.endswith(": proven") for l in executor), out


def test_visual_route_without_a_current_vision_proof_says_so(env):
    """#211: trust `proven` is not vision. A proof on an older harness version
    no longer counts, and the trust line must say why the route is rejected."""
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
    visual = [l for l in _detail(out) if l.startswith("trust visual_reviewer ")]
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
