---
name: auto-verification
description: Internal Auto Office v3 verification spoke. Use to build or execute the verification floor for mutable work, choose targeted/regression/static/build/runtime checks, prove known-bad inputs fail critical gates, run browser acceptance flows for user-facing work, and package validation evidence for independent review and closeout.
---

# Auto Verification

Every mutable run self-verifies. Add independent verification when risk, gear, playbook, repository policy, or acceptance path requires it.

Prefer existing targeted tests, regression tests, static/type/lint, build/package, focused runtime, then broader suites when justified.

Do not treat exit code zero or a green gate alone as proof. Where practical, demonstrate critical gates fail on known-bad/mutated input before trusting green.

For user-facing work, when a reachable local/preview runtime can reasonably be produced, execute the real acceptance flow in a browser. Opening the homepage is not browser verification.

Record exact commands/flows and evidence hashes in durable state.

## A green suite that injects a double at every seam proves the double

When every test substitutes a test double for the same dependency, no test exercises the real construction path. The suite reports the doubles are consistent with each other, which is not the claim anyone wanted.

The failure shape: a shared factory gains a required argument; every call site in the suite passes a double instead, so the suite stays green while every real invocation raises immediately on first contact with the live system. Test count and pass rate are unchanged, so nothing in the report signals the gap.

Cover the wiring separately from the behavior. Assert what arguments reach the real factory, without opening a connection. When a suite is green but the code has never run against the real dependency, say so in the evidence rather than reporting the pass rate alone.

Rebasing onto current mainline is part of verification, not a formality before it. Re-run the suite after the rebase: a clean merge says the text reconciled, never that the semantics did. A required-argument change in a merged dependency is invisible to the merge and fatal at runtime.

## An artifact a worker could not have captured is fabricated

When a worker is forbidden from calling the live system, it cannot produce any artifact that is defined as a capture of that system: snapshots, rollback targets, backups, recorded fixtures, golden files. Asked for one, it will hand-write something structurally valid and plausible.

The artifact then sits in a directory whose name asserts provenance it does not have. That is worse than the file being absent: absent fails loudly at the moment of need, while a plausible fake is restored during an incident and overwrites real state with a fiction.

Check provenance by scale before shape. A hand-written stand-in reproduces the schema faithfully and the cardinality not at all — a couple of representative entries where the real thing holds hundreds, and placeholder names. Compare the artifact's record count against the live system's before trusting it.

Capture provenance belongs to whoever may call the live system. If the brief forbids the calls, it must also forbid the artifact, and the capture becomes an orchestrator step with its own evidence.

Two gate-placement errors travel with this, both worth checking directly:

- **A capture path that validates before it captures can never capture.** The states worth capturing are the invalid ones: before first provisioning, and after a corruption. Validate on restore, never on export. Refusing to record garbage is refusing to record exactly when the record matters.
- **Confirmation flags gate writes, not reads.** A read-only export behind a write-confirmation flag means the rollback target can only be produced by someone who has already confirmed the write it protects against.

## Measure the bytes that get written

A size or format gate must operate on the exact serialization that reaches the destination. A validator measuring a compact encoding while the writer emits a spaced one passes documents that overflow the destination column, and fails late — after partial writes — instead of before the first one.

Pick one canonical serialization and use it in both the gate and the writer. Where a gate and a writer each serialize independently, treat that as a defect even when current inputs happen to fit.
