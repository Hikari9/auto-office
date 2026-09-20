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

Read caps from `config.default.yaml`'s `gear_presets.<gear>.plan_review_max_rounds` /
`code_review_max_rounds` (`full`: 5/5, `express`: 2/2) via `office_runtime.py resolve-gates
--state-dir <dir>` or `start`'s own `gates` output — never hardcode them here.
`direct+review`/`light`/`quick` carry a literal `plan_review: false`: no plan-review budget
of their own, by design. `direct` carries `plan_review: risk_forced`, resolving to a funded
review (drawing `ad_hoc_review_max_rounds`, 2 by default) whenever `start` was given
`--blast-radius production[-data]`, `--size-class L|XL`, or `--irreversible` — even with the
named gear pinned to `direct`. Unset risk inputs are never read as low risk.

The cap counts PLAN-DEFECT-triggered re-reviews only — a normal review with no defect stays
at one round regardless of the configured ceiling. `ACCEPTED` ends review immediately;
`CHANGES REQUIRED` is the producer's to fix without sending the plan back to the reviewer;
neither authorizes another round. Only an accepted `PLAN DEFECT` does — it contradicts an
assumption the plan was built on, so the fix needs a fresh independent look, not "a round
left in the budget." `BRIEF DEFECT` follows the same rule for the task brief. Check
`office_runtime.py plan-review-round-authorized --verdict <verdict>` rather than reasoning
about the verdict yourself.

**A second CHANGES REQUIRED on the same task is a hard stop, not a checkpoint to notice and keep
going past.** The moment a second consecutive CHANGES REQUIRED lands on the same artifact: stop.
Do not draft the next plan/code version. Do not requeue the reviewer. Before anything else, write
down — to the user, or in the run record if the user is not present to read it yet — one explicit
disposition:

- **escalate** — put the unresolved tension in front of the user and wait;
- **accept with named gaps** — proceed, but state exactly which findings are being carried forward
  unresolved and why that's acceptable now; or
- **re-scope** — the findings show the task/brief itself was wrong, not just the artifact, so the
  fix is upstream of another revise-and-resubmit cycle.

Producing v3 and immediately sending it back to the same reviewer is not a disposition — it is the
exact behavior this stop exists to interrupt. If the fix is obviously correct and small, "accept
with named gaps: none, proceeding" is a legitimate one-line disposition — the requirement is that
you stop and say which one, not that every second CHANGES REQUIRED must escalate.

This was skipped once in practice: a plan under `light` gear (no default plan-review budget, the
user added an ad-hoc reviewer) took a second CHANGES REQUIRED at round 2 and rolled straight into
rounds 3 and 4 with no disposition recorded, on a gear with no stated cap to catch it either. Both
gaps are closed above — the cap now defaults instead of being silently absent, and the stop is
stated as an action to take, not a fact to know.
