"""#1185: a pi pin never passes a bare model alias; pi's --model needs provider/model."""
from __future__ import annotations

from pathlib import Path

import pytest

from office import adapters, candidates
from office.state import Refused

LUNA = {"model_id": "gpt-6-luna", "invocation_model_id": "openai/gpt-6-luna", "invocation_harness": "pi",
        "effort": "high"}


def _rows(monkeypatch, *rows):
    base = [r for r in candidates.catalog_rows() if r.get("invocation_harness") != "pi"]
    from office import model_lineages
    monkeypatch.setattr(candidates, "catalog_rows", lambda: model_lineages.annotate(base + list(rows)))


def _argv(model, effort):
    adapter = adapters.load_all()["pi"]
    argv, _ = adapters.build_argv(adapter, "worker", model=model, effort=effort, cwd=Path("."))
    return argv


def test_bare_alias_resolves_to_the_one_registered_pi_slug(monkeypatch):
    _rows(monkeypatch, LUNA)
    cand = candidates.declared_candidate("pi", "luna", "high")
    assert cand["invocation_model_id"] == "openai/gpt-6-luna"
    argv = _argv(cand["invocation_model_id"], cand["effort"])
    assert argv[argv.index("--model") + 1] == "openai/gpt-6-luna"


def test_bare_alias_with_no_registered_row_is_refused_with_the_qualified_form(monkeypatch):
    _rows(monkeypatch)
    with pytest.raises(Refused) as err:
        candidates.declared_candidate("pi", "luna", "high")
    assert "provider-qualified" in str(err.value) and "pi/<provider>/luna" in (err.value.next_step or "")


def test_several_matching_slugs_are_refused(monkeypatch):
    _rows(monkeypatch, LUNA, {**LUNA, "model_id": "gpt-5.6-luna", "invocation_model_id": "openrouter/gpt-5.6-luna"})
    with pytest.raises(Refused) as err:
        candidates.declared_candidate("pi", "luna", "high")
    assert "openai/gpt-6-luna" in str(err.value) and "openrouter/gpt-5.6-luna" in str(err.value)


def test_qualified_slug_and_other_harnesses_are_unchanged(monkeypatch):
    _rows(monkeypatch, LUNA)
    assert candidates.declared_candidate("pi", "openai/gpt-6-luna", "high")["invocation_model_id"] == "openai/gpt-6-luna"
    assert candidates.declared_candidate("codex", "gpt-6-luna", "high")["invocation_model_id"] == "gpt-6-luna"
