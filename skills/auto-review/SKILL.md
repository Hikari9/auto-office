---
name: auto-review
description: Internal Auto Office v3 independent review spoke. Use for plan review, code review, accepted-material/minor/rejected/pending finding disposition, pointed blast-radius and bypass analysis, repeated review rounds, or validating PLAN DEFECT and BRIEF DEFECT exits. Never reuse the producer session as its own independent reviewer.
---

# Auto Review

Use a fresh/different reviewer session from the producer whenever the harness permits it. Never self-approve.

Before invoking a plan reviewer or code reviewer, publish the router's `selection_disclosure` to the user and preserve it in the dispatch record/readback. It must name the exact invocation model identifier when available, canonical model ID, effort, harness/version, and the evidence-backed reason this reviewer route won.

Focus prompts on concrete bypass paths, blast radius, protected paths, wrong-but-passing implementations, state/version mismatch, authority violations, and missing acceptance evidence rather than generic “review correctness.”

Findings use exactly: `accepted-material`, `accepted-minor`, `rejected-on-evidence`, `pending`. Full gate credit belongs only to accepted-material findings.

Resume the same reviewer session across rounds when possible so it retains prior uncertainty/findings, but never resume into a producer identity.

A defect exit earns gate-like credit only when it names a contradicted assumption and proves the current artifact was tested enough to expose it.
