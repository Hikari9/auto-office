# Issue #1143 — adaptive executor policy, qualified Hydrogen trials and rollout record

**Integration base:** #1142 (`catalog/hydrogen-executor-seeds-20261114`). Do not merge this implementation ahead of the catalog/conformance work. **Status:** implementation candidate; `routing.hydrogen_trials.enabled: false` in shipped config; no automatic Hydrogen execution authorized by a draft/merge alone.

## Owner-approved effective seed (advisory, not forced)

| Size | First preference rung | Next rung |
| --- | --- | --- |
| XS | Claude Haiku 5.5 Medium | Codex Terra 6.1 Low; Agy Hydrogen Low (manual only) |
| S | Codex Terra 6.1 Medium; Agy Hydrogen Medium (gated) | Claude Sonnet 5.5 Medium |
| M | Claude Sonnet 5.5 Medium; Agy Hydrogen High (gated) | Codex Terra 6.1 High |
| L | Claude Sonnet 5.5 High | Agy Hydrogen High (manual only) |
| XL | Claude Opus 5.5 Medium | Codex Sol 6.1 Medium; Agy Argon High |

`rung` equality means peers have identical preference weight, **not** identical expected effectiveness/cost/speed/quota; the balanced 40/25/15/10/10 adaptive weights remain intact. Planner and review candidate policies remain separate; Fable remains planner-only, Gemini Flash remains a visual-review route. `review.max_rounds=3` and AA Coding/Intelligence Index v4.3.2 remain pinned; #862 alone activates v5.

## Hard Hydrogen gates

The route is discovery-eligible but not catalog-dispatchable. An ordinary `role allow`, missing score waiver, adaptive exploration, `--reroute`, general discovery, automatic fallback, or historical role pin does not make it an ordinary auto executor. The only automated path is an explicit exact-probe-qualified discovery trial on an **S/M** task with a declared `bounds: bounded`, reversible `local`/`repo` run, non-sensitive domain, independent code review, available same-size **non-Hydrogen** fallback, and owner-enabled switch. XS Low and L High can only be specifically named manually, with fresh exact model+effort probe evidence. XL and non-executor roles are rejected even manually. Unknown new task size returns `size_unclassified`; historical pinned unclassified runs keep their original path without silently mapping to L.

`route_probe.status` is an exact fingerprint receipt: invocation ID, effort, adapter hash, harness profile and freshness. Synthetic tests verify decision mechanics only; they never authorize production. A probe denial, isolation failure, expired probe, quota/auth error, missing launch readback, or denied role fails closed. The live operator must independently verify authorization, sandbox, process cleanup and no privilege escalation, then explicitly enable the switch for **new** runs; retained runs stay pinned.

## Atomic 20-opportunity counter

The `hydrogen_trial_opportunities` table in the existing WAL `runs.db` is append-only, with UPDATE/DELETE rejection and immutable auto-increment sequence. The write is made under the **same `BEGIN IMMEDIATE`** transaction that creates the dispatch. Each actual eligible automatic non-inline S/M dispatch writes one opportunity, including an ordinary non-Hydrogen dispatch. Until **19 eligible earlier dispatches** exist, all Hydrogen trials are denied (conservative cold start). After that, the last 19 eligible events plus the new trial may contain at most **3** experimental reservations. At most **one** Hydrogen experimental reservation per run for the lifetime of the ledger. Reserved attempts permanently count even if launch fails, the provider errors, or a run is abandoned; there is no success-only or retry cap reset. Failed/blocked reservations fall back to a qualified known-working same-size route, not to a second Hydrogen selection. The receipt records previous window count, accepted/blocked status, selected route and post-reservation count.

All concurrent Office processes **must use the same state `runs.db`**. No automatic trials on separate independent DB copies; migrating/moving the DB requires transferring the complete ledger. If the counter/DB is unavailable or corrupted, disable the switch and block experiments until the ledger is restored; do not reset the window. Historical nontrial launches from before this ledger are not reconstructed, so cold start is deliberately conservative.

## Quota and escalation

Executor reserves remain 20%, independent Luna reviewer reserves 5%; Codex reviewer quota is not reallocated to executor. A provider/account/shared-pool cooldown is recorded **only** when an actual quota probe explicitly supplies verified `provider`, `account_id` and `pool_id` metadata. Missing account identity is not proof of cross-account sharing. Quota exhaustion is distinguished from unsupported model/effort and transient transport errors; hard auth/authorization failures stop for operator action. The existing convergence loop stays at three rounds, and repeats escalate only when two substantive RECHECK finding sets are **identical**, not merely overlapping.

## Release gates / rollback

1. #1142 fully qualified and merged: exact Agy route IDs and effort, selected identity readback, auth, sandbox, writable boundary, child cleanup, quota and usable fallback, verified with live receipts for S Medium and M High (and XS/L before manual use). Do not infer sibling efforts.
2. This PR targeted, contract and full CI green, independent code review, doctor/smoke pass, and dry-run ledger/rollback verification with real profiles. Keep first authorized trial low-risk and reversible, observe counters and fallback receipts, retain #1143 **open until trial deployment verified**.
3. **Emergency rollback:** set `routing.hydrogen_trials.enabled: false` on the operator's current user/repo config for **new runs** (and repin active runs via the supported `office config --run <id> --apply-routing` path). Stop pending trial dispatches; roll back this PR/release if needed, **do not delete/alter the append-only ledger** or revoke legitimate historical pins. Keep visual review unchanged. If configuration cannot be applied to pinned runs safely, pause the run and escalate to operator, never silently run it.

No exact conformance receipt, live authorization/sandbox proof or actual first authorized rollout was available in the remote test sandbox when this implementation was prepared. This record is an implementation check list, **not** a production conformance claim.
