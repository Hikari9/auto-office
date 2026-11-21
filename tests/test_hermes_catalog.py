"""Hermes catalog rows: benchmark inheritance, provider scoping and no change to other harnesses.

A fake `hermes` on PATH stands in for the CLI; nothing here calls a model.
"""
from __future__ import annotations

import csv
import stat
import textwrap

import pytest

from office import adapters, candidates, route_policy, routecmd, shipped_catalog

AUTH_WITHOUT_OPENAI = textwrap.dedent("""\
    anthropic (1 credentials):
      #1  anthropic-oauth-1    oauth   id=216f00 priority=0 hermes_pkce <-

    gemini (1 credentials):
      #1  GOOGLE_API_KEY       api_key id=dcfef9 priority=0 env:GOOGLE_API_KEY <-
    """)
AUTH_WITH_OPENAI = AUTH_WITHOUT_OPENAI + "\nopenai (1 credentials):\n  #1  OPENAI_API_KEY  api_key id=5064ff priority=0 env:OPENAI_API_KEY\n"


def _fake_hermes(tmp_path, monkeypatch, auth: str):
    exe = tmp_path / "bin" / "hermes"
    exe.parent.mkdir(exist_ok=True)
    exe.write_text(f"#!/bin/sh\nif [ \"$1\" = auth ]; then cat <<'EOF'\n{auth}EOF\nelse echo 'Hermes Agent v0.23.3'; fi\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{exe.parent}:/usr/bin:/bin")
    monkeypatch.setenv("OFFICE_USER_ADAPTERS", str(tmp_path / "no-user-adapters"))
    route_policy.reset_provider_cache()
    yield
    route_policy.reset_provider_cache()


@pytest.fixture
def hermes_without_openai(tmp_path, monkeypatch):
    yield from _fake_hermes(tmp_path, monkeypatch, AUTH_WITHOUT_OPENAI)


@pytest.fixture
def hermes_with_openai(tmp_path, monkeypatch):
    yield from _fake_hermes(tmp_path, monkeypatch, AUTH_WITH_OPENAI)


def _rows():
    return shipped_catalog.load()["models"]


def _hermes_rows(rows=None):
    return [r for r in (rows or candidates.catalog_rows()) if r["invocation_harness"] == "hermes"]


def _index():
    return shipped_catalog.load()["benchmark_index_version"]


def test_hermes_rows_exist_with_provider_pinned_slugs():
    rows = _hermes_rows()
    assert rows
    for r in rows:
        provider, _, model = r["invocation_model_id"].partition(":")
        assert provider in {"anthropic", "openai", "gemini"} and model == r["model_id"], r["invocation_model_id"]


def test_hermes_covers_the_named_benchmarked_models_with_provider_qualified_slugs():
    slugs = {r["invocation_model_id"] for r in _hermes_rows()}
    for effort in ("low", "medium", "high", "xhigh", "max"):
        assert f"openai:gpt-6-astra" in slugs
        assert any(r["model_id"] == "gpt-6-astra" and r["effort"] == effort for r in _hermes_rows())
    assert not [r for r in _hermes_rows() if r["model_id"] == "gemini-3.8-flash"], "retired in #1284"
    assert any(r["invocation_model_id"] == "gemini:gemini-4-argon" and r["effort"] == "high" for r in _hermes_rows())


def test_every_hermes_row_has_a_canonical_benchmark_under_another_harness():
    # A canonical row has some benchmark score; the current index (AA Engineering Index)
    # may be absent for it, and then the Hermes row inherits that absence (see below).
    canonical = {(r["model_id"], r["effort"]) for r in _rows()
                 if r["invocation_harness"] != "hermes" and not r.get("alias_family")
                 and any(v is not None for v in (r.get("benchmark_indexes") or {}).values())}
    for r in _hermes_rows(_rows()):
        assert (r["model_id"], r["effort"]) in canonical, f"hermes/{r['model_id']}@{r['effort']} has no canonical benchmark"


def test_hermes_score_equals_the_canonical_score_at_the_same_effort_without_duplicate_rows():
    index = _index()
    rows = candidates.catalog_rows()
    scored = unscored = 0
    for h in _hermes_rows(rows):
        sources = [r for r in rows if r["invocation_harness"] != "hermes" and not r.get("alias_family")
                   and (r["model_id"], r["effort"]) == (h["model_id"], h["effort"])
                   and (r.get("benchmark_indexes") or {}).get(index) is not None]
        inherited = (h.get("benchmark_indexes") or {}).get(index)
        if sources:
            assert {s["benchmark_indexes"][index] for s in sources} == {inherited}
            scored += 1
        else:
            # No canonical score at this index: the Hermes row stays unscored (never 0).
            assert inherited is None, f"hermes/{h['model_id']}@{h['effort']} invented {inherited!r}"
            unscored += 1
    assert scored, "no Hermes row inherited a current-index score"
    with open(shipped_catalog.files()[2], newline="", encoding="utf-8") as f:
        assert not [r for r in csv.DictReader(f, delimiter="\t") if r["route_key"].startswith("hermes/")]


def test_a_hermes_row_without_a_canonical_source_stays_unscored():
    rows = [{"model_id": "m", "invocation_harness": "hermes", "effort": "high", "benchmark_indexes": {}},
            {"model_id": "m", "invocation_harness": "claude", "effort": "low", "benchmark_indexes": {"i": 1}}]
    assert candidates.inherit_benchmarks(rows)[0]["benchmark_indexes"] == {}


def test_inheritance_ignores_aliases_and_other_efforts():
    rows = [{"model_id": "m", "invocation_harness": "hermes", "effort": "high"},
            {"model_id": "m", "invocation_harness": "claude", "effort": "low", "benchmark_indexes": {"i": 1}},
            {"model_id": "m", "invocation_harness": "codex", "effort": "high", "alias_family": "x", "benchmark_indexes": {"i": 2}},
            {"model_id": "m", "invocation_harness": "agy", "effort": "high", "benchmark_indexes": {"i": 3}}]
    assert candidates.inherit_benchmarks(rows)[0]["benchmark_indexes"] == {"i": 3}


def test_other_harness_candidate_rows_are_unchanged_by_the_hermes_rows():
    """The rows other harnesses see are what the same pipeline yields with no hermes rows in the catalog."""
    from office import model_lineages, user_catalog
    others = [r for r in _rows() if r["invocation_harness"] != "hermes"]
    without = model_lineages.annotate(candidates.resolve_aliases(user_catalog.apply(list(others))))
    now = [r for r in candidates.catalog_rows() if r["invocation_harness"] != "hermes"]
    assert now == without


def _built(role="executor"):
    return candidates.build_candidates(None, role, probe=False, user_policies=[], quota_snapshot={})


def test_unreachable_provider_makes_the_route_ineligible_with_a_reason(hermes_without_openai):
    built, skipped = _built()
    by_label = {s["candidate"]: s for s in skipped}
    gone = [s for s in skipped if s["candidate"].startswith("hermes/gpt-")]
    assert gone and all(s["category"] == "provider-unreachable" and "openai" in s["reason"] for s in gone)
    assert not [c for c in built if c["harness"] == "hermes" and c["model_id"].startswith("gpt-")]
    assert by_label["hermes/gpt-6.1-sol@high"]["reason"].startswith("provider-unreachable")


def test_reachable_provider_is_not_marked_unreachable(hermes_without_openai):
    _, skipped = _built()
    assert not [s for s in skipped if s["candidate"].startswith("hermes/claude-")
                and s["category"] == "provider-unreachable"]


def test_openai_credential_clears_the_block(hermes_with_openai):
    _, skipped = _built()
    assert not [s for s in skipped if s["category"] == "provider-unreachable"]


def test_unreachable_provider_is_visible_in_effective_policy(hermes_without_openai):
    result = routecmd.policy(role="executor", effective=True)
    lines = [ln for ln in result.lines if "hermes/gpt-6.1-sol@high" in ln]
    assert lines and "provider-unreachable" in lines[0] and "does not dispatch" in lines[0]
    ok = [ln for ln in result.lines if "hermes/claude-sonnet-5-5@high" in ln]
    assert ok and "provider-unreachable" not in ok[0]


def test_a_user_allow_does_not_lift_an_unreachable_provider(hermes_without_openai):
    cand = {"harness": "hermes", "model_id": "gpt-6.1-sol", "effort": "high", "invocation_model_id": "openai:gpt-6.1-sol"}
    allow = route_policy.user_policy({"routing": {"user_policy": {"role_rules": [
        {"role": "executor", "route": "hermes/gpt-6.1-sol@high", "state": "allow"}]}}})
    assert route_policy.effective_role_state(cand, "executor", [allow])["source"] == "provider-unreachable"


def test_absent_hermes_is_not_a_provider_problem(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    route_policy.reset_provider_cache()
    cand = {"harness": "hermes", "invocation_model_id": "openai:gpt-6.1-sol"}
    assert route_policy.provider_unreachable(cand) is None
    assert route_policy.provider_unreachable({"harness": "hermes", "invocation_model_id": "bare"}) is None
    assert route_policy.provider_unreachable({"harness": "codex", "invocation_model_id": "openai:x"}) is None


def test_gemini_hermes_routes_follow_the_gemini_credential(hermes_without_openai):
    _, skipped = _built()
    assert [s for s in skipped if s["candidate"].startswith("hermes/gemini-")]
    assert not [s for s in skipped if s["candidate"].startswith("hermes/gemini-") and s["category"] == "provider-unreachable"]
    assert [s for s in skipped if s["candidate"].startswith("hermes/gpt-6-astra") and s["category"] == "provider-unreachable"]
