# #415 — Task-aware routing: research, evidence contract, and staged implementation

**Status:** researched design proposal; implementation parameters are provisional until replayed against local Auto Office outcomes.  
**Research snapshot:** 2026-10-08. **Scope:** executor/worker adaptive routing, per-task metadata, replay and audit. **Not in scope:** altering the planner/reviewer legacy selection pipeline or granting model/harness authority.

## Findings from the actual implementation

- `src/office/planfile.py` parses a task's PLAN.md fields, `src/office/plans.py:sync_tasks` persists them to `tasks`, and `src/office/plan_view.py` previews executor routes by task ID.
- `src/office/candidates.py:adaptive_inputs` currently passes **run-level** playbook, size and gear; `src/office/adaptive.py` uses a single Artificial Analysis Intelligence Index prior. No task-specific benchmark identity is in its candidate evidence.
- `src/office/route_learning.py` trains on **settled, attribution-weighted task-route episodes**, with playbook/size/dispatch-kind comparability, recency and harness-major decay. This is the correct foundation; do not replace route-level trust with descriptor pools.
- `src/office/adaptive.py:_attempt_cost_prior` uses flat input/output price and synthetic per-attempt tokens. The open Haiku 5.5 PR #413 has *catalog tier metadata*, not yet a generic size-aware scorer; #415 must not silently assume #413 merged.
- Existing qualification is independent: exclusions → derived trust → required capabilities → role floor → playbook shape → quota reserve → learned eligibility; adaptive ranking and the absolute cost ceiling happen **only after** qualification. Keep this order.

## What the public evidence does and does not mean

Public scores are **priors, not validated task-outcome probabilities**. Benchmarks measure different constructs and often share tasks, training contamination, graders or agent scaffolds. Do not sum raw percentiles or let model brands substitute for a workload measurement.

| Task dimension | Candidate evidence (version, source and evaluation setup required) | Inferential limit |
| --- | --- | --- |
| UI appearance and visual translation | [Design2Code](https://github.com/jiahuigeng/Design2Code) screenshot-to-code rendering and geometric fidelity; [Arena WebDev](https://arena.ai/leaderboard/code/webdev/overall) blind user pairwise preference on realized apps | Screenshot similarity does not prove usability/taste; human votes conflate prompts, aesthetics, interactions and solution reliability |
| Backend/repository engineering | [SWE-bench Pro](https://labs.scale.com/leaderboard/swe_bench_pro) reproducible repository-resolution outcomes, or equivalent held-out repo issue set; [LiveCodeBench](https://github.com/LiveCodeBench/LiveCodeBench) as *limited* coding/reasoning evidence | Algorithmic puzzles are not integration work; SWE-bench Verified has acknowledged contamination risks ([OpenAI, 2026](https://openai.com/index/why-we-no-longer-evaluate-swe-bench-verified/)); even Pro evaluations require checking tasks, graders and setup ([discussion](https://openai.com/index/separating-signal-from-noise-coding-evaluations/)) |
| Browser/computer use | [VisualWebArena](https://github.com/web-arena-x/visualwebarena) for visually grounded web interaction; [OSWorld](https://os-world.github.io/) for desktop/computer use | Agent scaffold, tools, retries, perception and environment heavily influence success; neither alone measures app-building skill |
| Architecture/complex reasoning | Versioned, held-out architectural-change tasks with independent structural/security review and successful integration, plus the current versioned general reasoning index as fallback | There is **no established universal architecture benchmark** with proven predictive transfer to these Office tasks; don't invent one |
| Long-context work | Documented model context limits, pricing tiers, per-model tokenizer measurements and local context-bucket outcomes | Max context capacity is a capability constraint; long-context quality, throughput and cost are separate variables |

**Provenance contract:** a dimension record must include `source_url`, `benchmark_name`, `version/snapshot_date`, `scoring_direction`, `raw_score`, `normalization/calibration`, `agent_scaffold` (if relevant), `model_id`, `effort`, and `confidence`; expiry and contamination concerns are auditable. No provenance → no specialized prior. A score for one harness/scaffold must not automatically confer a different harness's execution reliability. The existing benchmark floor remains pinned to its specific index/version regardless of specialized records.

## Proposed per-task descriptor contract (v1)

A task carries a *sparse* descriptor. **Omitted is unknown**, not `backend`, `objective`, short context, or a classifier's guessed category. Strings below are a candidate schema, not a model-routing table:

```yaml
# PLAN.md, within ### T1:
domain: ui               # ui | backend | data | infrastructure | mixed
work: implementation     # mechanical | implementation | architecture
modality: browser        # code | visual | browser | multimodal
quality: taste           # objective | taste | mixed
bounds: bounded          # bounded | open
task_size: M             # S | M | L | XL; independent of run risk
estimated_input_tokens: 80000  # optional integer >= 0: per dispatch prompt estimate
estimated_output_tokens: 12000 # optional integer >= 0
```

The frozen JSON representation also holds `descriptor_version`, `provenance` by field (`planner-explicit`, `deterministic-derived`, or `unknown`), and `estimate_confidence` (`low`/`medium`/`high`) for tokens. A planner explicitly decides semantic domain, work, modality, quality and bounds when materially relevant. It must **never** silently classify taste, architecture, risk, expected context, or user preferences from a file extension or a title. The runtime may deterministically derive: `visual: none` versus an explicitly specified visual gate; exact scope/depends; count of supplied files; and *measured* token usage. Derived metadata may suggest questions, never override an explicit planner value. A task that is genuinely mixed stays `mixed`; do not collapse into a model-brand preference.

At `office submit`, validate values, attach to **each task** in the versioned plan and `tasks.descriptor_json`, and include the snapshot in the plan preview's `route_audit`. Dispatch re-routes against fresh quota/trust/cost evidence using that frozen task descriptor, then snapshots the applied descriptor on **each dispatch** for learning. An ordinary plan amendment may edit descriptors for *future* routing; a running/ended episode keeps its original dispatch snapshot. A task with no fields keeps legacy behavior exactly.

### Evidence-only tags

Four optional tags let the planner record what a task was, so later replay can compare outcomes by task kind. They are **evidence-only**: validated and persisted exactly like the fields above (PLAN.md → `tasks.descriptor_json` → plan amendment → per-dispatch `descriptor_json` snapshot), but `dimensions()`, `benchmark_fit()`, `price_tier()` and the `domain` field never read them, so they add no benchmark dimension, price tier or benchmark score adjustment. Known limit: `route_learning.comparability` treats any non-empty descriptor as "present", so a task whose descriptor holds only these tags discounts history without a descriptor by `descriptor_unknown_weight`; no tag value changes a weight. Omitted is unknown and stays absent. An invalid value is refused at plan submit with the allowed list.

| Tag | Values | Meaning |
| --- | --- | --- |
| `evidence_domain` | `frontend`, `backend`, `infra`, `docs`, `tests` | The area of the codebase the task changes. Separate from `domain`, which selects benchmark dimensions. |
| `intent` | `feature`, `fix`, `refactor`, `test-pruning`, `migration` | What the change is for. |
| `difficulty_estimate` | `low`, `medium`, `high`, `very-high`, `unknown` | The planner's estimate of difficulty, recorded so it can be compared with actual outcomes. |
| `brief_shape` | `deliverables-enumerated`, `checks-only`, `unknown` | Whether the brief lists the deliverables or only states checks to satisfy. |

## Benchmark-to-task weighting — evidence before coefficients

1. Translate a descriptor into **dimensions**, not favored model names: UI + taste → appearance/human-eval; browser → visually grounded action; backend → repository repair; architecture → independent structured architecture evaluation (where actually available); mixed → calibrated mixture.
2. Partition correlated evaluations into evidence **families**, e.g. one UI appearance family, one repo-engineering family, one tool-interaction family, one general-reasoning family. Use **at most one version-compatible score per family** (or pre-calibrate a family aggregate on disjoint held-out tasks). Do not double-count near-duplicate public benchmarks.
3. Normalize each score only using a benchmark-specific calibration estimated on held-out Auto Office outcomes, *not* raw rank or a universal score-to-probability conversion. Reject out-of-window, unversioned, contaminated or scaffold-incompatible measurements as specialized evidence; record the reason.
4. Suggested **replayable candidate formula** (not a shippable numeric coefficient): `p_public = sigmoid(logit(p_AA) + lambda * sum_family(weight(task, family) * calibrated_log_odds_delta))`, then Beta posterior with the existing effective `prior_strength`. Bound specialized adjustment and reserve weight for general reasoning where relevant. Set `lambda=0` (existing behavior) until temporal replay, calibration and no-leak checks justify a nonzero value.
5. Maintain `p_local = (k*p_public + comparable_local_successes)/(k+comparable_local_n)`; mature local outcomes naturally dominate. Preference continues only as existing capped soft preference, never disguised as benchmark truth.
6. Reject claims like “Gemini always wins UI” or “GPT always wins backend.” Represent user-supplied task preferences explicitly and separately, identify their source/authority and cap them under current preference policy. The eligible model × effort × harness slate competes on task-scoped evidence and measured costs.

**Quality bar before promotion:** chronological backtest and ablation on held-out Office episodes, grouped by run/task to prevent leakage; calibration curves (Brier/log-loss), change in *accepted task per dollar/hour*, domain slices, safety-gate equivalence, regret and sensitivity to missing/old benchmarks. A new public benchmark may enrich catalog metadata without changing ranking until proven. Never turn a soft specialized score into a role-floor or trust bypass.

## Local learning and pooling

- Preserve route identity: `harness@major × invocation model × effort`. Per-task descriptor snapshots are carried into `derive_outcomes` and grouped into the existing **task-route episode**, keeping existing attribution/labeling exclusions and avoiding duplicate failures for fix rounds.
- Multiply the existing playbook/size/kind/recency/harness weights by **task comparability**, not a new standalone “frontend win rate.” First pool on a broad descriptor **family** (domain, or modality when safely observed), then optionally specialize by work/quality/bounds only when enough independent outcomes span multiple runs.
- Proposed safe starting guardrail: require at least **8 effective comparable episodes spanning 3 runs** before a narrow subfamily gets meaningful additional weight; otherwise use broad route evidence and retain `k` prior shrinkage. Tune these numbers via replay; they are *not* published empirical thresholds.
- A mismatch should **discount**, not delete, evidence. Unknown historical descriptors must remain available as low-specificity pooled evidence; missing values must never fabricate a match. Overfit checks should compare performance at each descriptor depth.
- Do not let descriptor-conditioned evidence immediately mark a route learned-eligible/ineligible or alter trust. The existing mature, route-level eligibility transition remains authoritative unless a separately reviewed safety design changes it.

## Prompt length, token economics and audit

A token estimate applies to the **per-attempt prompt**, including base system/developer instructions, task brief, repo context, retained history, tool overhead and cache effects. Planner may supply a bounded estimate and confidence; runtime can improve this with observed token telemetry. Never infer short context solely from `task_size=S`.

- Price at the model's applicable published *input-prompt* tier using the **estimated total prompt token count**, not output count, while calculating actual input/output/cached tokens using their own applicable rates.
- Represent tiers as provenance-bearing half-open intervals or an explicit `threshold_operator` to avoid off-by-one errors. Example from open #413: Haiku 5.5 `<=100000` vs `>100000` prompt tokens, corresponding to 5x input/output rate difference. The main catalog does **not yet** contain these unmerged rows.
- If the estimate is unknown and a route has materially different price tiers, show `range/unknown` and use the **conservative applicable cost** for qualification against the absolute budget ceiling and for recommendation; never quietly use its cheaper headline tier.
- Where measured token/spend exists, prefer *comparable context-bucket* actual data: a 20k-token episode must not overwrite the 150k-token prior merely because model/effort match. Blend with confidence and expose `prior | local | blended`. Real billing and tokenizer variation are measured, not hard-coded family multipliers.
- Audit fields: descriptor and version/source; chosen benchmark dimension family, score/version/source/normalization and ignored/absent evidence; weighted contribution to prior; local effective n and pooling depth; prompt size estimate/confidence or unknown; tier identifier, price source/date and effective unit prices; cost component and decision hash.

## Four worked examples (illustrative, not model recommendations)

| Task | Descriptor | Evidence mapping | Cost/learning effect |
| --- | --- | --- | --- |
| Build a polished responsive hero from an approved screenshot | `ui, implementation, visual, taste, bounded, M` | Calibrated appearance/human-eval family; separate general-reasoning fallback | UI-comparable local acceptances eventually dominate; no “model X for UI” shortcut |
| Change an API contract and verify integration tests | `backend, implementation, code, objective, bounded, M` | Held-out repo-engineering evidence; code-puzzle score only low-transfer auxiliary evidence | API/domain episode comparability; no visual score just because model is multimodal |
| Redesign a risky cross-module lifecycle | `mixed, architecture, code, mixed, open, XL` | Calibrated architecture review if available; otherwise the existing general reasoning prior; higher uncertainty | Run-level risk and mandatory checks still govern; unknown architecture score is *not* zero |
| Review a >100k-token migration/refactor context | `data, implementation, code, objective, bounded, L; estimated_input_tokens: 160000` | Task-relevant repo engineering where available | Select a documented >100k price tier, disclose effective rates, compare context-similar outcomes; quota/trust/floor unchanged |

## Compatibility and delivery sequence

1. **Research first (this document):** inventory sources, declare uncertainty, record proposals and critical no-double-count/attribution/gating rules.
2. **Additive descriptor transport:** optional PLAN.md keys → validated task snapshot → SQLite nullable JSON migration → task preview/dispatch/audit. A task omitting descriptors remains byte-for-byte semantically equivalent to old routing. Add roundtrip and invalid-value tests.
3. **Economics, independently testable:** accept optional versioned tiered catalog pricing (including #413 once merged), choose by explicit estimated prompt tokens and show unknown/conservative behavior. Add boundary, stale-price and actual-spend-bucket tests.
4. **Public evidence ingestion:** optional `task_benchmarks` records only with provenance; alias resolution and confidence gates. No nonzero adjustment until measured calibration. Tests for unrelated, stale, correlated and unversioned evidence.
5. **Controlled learning/recommendation activation:** enable descriptor comparability behind a policy flag, then calibrated dimension weights only after temporal replay and scorecard review. Record policy/version/ablation artifacts; roll back to baseline without losing source task descriptors.
6. **UX/Audit:** `office inspect route T1 --json` exposes the full reasons, terminal table shows descriptor, specialized evidence and pricing tier. Planner receives a concise documented list of optional fields.

**Migration:** keep old plans and dispatches readable; nullable additive columns, idempotent migrations and no restamping of past evidence. Catalog pricing fields stay backward compatible with flat rates. Leave historical episodes unclassified rather than retrofitting guesses. Reject unknown explicit descriptors at plan submit with field-level guidance; no new gate for missing optional data.

**Tests for final implementation:** parser validation, per-task isolation in one run, plan version/dispatch freeze, old-plan replay unchanged, database migration, hard-gate invariance, descriptor-specific public prior with calibrated pinned fixture, correlated bench dedupe, local sample override and failure-attribution protection, prompt tier at 100000/100001 and unknown estimate, cost override by *comparable* actual history, audit/provenance/decision hash consistency, stable reroute and planner overrides. Run targeted suites and complete routing replay before changing production ranking.

**Decision held open pending actual evidence:** exact taxonomy refinements, calibration function, nonzero benchmark weights, effective-n/pooling thresholds and context-length estimator are not factual constants; measured replay must choose them. Until then, the safe default is zero specialized adjustment with audit-only descriptor collection.
