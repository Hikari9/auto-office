"""#1130: the orchestrator signal's `next` names the preflight refusal that stopped the worker, and the
`fixed <test> mutation=failed` advice appears only for the finding line it fits."""
from __future__ import annotations

import json

import pytest

from office import briefs, preflight
from test_preflight import _dispatched, _signals
from test_self_review_ledger import ledger_text, write_ledger

CAP = "self-review round cap spent with repairs outstanding: "
MUTATION = "fixed <test> mutation=failed"
LEDGER = briefs.LEDGER_FILE

# (case, reason as preflight emits it, text the next step must carry, text it must not carry)
CASES = [
    ("no ledger yet", f"{CAP}ledger: no {LEDGER}; run the SELF-REVIEW and write it in this worktree root in the "
     "format the brief gives", f"write {LEDGER}", MUTATION),
    ("lens skipped while the diff needs it", f"{CAP}ledger: lens platform is skipped, but the deep tier reviews all "
     "four lenses: review it and write `LENS platform reviewed`", "LENS <name> reviewed", MUTATION),
    ("stale ledger COMMIT", f"{CAP}ledger: names commit aaaaaaaaaaaa but HEAD is bbbbbbbbbbbb; the review is stale: "
     "re-review what changed, then update COMMIT", "COMMIT is stale", MUTATION),
    ("proof environment error", "self-review: revert-and-fail proof: t.py with the source reverted exited 127, an "
     "environment error that is not a test failure", "environment error", MUTATION),
    ("proof failure", "self-review: revert-and-fail proof: t.py fails on the submitted commit (exit 1)",
     "proof failed", MUTATION),
    ("round cap, repair outstanding: uncommitted work", f"{CAP}ledger: uncommitted in-scope changes (calc.py): commit "
     "them first, then rerun preflight", "commit the in-scope changes first (uncommitted in-scope changes (calc.py)",
     "repair the ledger"),
    ("round cap, other ledger repair", f"{CAP}ledger: COMMIT line is missing", "repair the ledger: COMMIT line is missing",
     MUTATION),
    ("round cap, repair outstanding", f"{CAP}ledger line 5: low calc.py:9 nit is open: fix it and mark it "
     f"`fixed <test path> mutation=failed`, or mark it `dismissed <reason>` or `out-of-scope`", MUTATION, "LENS"),
]


@pytest.mark.parametrize("case,reason,has,lacks", CASES, ids=[c[0] for c in CASES])
def test_next_names_the_refusal_not_the_generic_mutation_hint(case, reason, has, lacks):
    nxt = preflight._signal_next("T5", "D1", reason)
    assert has in nxt and lacks not in nxt, nxt
    assert nxt.startswith("office prompt D1 --") and "office preflight" in nxt and "office submit" in nxt, nxt


@pytest.mark.review_contract("v3.1")
@pytest.mark.parametrize("kw,has", [({"drop": ("edge-cases",)}, "LENS <name> reviewed"), ({"head": "0" * 40},
                                                                                                "COMMIT is stale")])
def test_a_spent_cap_signal_carries_the_repair_for_the_ledger_line_that_failed(env, kw, has):
    wenv, wt, d = _dispatched(env)
    head = env.git("rev-parse", "HEAD", cwd=wt).strip()
    write_ledger(wt, ledger_text(kw.get("head", head), rnd=3, drop=kw.get("drop", ())))
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 1, out
    nexts = [json.loads(e["payload_json"])["next"] for e in _signals(env)]
    assert nexts and all(has in n and MUTATION not in n for n in nexts), nexts


def test_an_open_finding_at_the_cap_asks_the_orchestrator_to_decide_and_names_the_finding():
    """The refusal tells the worker to report it, so the hint is the orchestrator's decision, not a worker repair."""
    reason = ("self-review: round 3 ended with a finding still open (line 5: high calc.py:2 x); "
              "the round cap is spent: report it")
    nxt = preflight._signal_next("T5", "D1", reason)
    assert nxt.startswith('office prompt D1 -- "<your decision on line 5: high calc.py:2 x:'), nxt
    assert MUTATION in nxt and "dismissed <reason>" in nxt and "office revoke T5" in nxt, nxt


def test_a_reason_with_double_quotes_cannot_break_the_prompt_argument():
    nxt = preflight._signal_next("T5", "D1", f'{CAP}ledger: COMMIT "x" is bad')
    assert nxt.count('"') == 2, nxt
