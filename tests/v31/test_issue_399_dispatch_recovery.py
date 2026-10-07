"""Regression coverage for issue #399 dispatch recovery."""
from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import yaml


def test_codex_headless_review_does_not_clobber_the_reply_file():
    root = Path(__file__).resolve().parents[2]
    codex = yaml.safe_load((root / "adapters" / "seed" / "codex.yaml").read_text())
    for role in ("reviewer", "vision"):
        argv = codex["office_profiles"][role]["argv"]
        assert "-o" not in argv, (role, argv)
        assert "{output}" not in argv, (role, argv)


def test_headless_invalid_result_does_not_claim_it_reprompted_or_kept_a_pane(tmp_path):
    from office import gates

    output = tmp_path / "reply.txt"
    d = {"id": "D399", "triple": "codex/test@high", "launcher": "process-fallback"}
    parsed = SimpleNamespace(errors=["no VERDICT line"])
    _, _, reason = gates._reprompt_until_valid(None, {"gates": {"review_reprompt_max": 3}}, d, tmp_path, output,
                                                parsed, plan_review=False, visual=False)
    assert "headless process-fallback" in reason
    assert "after re-prompting" not in reason
    assert "pane is kept" not in reason
    assert "no live reviewer pane exists" in reason
    assert "output.log" in reason
    assert "office resume" in reason
