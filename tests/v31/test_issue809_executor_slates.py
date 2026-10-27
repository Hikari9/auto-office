"""#809 final executor slate, versioned task floors and missing catalog qualifications."""
import pytest
from office import executor_slates as slates
from office import routing


@pytest.mark.parametrize("size,floor", list(slates.FLOORS.items()))
def test_pinned_benchmark_per_real_subtask_size(size, floor):
    candidate = {"benchmark_indexes": {slates.INDEX: floor}}
    assert slates.benchmark_gate(candidate, size) is None
    candidate["benchmark_indexes"][slates.INDEX] = floor - 1
    assert slates.benchmark_gate(candidate, size) is not None
    candidate["benchmark_indexes"] = {"Artificial Analysis Intelligence Index v4.3.1": floor + 50}
    assert "missing" in slates.benchmark_gate(candidate, size)
    candidate["benchmark_indexes"] = {slates.INDEX: None}
    assert slates.benchmark_gate(candidate, size)


@pytest.mark.parametrize("effort,score,largest", [
    ("none", 33, "XS"), ("low", 36, "XS"), ("medium", 41, "M"),
    ("high", 44, "L"), ("xhigh", 45, "L"), ("max", 48, "XL"),
])
def test_terra_effort_thresholds_do_not_claim_qualification(effort, score, largest):
    c = {"model_id": "gpt-6.1-terra", "effort": effort,
         "benchmark_indexes": {slates.INDEX: score}, "route_status": "untried"}
    for size, floor in slates.FLOORS.items():
        assert (slates.benchmark_gate(c, size) is None) == (score >= floor)
    assert c["route_status"] != "available"
    assert largest in slates.FLOORS


def test_sonnet_self_fallback_order_and_same_effort():
    for size, eff¶»§q«^