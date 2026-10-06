# v3.1 rolling plan review and checkpoint gate semantics

> Superseded for new runs: the review policy here is replaced by [`review-convergence.md`](review-convergence.md) (#337). It still governs runs pinned to the v3.1 review contract.

Status: **decided design, implemented in Auto Office 3.1.0** (`src/office/gates.py`, `src/office/amend.py`, `src/office/integration.py`; see [`v31-implementation.md`](v31-implementation.md)). Resolves [Decide rolling plan review and parallel checkpoint gate semantics](https://github.com/Hikari9/auto-office/issues/160) on the [v3.1 map](https://github.com/Hikari9/auto-office/issues/152). Product policy comes from the [ratified charter](https://github.com/Hikari9/auto-office/issues/153#issuecomment-5832359821); mechanism evidence from [the gates research](https://github.com/Hikari9/auto-office/issues/155#issuecomment-5832808796). Code pointers are against `main` at `3bce9b1`.

Terms are defined in [`CONTEXT.md`](../CONTEXT.md). The storage choice is recorded in [ADR 0001](adr/0001-sqlite-single-state-authority.md).

## 1. Decisions

Codes match the grilling session (maintainer answered "all recommended" to every round).

| Code | Decision |
|---|---|
| Q1 | **Plan defect** is a closed list: (a) contradicts frozen requirements/acceptance, (b) false interface/contract assumption executors would build on, (c) unsafe, irreversible, or authority-exceeding action, (d) two writers for one scope. Reviewer must cite evidence (requirement, `file:line`, or reproducible fact). Everything else is ordinary. The reviewer assigns the label; the orchestrator cannot downgrade it and may contest it once, which spends the escalation. |
| Q2 | **Defect clearance** requires an independent plan reviewer (original route or substitute) to confirm by `finding_id` that a revision containing the fix resolves it. The orchestrator never self-clears. The user is reached only after the escalation fails. |
| Q3 | Planner owns the initial plan and every **contract amendment** (cross-scope interfaces, ownership boundaries, authority, irreversible actions). Orchestrator owns **ordinary amendments** (decomposition, ordering, routing, acceptance detail inside frozen requirements, added tests). A **requirements change** originates only from the user; the orchestrator records it. |
| Q4 | Durability save is renamed **snapshot**; **checkpoint** means an acceptance slice with a gate contract; **acceptance** means all required gates passed on one revision. |
| Q5 | **Revision** = runtime-made commit of the submitted worktree (dirty edits included) + applied requirements/plan/acceptance versions + relevant environment/config fingerprint + lease ID (Q17). |
| Q6 | Code and UI reviews dispatch together on one revision after deterministic checks pass (UI also needs a valid capture). A new revision invalidates every verdict whose inputs changed; when out-of-scope cannot be proven, invalidate. Deterministic checks always rerun. |
| Q7 | **Integration review** only where one checkpoint consumes another's unmerged output, or a landing combines ≥2 scopes sharing an interface or file. Otherwise deterministic checks on the composed tree only. |
| Q8 | Budgets count fix rounds per (checkpoint, gate kind); plan review keeps its own budget. Invalid-capture and environment/adapter failures do not spend implementation rounds; they have a separate small retry bound and route to the environment owner. A repeated finding without progress ends the loop early. One **escalation** per checkpoint and one for the plan; then pause with work preserved and the smallest blocker surfaced to the user. |
| Q9 | Only explicit commands (submit, amend, ack, review-result ingest) trigger gates and transitions. Hooks notify or nudge only. `office status`/resume reconstructs pending jobs and deliveries from durable state. A hook failure may delay; it never mints a pass. |
| Q10 | **Authorization** binds to requirements version + **authority envelope**. Ordinary and contract amendments preserve it; an amendment that adds or changes an envelope entry needs fresh authorization for that entry only; a requirements change invalidates it. **Plan-review status** is a separate record. |
| Q11 | SQLite `runs.db` becomes the single authoritative store; one semantic transition = one transaction; JSON files become generated read-only views. See ADR 0001. |
| Q12 | Runtime queues a scoped delta per affected executor, attaches it to that executor's next `office` command response, and redelivers after resume (**delivered**). Executor confirms with `office ack` at its next safe boundary (**applied**). A submission under an older applied version gets "amendment pending": not a failure, no budget round. |
| Q13 | Each executor receives one combined delta from its applied version to current. Newer amendments **supersede** unapplied older ones. An ack for a superseded version is rejected and the current delta returned. |
| Q14 | A **stale result** is audit-only and never passes a gate. Its open findings carry forward to the current revision; the next review confirms or **retracts** them. Plan defects on older plan revisions stay blocking until cleared. Plan review ends at the first PASS on any plan revision or at budget exhaustion; after that only contract amendments get a delta plan review. |
| Q15 | Dependency gets an ordinary amendment: dependant continues. Contract amendment touching an interface the dependant consumes: dependant pauses and resumes with the delta. Either way, dependant acceptance waits for the dependency's acceptance on the revision it built on, or a re-gate after rebase. |
| Q16 | Irreversible actions, landing, and closeout wait for all required reviews, including a concurrent plan re-review and any open plan defect on the affected scope. Dependants may start on a submitted-but-unaccepted dependency revision; their acceptance waits (Q15). |
| Q17 | One **lease** per scope in the existing SQLite `leases` table, renewed by any `office` command from the holder. Takeover only after expiry **and** confirmed-dead prior process, or explicit orchestrator revoke. New holder resumes from the latest snapshot. Revisions carry the lease ID, so a late submit from a former holder is rejected. |
| Q18 | Submit operation ID = (task, lease, tree hash); duplicates return the same receipt with no new reviewer or version bump. Gates run as jobs under a run-scoped worker that exits when idle (no mandatory daemon). Submit returns a pending receipt promptly. Results reach the executor via the Q12 channel; the orchestrator hears only acceptance, escalation, or a blocker. |
| Q19 | One verdict enum: `PASS \| CHANGES_REQUIRED \| PLAN_DEFECT \| BRIEF_DEFECT \| UNAVAILABLE`. Retire `IMPLEMENTATION_DEFECT` (a code gate expresses it as `CHANGES_REQUIRED` with material findings). Spaced prose forms are accepted only by a migration shim. |
| Q20 | The orchestrator declares the amendment kind. The runtime rejects an "ordinary" amendment whose delta changes anything detectably contract-level: a scope's ownership or boundary, a declared cross-scope interface, or an authority-envelope entry. A plan reviewer can still flag a disguised contract change as `PLAN_DEFECT`. |

## 2. Plan-review fork

| First plan-review verdict | Executors | Plan review |
|---|---|---|
| `PASS` | Launch ready executors. | Ends. |
| `CHANGES_REQUIRED`, no open plan defect | Orchestrator applies an ordinary amendment, then launches eligible executors **before** the next result. | Amended revision re-reviewed concurrently; ends at first `PASS` or budget. Later findings become ordinary amendments delivered per Q12. |
| `PLAN_DEFECT` / `BRIEF_DEFECT` open | None for the affected scope (whole plan if the defect is plan-wide). | Owning role amends (Q3); blocks until clearance (Q2); budget and escalation per Q8. |
| `UNAVAILABLE` | None until a substitute reviewer or user decision. | Routed to substitute; counts as environment failure (Q8), not a plan round. |

A defect raised mid-run pauses the affected scope and its dependency closure only. Unrelated scopes continue.

## 3. Transition and authority table

Every row is one command and one SQLite transaction. "Who" is the only role that may invoke it. Hooks never appear in the Who column.

| Transition | Who | Preconditions | Effects in the same transaction |
|---|---|---|---|
| authorize | user | requirements version frozen | authorization(requirements_version, envelope) |
| authorize-envelope-entry | user | amendment pending on that entry | envelope entry added/changed |
| ingest plan review | runtime (from reviewer job) | result bound to a plan revision | plan-review status; findings; stale results marked audit-only (Q14) |
| amend (ordinary) | orchestrator | no detectable contract-level change (Q20); budget not exhausted | plan/routing version bump; per-executor deltas queued; superseded deltas marked |
| amend (contract) | planner | orchestrator requested; affected scopes paused | plan version bump; deltas queued; delta plan review job queued; envelope entries flagged for re-authorization if touched |
| record requirements change | orchestrator on user's instruction | user quote recorded | requirements version bump; authorization invalidated |
| clear defect | runtime (from independent plan reviewer) | reviewer confirms `finding_id` on a revision containing the fix | finding cleared; affected scopes unpaused if nothing else blocks |
| acquire / renew / release lease | executor (via any command) / runtime | Q17 rules | lease row; fencing token |
| revoke lease | orchestrator | named scope | lease revoked; holder's later submits rejected |
| ack | executor | delta delivered; version not superseded | applied version recorded for that executor |
| snapshot | executor or runtime | lease held | durable save; no gate effect |
| submit | executor | lease held | revision commit; operation ID dedup; gate jobs queued; "amendment pending" if applied < current |
| ingest gate result | runtime (from gate job) | result bound to a revision | verdict recorded; findings carried forward or retracted; stale if revision not current |
| accept checkpoint | runtime only | every required gate `PASS` on the same current revision; applied version = current; no open plan defect on scope; dependency acceptance per Q15 | acceptance record |
| escalate | runtime | budget exhausted or no-progress detected; escalation unused | one escalation job; afterwards pause + user blocker |
| land / irreversible action / closeout | orchestrator, with user authorization where required | Q16 barriers satisfied; authority envelope covers the action | landing record |

## 4. Amendment and invalidation rules

1. A plan or routing bump never invalidates authorization (Q10). Only a requirements change or a touched envelope entry does, and the latter only for that entry.
2. A new revision invalidates each gate verdict whose inputs changed. Unknown impact means invalidate (Q6).
3. An ordinary amendment invalidates only the checkpoints whose acceptance detail or tests it changes. Their next submit is judged against the new version once applied.
4. A contract amendment pauses affected scopes and the dependency closure of any interface it touches (Q15), and queues a delta plan review (Q14).
5. Evidence reuse is allowed only when the gate's full input key (revision, versions, reference, environment) is unchanged. Anything else reruns.
6. Stale findings carry forward. Stale passes do not.

## 5. Trigger contract

- Triggers: `submit`, `amend`, `ack`, `authorize`, review/gate result ingest, lease revoke. Nothing else changes state.
- Delivery rides on command responses (Q12) and resume reconstruction. Native harness messaging, where proven, is an optimization, never the only path.
- Hooks may run `office status` or print nudges. A hook that errors, times out, or is absent leaves state unchanged.
- The run-scoped job worker is started on demand by a command that queues a job and exits when the queue is empty. On a host that cannot keep a detached process alive, jobs resume on the next command or `office status`.

## 6. Scenarios

Positive (must hold):

- S1 First `PASS`: executors launch; zero further plan-review jobs.
- S2 First `CHANGES_REQUIRED`: amendment committed, executor job and re-review job both queued before any second result exists.
- S3 Re-review returns `PASS` for plan v2 while ordinary v3 exists: plan review ends; v3 deltas continue delivery; no v3 review job.
- S4 Two independent ready checkpoints with disjoint leases run and accept concurrently; orchestrator chose to run them together.
- S5 Code and UI reviews run on the same revision; both `PASS`; checkpoint accepts.
- S6 Ordinary amendment to dependency A while dependant B runs: B continues; B accepts only after A accepts on B's base revision.
- S7 Crash after amendment commit, before delivery: on resume the delta is delivered once and the version is not bumped again.
- S8 Duplicate submit after a lost response: same receipt, one reviewer job.
- S9 Hookless harness completes the full lifecycle using explicit commands only.

Negative (must be rejected or blocked):

- N1 Initial open `PLAN_DEFECT`: zero executor launches in the affected scope.
- N2 Orchestrator attempts to clear its own defect: rejected.
- N3 Orchestrator labels an ownership change "ordinary": runtime rejects (Q20).
- N4 Dirty edit, unchanged HEAD: prior `PASS` does not satisfy the new submit.
- N5 Stale `PASS` for revision r1 arrives after r2 submitted: recorded audit-only; r2 not accepted.
- N6 Executor submits under applied v2 when v3 is current: "amendment pending", no budget spent, no acceptance.
- N7 Ack for superseded v2 after v3 queued: rejected; current delta returned.
- N8 Former lease holder submits after takeover: rejected by fencing token.
- N9 Expired lease with a live holder process: takeover refused without explicit revoke.
- N10 Landing while a concurrent plan re-review is pending or a defect is open on the scope: blocked.
- N11 Plan amendment adds a production deploy: that envelope entry needs user authorization; other work unaffected.
- N12 Hook crashes during submit: no pass minted; state recoverable from `office status`.
- N13 Invalid capture three times: no implementation round spent; routed to environment owner; bounded.
- N14 Same finding repeated with no code change between rounds: early stop, escalation, then pause.
- N15 Concurrent completions from two workers: no duplicate event sequence, no lost ack.

## 7. Exact amendments to current v3 contracts

Prose:

| Location | Change |
|---|---|
| `protocol/lifecycle.md:3` | Replace the fixed "plan review → execution packets" order with the §2 fork. |
| `skills/auto-planning/SKILL.md:16-17` | Packets may be emitted after first `CHANGES_REQUIRED` once the ordinary amendment is committed. Defects are handled by contract amendment (Q3), not by an unconditional version increment. |
| `skills/auto-review/SKILL.md:22-32` | Replace the "second CHANGES REQUIRED is a hard stop" rule with Q8 budgets, the no-progress stop, and one escalation. "Accept with named gaps" becomes a user choice after escalation. Quote the Q19 enum instead of spaced forms. |
| `protocol/roles-and-authority.md:4,7` | Orchestrator declares amendment kind (subject to Q20 checks) and owns ordinary amendments. Planner owns contract amendments. Defect clearance belongs to an independent plan reviewer. |
| `protocol/families-and-amendments.md:30-40` | Map kinds: routing and ordinary plan changes → ordinary; `plan_contract` → contract; requirements delta → user-originated requirements change. Add delivered/applied/superseded semantics and the combined-delta rule. |
| `docs/v3-runtime-contracts.md:388` | Adopt the Q19 enum and the acceptance evaluator preconditions from §3. |

Code and schemas:

| Location | Change |
|---|---|
| `scripts/office_runtime.py:790-808` `plan_review_round_authorized` | Authorize a concurrent re-review after the first `CHANGES_REQUIRED`; include `BRIEF_DEFECT`; stop at first `PASS` or budget. Update `tests/test_runtime.py:608-629` deliberately. |
| `scripts/office_runtime.py:1596-1649` `_invalidate_approval_for_version_change` / `increment-plan` | Stop dropping authorization on plan bumps (Q10). Merge into the single amend transition. |
| `scripts/office_runtime.py:1040-1079` `approve-plan` | Split into `authorize` (user) and plan-review status. Store the authority envelope. |
| `scripts/office_family.py:383-490` `apply_amendment` | Becomes the single amend transition in SQLite. Keep optimistic `expected_prior_versions`. Add Q20 detection, delta queueing, and supersession. |
| `scripts/office_family.py:620` `save_checkpoint`, `schemas/checkpoint.schema.json` | Rename to snapshot. Add a separate acceptance record keyed to revision. |
| `scripts/office_runtime.py:1082-1125` leases | Add process-liveness check before takeover and a fencing token recorded in each revision. |
| `schemas/review-result.schema.json:90-95` | Canonical enum (Q19). Add `revision` binding (Q5) alongside `reviewed_head_sha`. |
| `schemas/finding.schema.json`, `review-result` `findings[].status` | Add `retracted` and `superseded`. |
| `schemas/amendment.schema.json` | Add `class: ordinary \| contract` and envelope-entry changes. Keep `kind` for version field ownership. |
| `scripts/review_finding.sh:7,58`, `scripts/review_loop.sh:290-325` | Drop `IMPLEMENTATION_DEFECT`. Stop calling `increment-plan` directly. |
| `scripts/review_loop.sh:48,82` `MAX_ITERATIONS` / `max_review_iterations` | Retire. Use the config budgets from `gear_presets.*`. |
| `config/config.default.yaml:73-127` | Add the environment-failure retry bound. No other budget knobs. |

## 8. Writer consolidation

One state module owns every write to `runs.db`. These current writers become callers of it or are retired:

- `office_runtime.py` `_atomic_write_json` call sites (`state.json`, `envelope.json`) and the non-atomic `write_text` in `increment-plan` / `invalidate-packets` (`:1591`, `:1640`)
- `office_family.py` registry, amendment, checkpoint, landing, and review writes under `.family_registry.lock`
- `review_finding.sh:53,92` findings JSON and SQLite insert
- `verify.sh:118` validation insert
- `office_monitor.py:146,151,320` event append and cursor replace
- `office_spawn.sh:196,236` pane ledger and start receipt

JSON files remain as generated read-only views for inspection and backward-compatible readers during migration.

## 9. Left to other tickets

- Command names and the exact executor/orchestrator command set: [the minimal CLI prototype](https://github.com/Hikari9/auto-office/issues/159).
- UI capture validity, drift thresholds, and specialist fallback: [the visual-drift decision](https://github.com/Hikari9/auto-office/issues/158).
- Migration order and pruning ratification: [the pruning ratification](https://github.com/Hikari9/auto-office/issues/157).
- The environment-failure retry bound's numeric default is left to the pruning/acceptance ratification, measured against fixtures.
