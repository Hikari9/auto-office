from office import gates


def test_cap_diff_marks_truncation():
    diff = "x" * (gates.MAX_DIFF_CHARS + 10)
    capped = gates.cap_diff(diff)
    assert capped.startswith("x" * gates.MAX_DIFF_CHARS)
    assert capped.endswith("[diff truncated; inspect the checkout for the rest]")


def test_cap_diff_leaves_short_diff_unchanged():
    assert gates.cap_diff("small") == "small"
