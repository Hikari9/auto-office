# Engineering handoff: Owner-approved Hydrogen capped canary and adaptive XS–XL routing

**Approval:** November 14, 2026. **Canonical implementation issue:** [#1143](https://github.com/Hikari9/auto-office/issues/1143). **Existing unmerged model/seed PR:** [#1142](https://github.com/Hikari9/auto-office/pull/1142). **AA-v5 scoring research:** [#862](https://github.com/Hikari9/auto-office/issues/862). Fable planner-only registration: [#1133](https://github.com/Hikari9/auto-office/pull/1133).

## Mission

Build the **owner-approved adaptive executor preference + quota-aware escalation policy** without prematurely enabling unqualified Gemini 4 Hydrogen. The high-level approval is complete: **Hydrogen may get capped automatic trials on qualified, low-risk S/M tasks, max 15% in a rolling 20 eligible opportunities and at most 1 experimental trial per run, *after* exact local conformance**. XS/L manual selection only after conformance; XL excluded. Keep independent review and pinned-run semantics.

## Locked preferences

- **XS:** Claude Haiku 5.5 Medium → (GPT-6.1 Terra Low | Gemini 4 Hydrogen Low). XS Hydrogen **manual-only**.
- **S:** (GPT-6.1 Terra Medium | Gemini 4 Hydrogen Medium) → Claude Sonnet 5.5 Medium. Ordinary qualified default bias is Terra Medium, but don't starve Codex Luna reviewers.
- **M:** (Claude Sonnet 5.5 Medium | Gemini 4 Hydrogen High) → GPT-6.1 Terra High. Terra High currently denied at M; allow only explicitly and size-scoped after validation.
- **L:** Claude Sonnet 5.5 High → Gemini 4 Hydrogen High **manual-only**, with **NO Opus Low** L fallback.
- **XL:** Claude Opus 5.5 Medium → (GPT-6.1 Sol Medium | Gemini 4 Argon High). Hydrogen excluded.
- **Unknown:** classify task size and risk; never silently map to L for new automatic selection.
- **Visual review:** Gemini Flash stays working until a separately vision-qualified Hydrogen review path is proven; Flash is removed from new executor seeds. Fable is planner-only.

`|` is a same-rung **adaptive peer** concept; `→` is ordered seed bias/escalation, not forced first-always dispatch. The repository's present `catalog/executor-policy.yaml` stores a **flat** list. Maintain adaptive task-fit and local quality/cost/quota scoring; add group/rung semantics only if needed to faithfully represent the above. Prevent an automatic unqualified Hydrogen peer from being selected through a seed rank or generic `allow`.

## Suggested implementation sequence

### Phase A — qualify first, no Hydrogen live dispatch

1. Inspect the canonical issue and current main branch plus #1142 for conflicts. Confirm the actual selected Antigravity model ID **and effort** through the account's `agy models`/startup readback and isolated Office builder probe for S Medium / M High. Fail closed on model aliases, unavailable effort, authorization or environment incongruence. Record exact versions, timestamps and conformance receipts. Do not invent a v4.3.2 benchmark score or use the official v5.0.1 headline as a v4 floor.
2. Verify tool permissions, sandbox/worktree writes, subprocess cleanup, fallback on failed probes, and the existing per-run/rolling discovery implementation in `src/office/route_policy.py`, `src/office/routing.py`, `src/office/adaptive.py`, discovery event ledger, and config. The new guard must be **Hydrogen-specific** and independently enforced from the general adaptive exploration path.
3. Maintain existing disabled Hydrogen catalog rows until a deliberate canary-path release. If local probes cannot be run on the agent, stop at the gate with precise local commands/instructions; don't mark conformance passed.

### Phase B — implementation

4. Add optional equal-rung model groups to the policy and ranked selection/audit receipts (if needed to achieve faithful semantics), preserving the effective **40% effectiveness / 25% cost / 15% speed / 10% quota / 10% preference** balanced adaptive weighting unless separate evidence justifies a change. Compare the precise code defaults before editing.
5. Implement scoped Hydrogen trial permission gated on: role=executor, exact positive conformance, eligible classified S or M, bounded/low-risk local or repo blast radius, not security/finance/payment/credentials/privileged/production-data/irreversible/infrastructure work, known qualified same-size fallback, healthy required review, and sufficient trial+quota budget. Explicit manual XS/L (after conformance) must be reviewed separately; no automatic XS/L; reject XL.
6. Enforce **one experimental Hydrogen trial per run**, and **no more than 3 Hydrogen automatic trials per rolling 20 eligible S/M dispatch opportunities**, with atomic/cross-process booking and correct handling of concurrent starts, cold starts and aborted launches. Count all opportunities and reserved/launched trials transparently; don't open an unbounded shortcut through a fallback, user-role allow, or general adaptive exploration. A trial is still a trial if the provider errors after launch.
7. Add provider-pool quota-aware routing: protect Codex Luna reviewer reserve while selecting Terra, and demote all genuinely shared exhausted routes after a hard pool wall. Do not infer unsupported quota sharing across distinct accounts/providers. When a first-rung route is disqualified/exhausted, allow the next qualified rung and explain why.
8. Implement M-only Terra High role-policy change (no XS/S broadening), remove Flash from executor preferences only, keep visual reviewer unchanged until vision-qualified alternative. Maintain the v4.3.2 index pin and old run snapshots. Separately record whether the historic Haiku M floor exception remains for pinned runs; no accidental global floor waivers.
9. Preserve convergence-v1 and `review.max_rounds=3`. Two identical, substantive no-progress RECHECKs may trigger evaluation/escalation but **must not** bypass independent reviewer approval, restart an unsafe task or silently reduce the existing review contract. Hard security/auth failures stop immediately; adapter errors do not spend substantive implementation rounds.

### Phase C — tests, release and decision evidence

10. Tests: exact XS/S/M/L/XL + unknown ranks (including L no Opus); all Hydrogen variants unqualified fail closed; positive S/M probe enables *only* capped safe canary; XS/L manual-only; XL always excluded; cap=3/20 under concurrent/interleaved runs; one/run; cold-start and rollback; risk/size/fallback/reviewer absence; source/packaged parity; quota walls and provider reserve; no same-pool retry loop; user-deny precedence; historical pinned v4 runs; Gemini Flash retained in visual; Fable never executor. Simulate no valid fallback and ensure no trial.
11. Run targeted tests and the repository's full required CI, independent review, and smoke/doctor probes for exact selected model/effort. Record command/results with SHAs. Leave PR **draft** until proof is attached. No live deployment before all above gates pass. New runs only may use the approved trial policy; older runs resume under their pinned policy.
12. Record real trial outcomes by size/effort and evaluate before considering higher quota share or equal uncapped peer membership. Target **15–30 independent accepted trials per relevant S/M effort** as an *evidence target, not an automatic activation trigger*, subject to safety/quality. Pause on any trust/permission breach, severe/irreversible effect, missing fallback, repeated pool quota starvation, or statistically meaningful review/land-rate regression. Never infer performance from AA v5 alone.

## Agent execution instruction

Implement #1143 end-to-end using the current repository conventions and existing draft #1142. Treat all owner choices above as locked; derive ordinary technical implementation details yourself. **Do not ask again whether the canary is authorized**—it is approved conditional on conformance. Do not silently enable Hydrogen using a generic role allow or benchmark waiver. If exact local agy conformance or account-specific quota receipts cannot be verified in your environment, complete all safely implementable code/tests, leave activation gated, and provide a precise local-agent handoff for the missing probes. Do not merge/deploy until full checks and independent review pass; leave #1143 open until tested deployment and canary receipts exist. Keep AA v5 score migration #862 independent.
