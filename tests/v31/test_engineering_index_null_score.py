"""A missing Engineering Index score stays null and is not a floor failure on its own (AA v5 A8)."""

import pytest

from office import scoring

EI = "Artificial Analysis Engineering Index (2026-11)"
II = "Artificial Analysis Intelligence Index v4.3.2"
FLOOR = {"min_effort": "none", "min_benchmark_index": {"index_name": EI, "min_score": 31}}


def test_a_route_with_no_engineering_index_score_passes_the_floor_and_stays_null():
    candidate = {"effort": "low", "benchmark_indexes": {II: 46}}
    assert scoring.evaluate_capability_floor(candidate, FLOOR) == (True, None)
    assert EI not in candidate["benchmark_indexes"]  # nothing is invented or copied in


def test_a_route_with_no_benchmark_indexes_at_all_passes_the_engineering_floor():
    assert scoring.evaluate_capability_floor({"effort": "low"}, FLOOR) == (True, None)


def test_a_sibling_effort_score_is_not_substituted():
    # the sibling's 99 would pass; the route's own score is absent, so it passes on null, not on 99
    candidate = {"effort": "low", "benchmark_indexes": {}}
    sibling = {"effort": "high", "benchmark_indexes": {EI: 99}}
    assert scoring.evaluate_capability_floor(candidate, FLOOR) == (True, None)
    assert candidate["benchmark_indexes"] == {} and sibling["benchmark_indexes"] == {EI: 99}


def test_a_route_below_the_engineering_floor_still_fails():
    passed, reason = scoring.evaluate_capability_floor({"effort": "low", "benchmark_indexes": {EI: 30}}, FLOOR)
    assert not passed and "below role floor" in reason


def test_a_route_at_the_engineering_floor_passes():
    assert scoring.evaluate_capability_floor({"effort": "low", "benchmark_indexes": {EI: 31}}, FLOOR) == (True, None)


@pytest.mark.parametrize("candidate", [{"effort": "low"}, {"effort": "low", "benchmark_indexes": {EI: 99}}])
def test_a_missing_intelligence_index_score_still_fails_closed(candidate):
    floor = {"min_effort": "none", "min_benchmark_index": {"index_name": II, "min_score": 31}}
    passed, reason = scoring.evaluate_capability_floor(candidate, floor)
    assert not passed and f"benchmark_indexes.{II}" in reason
