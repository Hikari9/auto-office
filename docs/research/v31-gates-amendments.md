# v3.1 research: portable gates and non-blocking amendment delivery

Research for [Portable gate execution and non-blocking amendment delivery](https://github.com/Hikari9/auto-office/issues/155). Governed by [the ratified charter](https://github.com/Hikari9/auto-office/issues/153#issuecomment-5832359821).

**Repository baseline:** `93167420271527f1220700f2eebdff1ac2d1115c`, observed main on September 25, 2026. Public documentation checked the same day. Repository links are pinned; public documentation is mutable and does not establish installed-version conformance.

**Method:** source/call-path inspection, review of relevant test declarations, current primary documentation, and one isolated Bash semantics probe. No repository test suite, paid model dispatch, live harness test, or local historical-data analysis was run. No authenticated subagent runner was available. Historical token/cost/timing research is skipped at the user's request.

## Conclusion

The reusable substrate exists, but the advertised workflow is not yet one complete runtime operation. The useful addition is **a small semantic API over durable jobs, scoped version transitions, and revision-bound gate evaluation**, not a new fleet of bookkeeping agents.

Three different facts must remain distinct: a message was delivered, an amendment was applied at a safe boundary, and a revision passed the required gates. None implies the next.

## Current call-path evidence

| Concern | Present behavior | Consequence for v3.1 |
|---|---|---|
| Plan rereview authorization | `plan_review_round_authorized()` accepts only PLAN DEFECT variants; CHANGES REQUIRED does not fund another round. [Runtime][runtime] | Explicitly superseded by the new charter: amend-and-launch must also request ordinary rereview concurrently. Update the old test expectation intentionally. |
| Family amendment | `cmd_amend` validates a delta file, calls `apply_amendment`, prints a result. The family helper persists versions, amendments and paused scopes. [Runtime][runtime], [family][family] | No transport send, worker inbox or applied acknowledgement occurs in this path. Its notification language is not delivery evidence. |
| Version ownership | Routing/requirements/plan own separate fields, with expected-prior checks. [Family][family] | Reuse; do not bump all three for an ordinary routing adjustment. |
| Split state writers | `increment-plan` updates run state; family amendment updates family/registry. Shell PLAN_DEFECT path calls the former. [Runtime][runtime], [review loop][review] | One semantic transition must reconcile the sources, approval invalidation and affected packets. Do not make the orchestrator repair the drift. |
| Packets | Constructor derives packet identity and validates; invalidation can intersect task IDs/scopes. [Packets][packets] | Useful primitives. Normal commands should derive boilerplate, not ask an agent to submit a second metadata document. |
| Checkpoint | `save_checkpoint` validates/writes a snapshot. [Family][family] | Durability checkpoint is not an accepted behavioral slice; acceptance requires a gate contract. |
| Review loop | Checks supplied review-file identity, optional scope and HEAD, then records it; fixes use external FIX_COMMAND. [Review loop][review] | Does not itself launch the next reviewer. Replace coordination with durable jobs rather than re-reading the same stale file. |
| Gate coverage | `verify.sh` leaves runtime/browser commands empty. Its JSON communicates failures; its final shell exit is not a complete pass signal. [Verifier][verify] | Required/applicable gates must be concrete. Never equate exit 0, missing command, or a skipped result with acceptance. |
| Landing | CLI requires a reachable recorder; family landing checks a passing validation hash, but not the complete required gate set/current versions/review freshness. [Runtime][runtime], [family][family] | Reuse recorder-backed validation, add one shared acceptance evaluator; avoid weaker bypass entrypoints. |
| Revision binding | Shell review compares `reviewed_head_sha` to Git HEAD using prefix matching. Recorder checks family versions but not current source bytes. [Review loop][review], [family][family] | Uncommitted edits can change reviewed code without changing HEAD. Bind to an immutable reviewed revision/tree and target context. |
| Strict re-verification | `cmd_verify_landing --strict` runs commands with `cwd=ROOT`, the plugin root. [Runtime][runtime] | Verification must use the declared target worktree/environment, not whichever repository hosts Office. |
| Completion | Monitor distinguishes process exit, corroborated pane samples and raw statuses; ordered acknowledgements exist. [Monitor][monitor] | Retain distinction. Process completion or stable pane output is not evidence of successful work. |
| Concurrency | Event sequence allocation/dedup is read-then-append; cursor advancement is unlocked read-modify-replace. [Monitor][monitor] | Existing sequential idempotency is not race safety. Concurrent completions need serialized allocation and conflict detection. |
| Background path | `cmd_watch` blocks until terminal unless `--once`; source states tests use a fake bridge. [Monitor][monitor] | Start a managed run-scoped process or use a proven adapter event mechanism; do not make the orchestrator spin in a foreground polling loop. |

## Spawn lifecycle needs consolidation too

The current [spawner][spawn] makes start-receipt recording conditional on seven metadata flags all being supplied. It launches before it records that receipt. TIMEOUT is parsed but not used by the script's process lifetime control; a model's own timeout option is a different mechanism. The startup check also treats an already-exited process as failure even when a short successful command may have completed.

A narrow local experiment reproduced the shell-language risk in the nonzero-exit sentinel path:

```bash
set -euo pipefail
( false > output.log 2>&1; echo $? > exit_code ) &
wait $!
```

Observed in an isolated scratch directory: `wait` exited 1; `exit_code` was not created. This is **not an end-to-end Auto Office test**. It demonstrates why a supervisor cannot assume the current `eval command; echo $?` sequence always records failures under errexit. A runner should persist terminal classification through an explicit finally/trap path and preserve interruption/signal information. No repository source was patched.

The monitor's comment that all four adapters use identical CLI-only execution is also stale relative to the Claude seed, which now declares `cli` and `in-session`, and launches a background Claude form. Follow adapter mechanics and actual child receipts, not that blanket comment. [Claude adapter][claude-adapter], [monitor][monitor]

## Portability matrix and conservative holding behavior

All installed-version tests below are **NOT RUN**. Versions are those reported in the existing [CLI architecture][cli], not binaries observed in this environment. The portable contract should remain narrower than any one harness's advertised event set.

| Harness | Existing repository baseline | Current documentation / repository evidence | Safe integration stance |
|---|---|---|---|
| Claude Code | 2.1.282 reported | Official docs support synchronous command hooks and async command hooks. Async results cannot retroactively block; delivery normally waits for a later turn and `-p` teardown can cancel unfinished hooks. [Hooks][claude-hooks] | Native events can notify; persistent Office job/result state must not depend on the agent remaining alive. Use explicit transition gates. |
| Codex | 0.156.1 reported | Fetched English hook reference says async is parsed but unsupported, requires trust for changed non-managed hook definitions, and warns specialized tool paths may skip hooks. [Hooks][codex-hooks] | Do not assume async support or universal interception. Keep repository's unverified-denial holding policy until a local doctor test proves the installed path. |
| Gemini CLI | 0.46.0 reported | Official hooks run synchronously within the agent loop. [Hooks][gemini-hooks] | Fast hook returns after notification/enqueue; expensive verification belongs outside the synchronous hook. |
| agy | 1.2.10 reported | Repository documents incomplete lifecycle coverage and prompt/status quirks; model invocation differs from native Gemini. [Adapter][agy-adapter], [CLI specification][cli] | Do not inherit Gemini CLI capabilities by model name. Explicit submission/boundary commands and durable replay are the fallback. |
| Hermes | v0.21.5 reported | Seed has stdin invocation, pending conformance and no quota-probe command. [Adapter][hermes-adapter] | No proven visual/event/effort path may be inferred from a seeded field. Use explicit runtime operations; conformance remains outstanding. |
| Herdr | 0.9.1 reported | Repository monitor polls pane status/content and corroborates samples; this is not a model harness with a durable task mailbox. [Monitor][monitor] | Retain user-visible panes and uncertain-state evidence. Process/receipt evidence and Office state govern acceptance. |

The latest public documents and the repository's earlier matrix can differ. Do not register new hooks or mark an adapter proven solely from this research. A checked capability for one harness/version does not promote another.

## Proposed runtime mechanics for the detailed-design ticket

These are implementation recommendations to resolve in [Rolling plan review and parallel checkpoint gate semantics](https://github.com/Hikari9/auto-office/issues/160), not already implemented interfaces.

### 1. Short command, durable operation

A semantic submit resolves its bound run/task, validates authority and current plan scope, obtains a stable source snapshot, records a deduplicated operation and required gate plan, and returns a small receipt/next action. Ordinary tests, capture and reviewer jobs then operate from that fixed input. The orchestrator does not author the job envelopes or relay routine findings.

One-submit does not mean one huge blocking process. A run-scoped worker may execute authorized jobs and exit when idle or complete. This is compatible with no mandatory always-running daemon. On platforms where detachment cannot survive the host session, report that limitation and reconstruct from the durable operation on resume; do not promise background completion without a live executor.

### 2. Canonical state plus replayable consequences

Use a transactionally consistent authoritative store, or a journal and deterministic projection protocol. An accepted amendment and its outbound delivery intent must not be separable by a crash. Record the operation before sending, retry delivery safely, and reconcile response loss without reapplying a version bump.

The exact storage implementation is still a decision; a transactional outbox is a pattern, not a mandate for a new broker/service. Existing SQLite and filesystem primitives may be sufficient. File locks alone do not make a sequence of several file replacements crash-atomic.

### 3. Delivery is not application

A scoped delta needs an identity, prior/new applicable version, affected scope/dependants, and the exact changed contract. Runtime-generated metadata should stay out of normal prompts. Track at least queued/delivered/applied or rejected states internally. A transport acknowledgement only says the message arrived. Worker application acknowledgement names the safe boundary and adopted version; only then may subsequent output satisfy that amended contract.

Out-of-order, duplicate and superseded deltas require defined handling. If a harness cannot accept messages while executing, use a verified boundary/resume mechanism rather than silently injecting keystrokes or spawning another writer. Never treat restarting a process as proof it resumed the same role authority.

### 4. Early execution without abandoning the defect barrier

The initial review result and budget govern the fork: APPROVED launches without extra review; ordinary CHANGES_REQUIRED is amended and launches execution while amended-plan review runs concurrently. Unresolved initial PLAN_DEFECT launches nothing. Later defects pause affected work and dependency closure; normal amendments wait for atomic boundaries.

Ordinary authorized plan refinement must not erase user approval merely because a generic legacy helper increments plan_version. Conversely, changes to authority, irreversible actions or a genuinely invalid contract cannot hide inside a harmless-refinement label. Detailed defect-clearance evidence and the distinction between user authorization and plan-review status must be explicit. This is already within the charter's direction, but its concrete state representation remains open.

### 5. Gate freshness and parallelism

Key a gate attempt to task/checkpoint, reviewed source snapshot, acceptance/reference versions and relevant environment/configuration. A passing older result remains audit evidence but cannot pass a changed input. Begin code and UI review concurrently only when both prerequisites are ready for that fixed revision. Relevant changes invalidate affected gate receipts; conservative reruns are preferable when dependency impact is unknown.

The orchestrator retains strategic stacking/parallelism decisions. The runtime enforces prerequisites, leases and non-overlapping mutation, not a new inflexible global wave policy. Integrated output must receive applicable integration checks.

### 6. Role authority and result delivery

A read-only reviewer should return findings through stdout/reply or a dedicated write-only result channel owned by the runner; it must not need permission to rewrite the application or Office's state. The repository already tests this delivery incompatibility in [test_packet_delivery_channel.py][delivery-test]. An output JSON envelope is runtime-owned; a reviewer needs to supply the actual verdict/findings, not timestamps/hashes/IDs it could invent.

This is not a cryptographic security boundary against an agent with unrestricted write access to the same state directory. Reviewer/worker isolation and protected runtime-owned state are required if malicious or compromised-worker resistance is claimed. Do not equate schema-valid self-written receipts with independent evidence.

## Required test seams for implementation

| Scenario | Observable assertion |
|---|---|
| First APPROVED | Executor launches; no second plan-review dispatch is generated. |
| First ordinary CHANGES_REQUIRED | Amendment persists; executor and second plan reviewer both launch before that review returns. |
| Initial plan defect | Zero executor launches until attributed, evidence-backed clearance. |
| Later defect | Affected dependency closure pauses; unrelated ready work remains runnable. |
| Duplicate submit / lost CLI response | Same operation reused; no duplicate reviewer or version bump. |
| Crash between amendment persistence and send | Pending delivery resumes; version applies once. |
| Delivered but not applied delta | New-version acceptance blocked until application acknowledgement. |
| Out-of-order deltas | Stale delta rejected or explicitly superseded, not applied after a newer contract. |
| Concurrent event/cursor writers | No duplicate sequence allocation or lost acknowledgements. |
| Dirty edit with unchanged HEAD | Prior review cannot pass the changed snapshot. |
| Invalid capture or environment failure | Correct category and recipient; no fabricated implementation defect/success. |
| Foreground command returns while job runs | Measured bounded return with durable pending job; no model polling loop needed. |
| Process exits nonzero, quickly or by signal | Terminal classification always recorded without mislabeling quick success. |
| No native hook support | Explicit commands still enforce acceptance and reconstruct on resume. |
| Budget exhaustion | One bounded escalation, then paused resumable work; never silent pass. |

Existing tests to extend rather than replace indiscriminately: `test_runtime.py`, `test_amendments.py`, `test_families.py`, `test_monitor.py`, `test_review.py`, `test_landings.py`, `test_packet_delivery_channel.py`, and `test_worktree.py`. No newly passing test results are asserted.

## Research completion boundary

The code/documentation question is resolved enough for the detailed semantics decision and CLI prototype. Local harness conformance, quantitative latency, actual cost savings and historical behavior remain unmeasured; historical data was deliberately skipped. No paid probes, hook installation, runtime configuration changes, main-branch edits, or production actions were performed.

[runtime]: https://github.com/Hikari9/auto-office/blob/93167420271527f1220700f2eebdff1ac2d1115c/scripts/office_runtime.py
[family]: https://github.com/Hikari9/auto-office/blob/93167420271527f1220700f2eebdff1ac2d1115c/scripts/office_family.py
[packets]: https://github.com/Hikari9/auto-office/blob/93167420271527f1220700f2eebdff1ac2d1115c/scripts/office_packets.py
[review]: https://github.com/Hikari9/auto-office/blob/93167420271527f1220700f2eebdff1ac2d1115c/scripts/review_loop.sh
[verify]: https://github.com/Hikari9/auto-office/blob/93167420271527f1220700f2eebdff1ac2d1115c/scripts/verify.sh
[monitor]: https://github.com/Hikari9/auto-office/blob/93167420271527f1220700f2eebdff1ac2d1115c/scripts/office_monitor.py
[spawn]: https://github.com/Hikari9/auto-office/blob/93167420271527f1220700f2eebdff1ac2d1115c/scripts/office_spawn.sh
[delivery-test]: https://github.com/Hikari9/auto-office/blob/93167420271527f1220700f2eebdff1ac2d1115c/tests/test_packet_delivery_channel.py
[cli]: https://github.com/Hikari9/auto-office/blob/93167420271527f1220700f2eebdff1ac2d1115c/docs/office-cli-architecture.md
[claude-adapter]: https://github.com/Hikari9/auto-office/blob/93167420271527f1220700f2eebdff1ac2d1115c/adapters/seed/claude.yaml
[agy-adapter]: https://github.com/Hikari9/auto-office/blob/93167420271527f1220700f2eebdff1ac2d1115c/adapters/seed/agy.yaml
[hermes-adapter]: https://github.com/Hikari9/auto-office/blob/93167420271527f1220700f2eebdff1ac2d1115c/adapters/seed/hermes.yaml
[claude-hooks]: https://code.claude.com/docs/en/hooks#run-hooks-in-the-background
[codex-hooks]: https://learn.chatgpt.com/docs/hooks
[gemini-hooks]: https://geminicli.com/docs/hooks/
