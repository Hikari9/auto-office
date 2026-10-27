# XS–XL complexity gears and independently sized task dispatch (#809)

This is the normative runtime boundary for **new runs**. It complements—not replaces—the [SDLC contracts](../README.md), runtime safety gates, pinned user requirements, authorization, reproducible tests, lease fencing, independent convergence and release approvals.

## Pinned run gears (complexity is not risk)

| Run gear | Planner | Producer mode | Producer self-review | Independent plan review | Independent convergence | Verification |
| --- | --- | --- | --- | --- | --- | --- |
| XS | Minimal inline plan | Orchestrator-inline, bounded low-risk by default | Four-lens inline ledger | Only if risk requires | Only if risk requires | Deterministic and applicable acceptance checks |
| S | Compact plan | Dedicated executor; inline only on recorded cost-to-success evidence | Four-lens ledger plus **one genuine runtime subagent** | Risk floor only | Yes | Deterministic and applicable acceptance checks |
| M | Structured plan | Dedicated executor; documented single-task inline exception possible | Ledger plus **one genuine runtime subagent** | Risk floor only | Yes | Deterministic and applicable acceptance checks |
| L | Deeper coordinated plan | Dedicated executor(s) | Ledger, plus runtime specialist audit if high complexity/risk | One independent reviewer | Yes | Deterministic and applicable integration checks |
| XL | Dedicated architecture/decomposition | Multiple coordinated waves, independent task sizing | Deeper ledger plus **two runtime subagent audits** | Independent **lead + 3 mandatory Goldfish**; up to 2 evidence-triggered hazard audits | Yes | Full applicable integration/visual/recovery verification |

A new run's **complexity** gear is pinned by the orchestrator at intake (or explicit authorized creation) from scope, coupling, uncertainty and multiwave coordination; the **risk** classification and floor remain independent. The planner may recommend size but cannot silently weaken the run's effective gates. Risk amendments need the existing authorization path; any change to accepted user scope needs its existing approval. The plan reviewer is **not** a gear-policy reviewer and has no L-to-XL reviewer-triggered escalation or reviewer-managed swarm.

### Real XS inline vs S/M exception

New XS tasks with low risk and bounded XS/S-size work acquire their normal fenced lease and isolated worktree, but use an **external/orchestrator worker session**. The CLI explicitly says **no executor model launched**; the orchestrator completes the implementation under Office's worker environment and uses `office preflight` and `office submit`. Neither this nor any S/M exception bypasses the worker's scope, authorization, evidence, deterministic checks, independent review or landing gates. A single task alone is not an exception. S/M need a validated `inline_advantage` explaining the total cost-to-accepted-result benefit; other work delegates by default. A risk classification is never a complexity classification.

## Per-task sizing, profiles and adaptive routing

At plan acceptance, every implementation task gets `task_size`, `task_size_rationale`, `profile`, `task_size_version`, and `profile_version` in its descriptor. Planner-declared evidence is considered; otherwise runtime uses ownership scopes, interfaces, dependency edges, boundedness, work type and uncertainty. Unknown scope defaults to **M**, not XS. XL tasks with independent ownership/interface boundaries carry a **decomposition recommended** flag. Every task in an XL run, including documentation XS tasks, gets its own route.

The four versioned profiles only change the adaptive router's relative weighting when the effective cost policy is *balanced* (explicit money-saver/quota-saver still wins):

| Task profile | Effectiveness | Expected cost to success | Critical-path speed | Quota | User preference |
| --- | ---: | ---: | ---: | ---: | ---: |
| `lean` | .34 | .34 | .20 | .06 | .06 |
| `balanced` | .40 | .25 | .15 | .10 | .10 |
| `deep` | .56 | .13 | .12 | .10 | .09 |
| `discovery` | .52 | .14 | .14 | .12 | .08 |

Eligibility, trust, verified invocation model/effort, the role capability floor, user denials, reserves, live quota, benchmark context, domain/work matching, historical comparable **subtask** size, critical-path latency, expected rework, same-wave spread and per-task overkill still come from the existing adaptive router. The weights never assign a fixed model to a size. Versioned profiles are audit evidence, not new run gears.

### Catalog qualification at 100% quota (illustrations, not executed dispatches)

An unlimited quota does **not** grant adapter trust, harness availability, role eligibility, or a missing model effort setting. Actual ordered slates are produced from the pinned catalog and live proof on each dispatch. An illustrative **eligible-candidate seed** (rather than a claim of a live ranked choice) based on the current active catalog is:

| Subtask size | First model to evaluate | Additional model candidates (qualify live and rank by adaptive result) |
| --- | --- | --- |
| XS | `agy/gemini-3.8-flash@low` | `codex/gpt-6-luna@medium`, `agy/claude-sonnet-5-5@low` |
| S | `agy/gemini-3.8-flash@medium` | `agy/claude-sonnet-5-5@medium`, `codex/gpt-6-astra@low` |
| M | `agy/claude-sonnet-5-5@medium` | `codex/gpt-6-astra@medium`, `agy/gemini-3.8-flash@medium` |
| L | `codex/gpt-6-astra@high` | `agy/claude-sonnet-5-5@high`, `claude/claude-opus-5-5@medium` |
| XL implementation task | `claude/claude-opus-5-5@high` | `codex/gpt-6-astra@high`, `agy/claude-opus-5-5@high` |

**Do not publish this illustration as a selected or qualified slate.** Selection requires actual installed harness version, trust proof, catalog status and live role qualification, under the currently pinned user policy. Haiku candidates are **retained but conformance-gated**, not promoted merely from model-name speculation. All four decommissioned MiMo V2.6 Pi/Kilo routes are non-dispatchable; their benchmark and old dispatch records remain in the catalog/history for reproducibility.

### Example: mixed-size XL run (illustrative, not a live dispatch)

A hypothetical cross-service reconciliation run: effective **XL** gear because 3 coupled services, shared interfaces, multiwave integration and recovery; risk **repo** (not production). Lead independent plan reviewer + requirements/architecture/readiness Goldfish, one authoritative verdict; convergence, verification and authorization still apply.

- Wave 1 `T1 M` shared schema discovery: open bounds and API review → `discovery`, delegated; choose from the live candidate slate.
- Wave 2 `T2 XS` bounded docs/config cleanup, 1 scope, no API change → `lean`, delegated **because run gear XL**, never XL-sized task or XL reviewer just because of the run.
- Wave 2 `T3 L` multi-service failure recovery, interfaces+dependency edges → `deep`, delegated; candidate can be much stronger than T2.
- Wave 3 `T4 S` test fixture updates, reversible single scope → `balanced`, delegated with task-sized historical analogues.

`office inspect plan` / `PLAN.md` show the pinned gear reason and funded gates, each task's size rationale, versioned profile, **preview-only** preferred route and qualified fallbacks, and actual dispatched route after launch. `office dispatch` shows the selected route or explicitly identifies orchestrator-inline preparation (never a fake model launch). Replans and retries write versioned route-audit deltas and keep previous evidence.

## XL Elephant/Goldfish review boundary

The Elephant owns discovery, decomposition and the effective plan. The runtime—not the plan reviewer—funds XL. Exactly **three mandatory, independently launched** Goldfish review (1) frozen requirements and comprehension, (2) actual repository architecture, reuse and failure modes, and (3) implementation readiness and integration. The runtime selects **at most two** safety/interaction/security hazard auditors from evidence and logs each reason. The lead receives the independent structured findings, deduplicates and emits **the one normal plan verdict**. Missing or invalid Goldfish output blocks the lead; it is never synthesized as an approval. Reviewers see frozen requirements, the plan and repository evidence, never the planner's private conversation. L runs, including L runs with XL-sized tasks, never invoke the XL fanout.

## Legacy migration and pin safety

The historical six presets (`direct`, `direct+review`, `light`, `quick`, `express`, `full`) continue to parse and retain their prior gating. Their `gear_model_version` is `legacy-pinned`; they are never silently mapped to XS–XL, and `direct` risk-raising amendments retain their old `express`/`full` upgrade contract. The new `run-gear-1` is only for a newly selected XS–XL run. Older descriptors without a task size retain their original run-size fallback during routing. Historical event, revision, review, learner and catalog data are never rewritten. In every version, explicit user authorization, scope/lease fencing, immutable revision-bound review, checks, integration and real merge/deployment approvals stay authoritative.
