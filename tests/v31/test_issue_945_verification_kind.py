"""#945: closeout labels each acceptance criterion with its verification kind, lists the faked and
unverified ones together, and fails closed on an unlabeled report."""
import re

import pytest

from office import closeout

LIVE = "verified-live"
FAKES = "verified-against-fakes"
NOT = "not-verified"
NO_CRITERIA = re.escape("acceptance cannot be reported as met without labeled criteria")
NO_LABEL = re.escape("verified-live, verified-against-fakes, not-verified); got ")


def test_each_criterion_is_labeled_with_its_verification_kind():
    lines = closeout.criteria_report([("CLI exits 0", LIVE), ("provider returns 200", FAKES), ("deploy hook fires", NOT)])
    assert lines == ["verified-live: CLI exits 0", "verified-against-fakes: provider returns 200",
                     "not-verified: deploy hook fires",
                     "not verified live: provider returns 200 (verified-against-fakes), deploy hook fires (not-verified)"]


def test_faked_and_unverified_criteria_are_listed_together_in_the_summary():
    lines = closeout.criteria_report([("a", LIVE), ("b", FAKES), ("c", NOT), ("d", FAKES)])
    assert lines[-1] == "not verified live: b (verified-against-fakes), c (not-verified), d (verified-against-fakes)"


def test_all_live_report_says_so_in_the_summary():
    assert closeout.criteria_report([("a", LIVE), ("b", LIVE)]) == [
        "verified-live: a", "verified-live: b", "all 2 acceptance criteria verified live"]


def test_criterion_text_is_cleaned_in_the_summary_too():
    lines = closeout.criteria_report([("\x1b[31mship\x1b[0m\tit؜", FAKES)])
    assert lines == ["verified-against-fakes: ship it", "not verified live: ship it (verified-against-fakes)"]


@pytest.mark.parametrize("kind", [None, "", "verified", "Verified-Live", "met", ["verified-live"]])
def test_criterion_without_a_known_kind_is_rejected(kind):
    with pytest.raises(ValueError, match=NO_LABEL + re.escape(f"('deploy hook', {kind!r})")):
        closeout.criteria_report([("a", LIVE), ("deploy hook", kind)])


@pytest.mark.parametrize("item", [None, "a", ("a",), ("a", LIVE, "extra")])
def test_malformed_criterion_entry_is_rejected_as_a_value_error(item):
    with pytest.raises(ValueError, match=NO_LABEL):
        closeout.criteria_report([item])


def test_empty_report_is_rejected():
    with pytest.raises(ValueError, match=NO_CRITERIA):
        closeout.criteria_report([])


def test_empty_generator_is_rejected_like_an_empty_list():
    with pytest.raises(ValueError, match=NO_CRITERIA):
        closeout.criteria_report((item for item in []))


def test_generator_of_criteria_is_fully_reported():
    lines = closeout.criteria_report((text, LIVE) for text in ["a", "b"])
    assert lines == ["verified-live: a", "verified-live: b", "all 2 acceptance criteria verified live"]


@pytest.mark.parametrize("text", ["", "   ", None, "\x1b[0m", "\x07‮"])
def test_criterion_without_printable_text_is_rejected(text):
    with pytest.raises(ValueError, match=NO_LABEL):
        closeout.criteria_report([(text, LIVE)])


def test_criterion_text_is_cleaned_before_it_is_printed():
    lines = closeout.criteria_report([("\x1b[31mred\x1b[0m\tcheck‮", LIVE)])
    assert lines == ["verified-live: red check", "all 1 acceptance criteria verified live"]
