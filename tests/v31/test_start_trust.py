"""F3: office start reports derived trust for each trust-gated role's routes."""
from __future__ import annotations

import re


def _trust_lines(out):
    return [l for l in out.splitlines() if l.startswith("trust ")]


def test_fresh_install_lists_routes_with_approve_command(env):
    code, out = env.office("start", "fixture goal", "--planner", "inline")
    assert code == 0, out
    lines = _trust_lines(out)
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
    executor = [l for l in _trust_lines(out) if l.startswith("trust executor ")]
    assert executor and all(l.endswith(": proven") for l in executor), out
