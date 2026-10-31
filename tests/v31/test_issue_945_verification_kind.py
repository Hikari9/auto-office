"""#945: closeout labels each acceptance criterion with its verification kind, lists the faked and
unverified ones together, and fails closed on an unlabeled report."""
import pytest

from office import closeout

LIVE = "verified-live"
FAKES = "verified-against-fakes"
NOT = "not-verified"


def test_each_criterion_is_labeled_with_its_verification_kind():
    lines = closeout.criteria_report([("CLI exits 0", LIVE), ("provider returns 200", FAKES), ("deploy hook fires", NOT)])
    assert lines[:3] == ["verified-live: CLI exits 0", "verified-against-fakes: provider returns 200",
                         "not-verified: deploy hook fires"]


def test_faked_and_unverified_criteria_are_listed_together_in_the_summary():
    lines = closeout.criteria_report([("a", LIVE), ("b", FAKES), ("c", NOT), ("d", FAKES)])
    assert lines[-1] == "not verified live: b (verified-against-fakes), c (not-verified), d (verified-against-fakes)"


def test_all_live_report_says_so_in_the_summary():
    lines = closeout.criteria_report([("a", LIVE), ("b", LIVE)])
    assert lines[-1] == "all 2 acceptance criteria verified live"


@pytest.mark.parametrize("kind", [None, "", "verified", "Verified-Live", "met"])
def test_criterion_without_a_known_kind_is_rejected(kind):
    with pytest.raises(ValueError, match="deploy hook"):
        closeout.criteria_report([("a", LIVE), ("deploy hook", kind)])


def test_report_with_no_criteria_is_rejected():
    with pytest.raises(ValueError):
        closeout.criteria_report([])


@pytest.mark.parametrize("text", ["", "   ", None])
def test_criterion_without_text_is_rejected(text):
    with pytest.raises(ValueError):
        closeout.criteria_report([(text, LIVE)])


def test_criterion_text_is_cleaned_before_it_is_printed():
    lines = closeout.criteria_report([("\x1b[31mred\x1b[0m\tcheck‮", LIVE)])
    assert lines[0] == "verified-live: red check"
