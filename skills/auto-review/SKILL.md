---
name: auto-review
description: Internal Auto Office v3 independent review spoke. Use for plan review, code review, accepted-material/minor/rejected/pending finding disposition, pointed blast-radius and bypass analysis, repeated review rounds, or validating PLAN DEFECT and BRIEF DEFECT exits. Never reuse the producer session as its own independent reviewer.
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

A review is a statement about a specific tree state. If the producer is still editing that tree, the reviewer's findings, line numbers, and contamination check all describe something that no longer exists, and afterward nobody can tell which findings survived. See references/review-tree-state.md for the observed collision pattern.

Give a concurrent repair its own worktree, or wait for the in-flight review to land; combining several partial repair rounds into one tends to cost less than paying for a full re-review per round.

Scope the contamination check to what changed between the review's start and end snapshot that falls outside the declared review scope — not to file authorship. A reviewer who never writes producer files will otherwise flag nearly every review as contaminated; the signal that actually matters is unscoped drift during the review window, regardless of who wrote it.

## Review round caps

`full`: 5 rounds. `express`: 2 rounds. A second CHANGES REQUIRED on the same task forces an orchestrator disposition, not an automatic re-plan. PLAN DEFECT and BRIEF DEFECT exit without consuming a round.
