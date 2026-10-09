"""Properties of the pure scoring and route-identity policy: the capability floor, the reward tie-break,
the harness-major identity trust keys on, and preferred-seed ranking.

These hold for any input, so they are stated as properties instead of example tables. The capability
floor and the reward tie-break are one contract shared by the packaged runtime (`office.scoring`) and the
retained 3.0 helper (`scripts/office_scoring.py`); both run the same properties.
"""
from __future__ import annotations

import pytest
from hypothesis import given, strategies as st

import office_scoring
from office import routing, scoring

IMPLEMENTATIONS = pytest.mark.parametrize("impl", [scoring, office_scoring], ids=["office", "scripts"])

# Weakest to strongest. `none` is a floor that asks for nothing.
EFFORTS = ["none", "low", "medium", "high", "xhigh", "max"]
_text = st.text(alphabet="abcdefghij:-.", min_size=1, max_size=8)
_efforts = st.sampled_from(EFFORTS)
_bad_efforts = st.one_of(st.none(), st.just(""), st.sampled_from(["HIGH", "ultra", "3"]), st.integers())
_scores = st.integers(min_value=0, max_value=100)


# --------------------------------------------------------------- capability floor

@IMPLEMENTATIONS
@given(candidate=st.dictionaries(st.sampled_from(["effort", "invocation_source", "benchmark_indexes", "x"]),
                                 st.one_of(st.none(), _text, _efforts), max_size=4),
       floor=st.sampled_from([None, {}]))
def test_no_floor_passes_every_candidate(impl, candidate, floor):
    assert impl.evaluate_capability_floor(candidate, floor) == (True, None)


@IMPLEMENTATIONS
@given(candidate=st.fixed_dictionaries({"effort": st.one_of(_efforts, _bad_efforts)}), minimum=_efforts)
def test_effort_floor_passes_exactly_the_efforts_at_or_above_it(impl, candidate, minimum):
    passed, reason = impl.evaluate_capability_floor(candidate, {"min_effort": minimum})
    effort = candidate["effort"]
    if minimum == "none":
        assert (passed, reason) == (True, None)
    elif effort not in EFFORTS:
        assert not passed and "'effort'" in reason, "an unknown effort fails closed and names the field"
    elif EFFORTS.index(effort) >= EFFORTS.index(minimum):
        assert (passed, reason) == (True, None)
    else:
        assert not passed and "effort below role floor" in reason and effort in reason and minimum in reason


@IMPLEMENTATIONS
@given(bogus=st.text(max_size=6).filter(lambda t: t and t not in EFFORTS))
def test_an_unrecognised_floor_effort_is_an_error_not_a_pass(impl, bogus):
    with pytest.raises(ValueError, match="min_effort"):
        impl.evaluate_capability_floor({"effort": "max"}, {"min_effort": bogus})


@IMPLEMENTATIONS
@given(candidate=st.one_of(st.just({}), st.fixed_dictionaries({"effort": _efforts})), minimum=st.sampled_from(EFFORTS[1:]))
def test_a_candidate_with_no_effort_field_never_meets_an_effort_floor(impl, candidate, minimum):
    if "effort" not in candidate:
        passed, reason = impl.evaluate_capability_floor(candidate, {"min_effort": minimum})
        assert not passed and "effort" in reason


@IMPLEMENTATIONS
@given(prefixes=st.lists(_text, min_size=1, max_size=3, unique=True), suffix=_text, extra=_text,
       source=st.one_of(st.none(), st.just(""), _text))
def test_source_floor_passes_only_a_source_under_an_allowed_prefix(impl, prefixes, suffix, extra, source):
    floor = {"allowed_sources": prefixes}
    assert impl.evaluate_capability_floor({"invocation_source": prefixes[0] + suffix}, floor) == (True, None)
    passed, reason = impl.evaluate_capability_floor({"invocation_source": source}, floor)
    if not source or not any(source.startswith(p) for p in prefixes):
        assert not passed and "invocation_source" in reason
    else:
        assert passed
    passed, reason = impl.evaluate_capability_floor({}, floor)
    assert not passed and "invocation_source" in reason, "a missing source fails closed"


@IMPLEMENTATIONS
@given(name=_text, minimum=st.integers(min_value=1, max_value=100), score=st.one_of(st.none(), _scores),
       other=_scores)
def test_benchmark_floor_needs_a_measured_score_at_or_above_the_minimum(impl, name, minimum, score, other):
    floor = {"min_benchmark_index": {"index_name": name, "min_score": minimum}}
    indexes = {} if score is None else {name: score}
    indexes[name + "-other"] = other  # another index's score never stands in for this one
    passed, reason = impl.evaluate_capability_floor({"benchmark_indexes": indexes}, floor)
    if score is None:
        assert not passed and f"benchmark_indexes.{name}" in reason
    elif score >= minimum:
        assert (passed, reason) == (True, None)
    else:
        assert not passed and name in reason and "below role floor" in reason


@IMPLEMENTATIONS
@given(candidate=st.fixed_dictionaries({"effort": st.one_of(_efforts, st.none()), "invocation_source": st.one_of(st.none(), _text),
                                        "benchmark_indexes": st.one_of(st.none(), st.fixed_dictionaries({"idx": _scores}))}),
       use=st.fixed_dictionaries({"effort": st.booleans(), "source": st.booleans(), "bench": st.booleans()}),
       min_effort=_efforts, prefix=_text, min_score=st.integers(min_value=1, max_value=100))
def test_a_floor_passes_only_when_each_of_its_constraints_passes_alone(impl, candidate, use, min_effort, prefix, min_score):
    parts = {"effort": {"min_effort": min_effort}, "source": {"allowed_sources": [prefix]},
             "bench": {"min_benchmark_index": {"index_name": "idx", "min_score": min_score}}}
    chosen = [k for k in parts if use[k]]
    combined = {k: v for name in chosen for k, v in parts[name].items()}
    alone = [impl.evaluate_capability_floor(candidate, parts[name])[0] for name in chosen]
    assert impl.evaluate_capability_floor(candidate, combined)[0] is all(alone)


# --------------------------------------------------------------- reward tie-break

_rewards = st.one_of(st.none(), st.floats(min_value=-1, max_value=1, allow_nan=False))


@IMPLEMENTATIONS
@given(rewards=st.lists(_rewards, max_size=12))
def test_rewards_rank_positive_then_unmeasured_then_neutral_then_negative(impl, rewards):
    """Unmeasured never ties a measured zero; better positives first; the least negative negative first."""
    ranked = sorted(rewards, key=impl.reward_sort_key)
    positive = sorted((r for r in rewards if r is not None and r > 0), reverse=True)
    unmeasured = [None] * rewards.count(None)
    neutral = [r for r in rewards if r is not None and r == 0]
    negative = sorted((r for r in rewards if r is not None and r < 0), reverse=True)
    assert ranked == positive + unmeasured + neutral + negative


# --------------------------------------------------------------- route identity

_majors = st.integers(min_value=0, max_value=99)
_part = st.integers(min_value=0, max_value=99)


@given(major=_majors, minor=_part, patch=_part, prefix=st.sampled_from(["", "v"]))
def test_a_harness_version_is_its_major_number(major, minor, patch, prefix):
    assert scoring.harness_major(f"{prefix}{major}.{minor}.{patch}") == str(major)
    assert scoring.harness_major(f"{prefix}{major}") == str(major)


@given(blank=st.one_of(st.none(), st.text(alphabet=" \t\n", max_size=4)))
def test_a_blank_harness_version_is_unknown_not_empty(blank):
    assert scoring.harness_major(blank) == "unknown"


@given(label=st.text(alphabet="abcdefghijklmnopqrstuvwxyz-_", min_size=1, max_size=10))
def test_a_non_numeric_harness_label_is_its_own_identity_so_it_never_inherits_a_major(label):
    assert scoring.harness_major(label) == label


@given(version=st.one_of(st.none(), st.text(max_size=12)))
def test_harness_major_is_stable_when_applied_again(version):
    once = scoring.harness_major(version)
    assert once and scoring.harness_major(once) == once


@given(harness=_text.filter(lambda t: "@" not in t), model=_text, effort=_efforts, major=_majors,
       a=_part, b=_part, c=_part)
def test_point_releases_of_one_major_are_one_route(harness, model, effort, major, a, b, c):
    first = scoring.normalize_triple(f"{harness}@{major}.{a}.{b}/{model}@{effort}")
    second = scoring.normalize_triple(f"{harness}@{major}.{c}/{model}@{effort}")
    assert first == second == f"{harness}@{major}/{model}@{effort}"
    assert scoring.normalize_triple(first) == first


@given(text=st.text(max_size=30).filter(lambda t: "@" not in t or "/" not in t))
def test_text_that_is_not_a_route_triple_is_left_alone(text):
    assert scoring.normalize_triple(text) == text


@given(harness=_text, model=_text, effort=_efforts, major=_majors, other_major=_majors, a=_part, b=_part)
def test_candidate_identity_keys_on_harness_major_not_the_full_version(harness, model, effort, major, other_major, a, b):
    def ident(version):
        return routing.candidate_id({"harness": harness, "harness_version": version, "model_id": model, "effort": effort})

    assert ident(f"{major}.{a}") == ident(f"{major}.{b}.9") == f"{harness}@{major}/{model}@{effort}"
    if other_major != major:
        assert ident(f"{major}.{a}") != ident(f"{other_major}.{a}")


# --------------------------------------------------------------- preferred seed

_CHOICES = dict(model_id=["m1", "m2", "m3"], effort=["low", "high"], harness=["claude", "codex"])
_candidate = st.fixed_dictionaries({k: st.sampled_from(v) for k, v in _CHOICES.items()})
_seed_entry = st.fixed_dictionaries(
    {"model_id": st.sampled_from(_CHOICES["model_id"])},
    optional={"effort": st.one_of(st.none(), st.sampled_from(_CHOICES["effort"])),
              "harness": st.one_of(st.none(), st.sampled_from(_CHOICES["harness"]))})


def _matches(entry, candidate):
    """An entry names a model and may narrow by effort and harness; an unset narrowing matches any."""
    return all(candidate[k] == v for k, v in entry.items() if v)


@given(candidate=_candidate, seed=st.one_of(st.none(), st.lists(_seed_entry, max_size=6)))
def test_preferred_rank_is_the_position_of_the_first_matching_entry(candidate, seed):
    expected = next((i for i, e in enumerate(seed or []) if _matches(e, candidate)), None)
    assert routing.preferred_rank(candidate, seed) == expected


@given(candidate=_candidate, seed=st.lists(_seed_entry, max_size=6), later=st.lists(_seed_entry, max_size=3),
       earlier=_seed_entry)
def test_only_entries_before_the_first_match_move_a_candidates_rank(candidate, seed, later, earlier):
    rank = routing.preferred_rank(candidate, seed)
    if rank is not None:
        assert routing.preferred_rank(candidate, seed + later) == rank
        if not _matches(earlier, candidate):
            assert routing.preferred_rank(candidate, [earlier] + seed) == rank + 1
