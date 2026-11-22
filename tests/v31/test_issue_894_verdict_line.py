"""#894: a FINDING that quotes `verdict == "..."` is not the verdict line. `_last_block` once started
there, cut off the real `VERDICT:` line, and every rewrite of a valid reply read "no VERDICT line"."""
from __future__ import annotations

import pytest

from office import contract, gates, review_parse

REPLY = """VERDICT: RECHECK
REVIEWED_COMMIT a2e091f236bc7f54093343626dd46b1edd1c1de8
FINDING F1 | high | blocking | tests/x.py:12 | the check passes vacuously | assert the status
FINDING F5 | low | non-blocking | tests/x.py:261 | `verdict == "UNAVAILABLE" or status == "UNAVAILABLE"` is a disjunction | assert one
NEXT fix F1 and F5
"""


def _parse(text):
    return review_parse.parse(gates._last_block(text), plan_review=False, visual=False, contract=contract.CONVERGENCE)


def test_a_finding_quoting_verdict_equals_keeps_the_real_verdict_line():
    parsed = _parse(REPLY)
    assert parsed.valid and parsed.verdict == "RECHECK", parsed.errors


@pytest.mark.parametrize("text", [
    "I read the diff first.\nVERDICT: APPROVED\nREVIEWED_COMMIT a2e091f236bc\nNEXT proceed\n",
    "notes\n**VERDICT: APPROVED**\nREVIEWED_COMMIT a2e091f236bc\nNEXT proceed\n",
    "- `VERDICT: APPROVED`\nREVIEWED_COMMIT a2e091f236bc\nNEXT proceed\n",
])
def test_narration_before_a_marked_up_verdict_line_is_still_dropped(text):
    assert gates._last_block(text).lstrip("-` *").startswith("VERDICT")
    assert _parse(text).verdict == "APPROVED"
