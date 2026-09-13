---
name: auto-review
description: Internal Auto Office v3 independent review spoke. Use for plan review, code review, accepted-material/minor/rejected/pending finding disposition, pointed blast-radius and bypass analysis, repeated review rounds, or validating PLAN DEFECT and BRIEF DEFECT exits. Never reuse the producer session as its own independent reviewer.
---

# Auto Review

Use a fresh/different reviewer session from the producer whenever the harness permits it. Never self-approve.

Focus prompts on concrete bypass paths, blast radius, protected paths, wrong-but-passing implementations, state/version mismatch, authority violations, and missing acceptance evidence rather than generic “review correctness.”

Findings use exactly: `accepted-material`, `accepted-minor`, `rejected-on-evidence`, `pending`. Full gate credit belongs only to accepted-material findings.

Resume the same reviewer session across rounds when possible so it retains prior uncertainty/findings, but never resume into a producer identity.

A defect exit earns gate-like credit only when it names a contradicted assumption and proves the current artifact was tested enough to expose it.

## Never review a tree another agent is editing

A review is a statement about a specific tree state. If the producer is still editing that tree, the reviewer's findings, line numbers, and contamination check all describe something that no longer exists, and you cannot tell afterwards which findings survived.

Serialize producer and reviewer on any shared working tree. If a repair round is needed while a review is in flight, either wait for the review to land, or give the repair its own worktree and review that separately. Dispatching both at once and getting usable findings anyway is luck, not method.

The tell is a contamination line that lists modified files the reviewer did not write. Treat that as "this review has unknown scope", not as a clean pass with a footnote.

Prefer one combined repair round over several partial ones. Each round costs a full re-review, and findings from different rounds are not additive when they overlap the same code.
