---
name: auto-review
description: Internal Auto Office v3 independent review spoke. Use for plan review, code review, accepted-material/minor/rejected/pending finding disposition, pointed blast-radius and bypass analysis, repeated review rounds, or validating PLAN DEFECT and BRIEF DEFECT exits.
---

# Auto Review

Every gate is held by an agent that did not produce the work — never self-approve. Use a fresh/different reviewer session from the producer whenever the harness permits it. When `HERDR_ENV=1` and `herdr` is reachable, that fresh session is a Herdr pane, not an in-process subagent tool (see the top-level `SKILL.md`'s Dispatch and mutation section) — check this before dispatching, not after defaulting to whatever tool is already loaded.

Before invoking a plan reviewer or code reviewer, publish the router's `selection_disclosure` to the user and preserve it in the dispatch record/readback. It must name the exact invocation model identifier when available, canonical model ID, effort, harness/version, and the evidence-backed reason this reviewer route won.

Focus prompts on concrete bypass paths, blast radius, protected paths, wrong-but-passing implementations, state/version mismatch, authority violations, and missing acceptance evidence rather than generic “review correctness.”

Findings use exactly: `accepted-material`, `accepted-minor`, `rejected-on-evidence`, `pending`. Full gate credit belongs only to accepted-material findings.

Review is local first (`review_mode: independent_adversary`) or inline for cheap/reversible/low-risk work (`labeled-inline`, never claimed as independent). A final `integration_adversary` is triggered only when dependent/merging landings from two or more executors must compose — never by executor count alone. `disposition_owner` stays with the executor (or planner for a plan-scoped finding) except a genuine, unresolved evidence conflict, which escalates through the orchestrator to the user rather than being decided by the reviewer.

Resume the same reviewer session across rounds when possible so it retains prior uncertainty/findings, but never resume into a producer identity.

A defect exit earns gate-like credit only when it names a contradicted assumption and proves the current artifact was tested enough to expose it.

## A review describes one tree state

A review is a statement about a specific tree state. If the producer is still editing that tree, the reviewer's findings, line numbers, and contamination check all describe something that no longer exists, and afterward nobody can tell which findings survived.

Give a concurrent repair its own worktree, or wait for the in-flight review to land; combining several partial repair rounds into one tends to cost less than paying for a full re-review per round.

Scope the contamination check to what changed between the review's start and end snapshot that falls outside the declared review scope — not to file authorship. A reviewer who never writes producer files will otherwise flag nearly every review as contaminated; the signal that actually matters is unscoped drift during the review window, regardless of who wrote it.

## Review round caps

`full`: 5 rounds. `express`: 2 rounds. Gear presets with `plan_review: false` (`direct`,
`direct+review`, `light`, `quick`) that fund plan review anyway on explicit user request inherit
`express`'s 2-round cap unless the user names a different one — a gear having no native review
budget is not license to run one unbounded.

A second CHANGES REQUIRED on the same task forces an orchestrator disposition, not an automatic
re-plan. "Forces a disposition" means stating one of the following, to the user, before any
further round is dispatched — not silently revising the plan again and resubmitting:

- **escalate** — hand the two rounds of findings to the user and let them decide whether to
  continue, change reviewers, or stop;
- **accept with named residual risk** — proceed past the cap with the specific unresolved finding
  named, not merely implied by a passing verdict;
- **widen the replan** — the findings indicate the plan's shape itself is wrong, not a fixable
  detail, so return to intake rather than patching forward;
- **stop** — the artifact cannot be made to pass this reviewer; end the review loop and report why.

Continuing straight to round 3 without one of these being stated is the failure this section
exists to name: an orchestrator can satisfy "revise the plan" and "resubmit for review" every
round and never notice it skipped the disposition step, because nothing about producing a
compliant-looking v3 and re-dispatching requires stopping to choose. Observed 2026-09-19 on gear
`light` (no native cap, plan review added by explicit user request): three completed rounds plus
a fourth in progress, each returning CHANGES REQUIRED with genuine, evidence-backed findings, none
followed by a stated disposition — the loop only stopped because the user interrupted it, and then
asked why a "one round" mandate (which does not exist in this doc) had been exceeded. The real gap
wasn't a missing round number; it was that this section described a required stop without giving
the orchestrator anything to actually say at that stop, so there was nothing to notice omitting.

PLAN DEFECT and BRIEF DEFECT exit without consuming a round.
