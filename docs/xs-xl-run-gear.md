# XS–XL complexity gears and independently sized task dispatch (#809)

This is the normative runtime boundary for **new runs**. It complements—not replaces—the [SDLC contracts](../README.md), runtime safety gates, pinned user requirements, authorization, reproducible tests, lease fencing, independent convergence and release approvals.

## Pinned run gears (complexity is not risk)

| Run gear | Planner | Producer mode | Producer self-review | Independent plan review | Independent convergence | Verification |
| --- | --- | --- | --- | --- | --- | --- |
| XS | Minimal inline plan | Orchestrator-inline, bounded low-risk by default | Single-tier, one-round ledger; automatic main-change red/green; no task subagent | Only if risk requires | Only if risk requires | Deterministic and applicable acceptance checks |
| S | Compact plan | Dedicated executor; inline only on recorded cost-to-success evidence | Single-tier, one-round ledger; one combined runtime edge/test-strength audit, platform by diff | Risk floor only | Yes | Deterministic and applicable acceptance checks |
| M | Structured plan | Dedicated executor; documented single-task inline exception possible | Single-tier, one-round ledger; one combined runtime edge/test-strength audit, platform by diff | Risk floor only | Yes | Deterministic and applicable acceptance checks |
| L | Deeper coordinated plan | Dedicated executor(s) | Single-tier, one-round ledger; one combined runtime edge/test-strength audit, platform by diff | One independent reviewer | Yes | Deterministic and applicable integration checks |
| XL | Dedicated architecture/decomposition | Multiple coordinated waves, independent task sizing | Single-tier, one-round ledger; one combined runtime edge/test-strength audit, platform by diff | Independent **lead + 3 mandatory Goldfish**; up to 2 evidence-triggered hazard audits | Yes | Full applicable integration/visual/recovery verification |

A new run's **complexity** gear is pinned by the orchestrator at intake (or explicit authorized creation) from scope, coupling, uncertainty and multiwave coordination; the **risk** classification and floor remain independent. The planner may recommend size but cannot silently weaken the run's effective gates. Risk amendments need the existing authorization path; any change to accepted user scope needs its existing approval. The plan reviewer is **not** a gear-policy reviewer and has no L-to-XL reviewer-triggered escalation or reviewer-managed swarm.

Historical gear aliases are still accepted only when explicitly selected for compatibility. Such starts retain the original legacy funding contract, emit a compatibility notice, and cannot be mistaken for new XS-XL runs. CLI help advertises XS-XL as the default; removing explicit aliases would break established legacy start integrations. Existing pinned runs resume unchanged.

### Self-review subagents (`self_review_subagents.py`)

Only pinned run-gear-1 runs fund a runtime subagent. XS funds none; S, M, L and XL fund
**one** combined read-only edge/test-strength audit per task (with platform/build review only
for relevant actual changed files). No gear funds four parallel task specialists. Missing,
invalid or wrong-commit reports block convergence without spending a round. Security is a
mandatory independent convergence pass for sensitive changed paths or newly added auth,
permission, input-parsing or production-write code. The one-round producer review retains a
test-strength lens, plus automatic main-change red/green proof in isolated checkouts; see
docs/review-convergence.md. A 15% self-review token budget is declared but currently not
precisely metered: the producer marks TRUNCATED when exhausted.


### Real XS inline vs S/M exception

New XS tasks with low risk and bounded XS/S-size work acquire their normal fenced lease and isolated worktree, but use an **external/orchestrator worker session**. The CLI explicitly says **no executor model launched**; the orchestrator completes the implementation under Office's worker environment and uses `office preflight` and `office submit`. Neither this nor any S/M exception bypasses the worker's scope, authorization, evidence, deterministic checks, independent review or landing gates. A single task alone is not an exception. A risk classification is never a complexity classification.

Inline rules (`task_sizing.execution_mode`), applied per task, each recording owner, reason and estimated savings:

| Run gear | Inline when | Otherwise |
| --- | --- | --- |
| XS | low risk and the task is XS or S (an M task delegates) | dedicated executor |
| S | low risk, an XS-M task and a measured `inline=<n> dedicated=<n> <unit>: <evidence>` estimate with both costs above zero, at least 12 characters and 3 words of evidence, and inline strictly cheaper | dedicated executor |
| M | low risk, an XS-M task and the run has exactly one task and a measured, strictly cheaper total cost-to-success estimate | dedicated executor |
| L, XL, legacy gears | never | dedicated executor |

Elevated risk always delegates. An inline task keeps its fenced lease, scope, self-review subagents, convergence review and landing.

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

### Pinned Intelligence Index floors and ideal executor slates

Only **new XS–XL run gears** apply the following intelligence safeguards to each
independently sized *implementation subtask*, using **Artificial Analysis Intelligence
Index v4.3.2**. A parent XL run can dispatch an S task against the **S floor 38**; the
parent gear never supplies a stronger/weaker subtask floor. An equal score passes.
Missing, wrong-version, nonnumeric, or below-floor evidence fails ordinary execution.
Legacy pinned run presets retain their historical contracts.

| Subtask size | Minimum intelligence |
| --- | ---: |
| XS | 31 |
| S | 38 |
| M | 40 |
| L | 44 |
| XL | 47 |

User-approved soft **ideal executor ordering**, assuming all exact model+effort+harness
routes qualify and quota is available. This is **not** a command to launch unavailable
models, override factual gates, or prefer an expensive model regardless of task fit.

| Size | Preferred | Immediate same-model harness self-fallback | Next qualified alternative | Last qualified alternative |
| --- | --- | --- | --- | --- |
| XS | Claude Haiku 5.5 medium (34; conditional) | — | Gemini 3.8 Flash low (33) | GPT-6.1 Terra low (36; conditional) |
| S | Gemini 3.8 Flash medium (40) | — | Claude Haiku 5.5 high (38; conditional) | GPT-6.1 Terra medium (41; conditional) |
| M | **Claude Code** Sonnet 5.5 medium (41; currently non-dispatchable) | **agy** Sonnet 5.5 medium (41; exact same model/effort) | GPT-6.1 Terra medium (41; conditional) | OpenCode GLM-5.3-Flash max (42; conditional) |
| L | **Claude Code** Sonnet 5.5 high (47) | **agy** Sonnet 5.5 high (47; same effort) | GPT-6.1 Terra high (44; conditional) | Gemini 4 Argon **medium** (conditional, exact score not yet verified) |
| XL | Claude Code Opus 5.5 medium (51) | — | Codex GPT-6.1 Sol medium (48; discovery-only) | Gemini 4 Argon **high** (53; conditional) |

**Required guardrails:** Sonnet 5.5 Claude Code → agy is the only same-model
harness fallback, at the **same effort**, immediately before switching model family.
Claude Code Sonnet Medium cannot dispatch until its missing exact invocation/effort
evidence is verified; a currently qualified agy variant may be selected instead.
Reserve Opus 5.5 automatic executor routing for **genuinely XL subtasks**, even inside
an XL parent run. No active MiMo routes. Haiku 5.5, Sol 6.1, Terra 6.1, Argon and
GLM conditional rows are never promoted merely because they appear here.
L Argon **Medium** cannot inherit Argon High's AA score. A user-approved, auditable
exception (`executor_slates.floor_exception`) lets exactly `gemini-4-argon` at
`medium`, executor role, subtask size `L` pass only the AA intelligence-floor check
while its Medium score is unpublished: no score is fabricated, the 44 floor stays for
every other model, effort and size, a published score is judged on merit, and
registration, conformance, trust, quota and risk gates still apply. Each use is
recorded as `routing.floor_exceptions` in the routing audit. A currently
eligible route may depart from this ideal order for cost-to-success, benchmark fit,
harness capability, quota, latency, prior rework, domain or explicit user preference.
Preview and actual dispatch must explain that departure, the skipped candidate,
and the correct fallbacks.

GPT-6.1 Terra's six version-pinned AA scores (non-reasoning 33, low 36, medium 41,
high 44, xhigh 45, max 48) are registered by merged PR #811 as **conditional catalog entries**
(`dispatchable: false`, discovery-eligible), not active dispatch claims until they pass
conformance. GPT-6.1 Sol Medium is also discovery-only until exact harness
qualification. Bounded **discovery trials** can investigate missing benchmark
provenance only where existing S/M discovery consent and risk rules permit, but
must never trial a **known below-size-floor score** or violate the XL Opus rule.
A discovery probe is not ordinary dispatch eligibility.

### Example: mixed-size XL run (illustrative, not a live dispatch)

A hypothetical cross-service reconciliation run: effective **XL** gear because 3 coupled services, shared interfaces, multiwave integration and recovery; risk **repo** (not production). Lead independent plan reviewer + requirements/architecture/readiness Goldfish, one authoritative verdict; convergence, verification and authorization still apply.

- Wave 1 `T1 M` shared schema discovery: open bounds and API review → `discovery`, delegated; choose from the live candidate slate.
- Wave 2 `T2 XS` bounded docs/config cleanup, 1 scope, no API change → `lean`, delegated **because run gear XL**, never XL-sized task or XL reviewer just because of the run.
- Wave 2 `T3 L` multi-service failure recovery, interfaces+dependency edges → `deep`, delegated; candidate can be much stronger than T2.
- Wave 3 `T4 S` test fixture updates, reversible single scope → `balanced`, delegated with task-sized historical analogues.

`office inspect plan` / `PLAN.md` show the pinned gear reason and funded gates, each task's size rationale, versioned profile, **preview-only** preferred route and qualified fallbacks, and actual dispatched route after launch. `office dispatch` shows the selected route or explicitly identifies orchestrator-inline preparation (never a fake model launch). Replans and retries write versioned route-audit deltas and keep previous evidence.

## XL Elephant/Goldfish review boundary

The Elephant owns discovery, decomposition and the effective plan. The runtime—not the plan reviewer—funds XL. Exactly **three mandatory, independently launched** Goldfish review (1) frozen requirements and comprehension, (2) actual repository architecture, reuse and failure modes, and (3) implementation readiness and integration. The runtime selects **at most two** safety/interaction/security hazard auditors from evidence and logs each reason. The lead receives the independent structured findings, deduplicates and emits **the one normal plan verdict**. Missing or invalid Goldfish output blocks the lead; it is never synthesized as an approval. Reviewers see frozen requirements, the plan and repository evidence, never the planner's private conversation. L runs, including L runs with XL-sized tasks, never invoke the XL fanout.

## Read-only rollout observations (not release authorization)

To inspect the settled results of an XS–XL pilot on a **copy** of `runs.db`:

```bash
python scripts/issue809_rollout_report.py --runs-db /path/to/COPY/runs.db
```

The report covers only newly pinned XS–XL runs and reads recorded `runs.db` data:

| Metric | Source |
| --- | --- |
| Cost per accepted task | summed `money_actual` over every attempt, only when **every** attempt has a recorded value (missing money is never $0) |
| End-to-end latency | per accepted task, task row creation (plan acceptance, so queue and wait count) to the last `task.accepted` event; mean, median, max, sample count and the number of accepted tasks without usable timestamps |
| First-pass acceptance | share of tasks whose first executor attempt succeeded |
| Rework | executor dispatches after the first, total and by first size |
| Undersizing | tasks whose later attempt carries a larger size than the first (`undersized_tasks`) |
| Review gate violations | `review.degraded_fallback` events, where a non-independent reviewer stood in |
| Permission gate violations | `submit.refused` events with code `outside-scope` |
| Landing gate violations | `pr.merged` events with no unrevoked `merge` authorization at or before the merge |

`gate_violation_count` is the sum of those recorded signals. A zero is the absence of a recorded signal, so `gate_violations_certified_zero` stays `false`. Worker-active seconds are reported separately and are not latency.

**Unexecuted counterfactual routes are not measured.** The report observes the routes that ran; it cannot say how another model would have done, and `counterfactual_routes_measured` is `false`.

**Still required before production/default rollout:** representative local history and first-pass/rework calibration; exact Codex/Claude/agy/Opencode harness conformance; a staged rollout owner with rollback and monitoring. A report against an empty or synthetic database authorizes no model promotion, reviewer bypass or release.

## Herdr pane labels

Every Office-launched Herdr pane for a task, executor or reviewer, shows the task's size class as a bracketed tag before the model tag, for example `auto-office T1 #809 PR-A gears [L] [sonnet high]` or `... review [L] [sonnet high]`. The size is the task descriptor's `task_size`. A task with no size, and plan and integration panes that have no task, show no tag.

## Legacy migration and pin safety

The historical six presets (`direct`, `direct+review`, `light`, `quick`, `express`, `full`) continue to parse and retain their prior gating. Their `gear_model_version` is `legacy-pinned`; they are never silently mapped to XS–XL, and `direct` risk-raising amendments retain their old `express`/`full` upgrade contract. The new `run-gear-1` is only for a newly selected XS–XL run. Older descriptors without a task size retain their original run-size fallback during routing. Historical event, revision, review, learner and catalog data are never rewritten. In every version, explicit user authorization, scope/lease fencing, immutable revision-bound review, checks, integration and real merge/deployment approvals stay authoritative.


## M executor Haiku High transition (#861, October 28, 2026)

For new M subtasks, the ideal slate is Claude Code Haiku 5.5 High, OpenCode
GLM-5.3 Flash Max, then Codex GPT-6.1 Terra Medium. GLM is preferred F1,
but currently has no qualified exact shipped catalog route: report its
missing/unqualified status and skip to Terra F2 when eligible. Never claim
GLM was selected or dispatched when absent. Sonnet Medium is no longer a
preferred M route, but stays a disclosed operational fallback until qualified
preferred routes are available. Other sizes, planner/reviewer funding and
historical pinned runs are unchanged.

The exact `claude/claude-haiku-5-5@high` M executor/worker route has a
narrow owner-authorized AA v4.3.2 38-vs-40 size-floor exception. The real
numeric score must be present, not invented or inferred from AA v5. The
exception does not bypass registration, exact CLI/model/effort conformance,
quota, discovery trial limits, permissions or safety. Keep the catalog
`dispatchable: false`, `discovery: eligible` until proven; fall back to
known-working routes in the meantime.

**Separate explicit user-authority path (#837/#845/#850):** An authenticated
user-owned exact role allow, user-configured preferred route, or validated
override receipt may intentionally authorize a catalog-disabled route. This
does **not** prove that the CLI can launch the requested model and effort, does
not change the shipped catalog status, and is never inferred from the default
size slate. The user's rules take precedence over shipped preferences. Such
explicit authority follows its own scope, receipt and remaining runtime
safeguards; it must not be portrayed as automatic Haiku qualification.
