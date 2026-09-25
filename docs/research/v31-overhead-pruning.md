# v3.1 research: overhead, duplicated authority, and safe pruning

Research for [Overhead baseline and safe pruning inventory](https://github.com/Hikari9/auto-office/issues/154), under [the v3.1 map](https://github.com/Hikari9/auto-office/issues/152).

**Inspected baseline:** `93167420271527f1220700f2eebdff1ac2d1115c`, the main-branch tip observed on September 25, 2026. All repository links below are pinned to that commit.

**Authority:** [the ratified charter](https://github.com/Hikari9/auto-office/issues/153#issuecomment-5832359821), especially orchestrator-first overhead reduction. Recommendations here do not ratify additional product choices or implement changes.

## Scope and evidence limits

The maintainer requested that research requiring local historical data be skipped. No local run database, personal configuration, transcript, historical token usage, cost record, or timing trace was accessed. Historical cost/latency baselines and numerical savings are therefore **SKIPPED**, not zero and not inferred from file size.

Work completed: pinned repository/source inspection, command and artifact inventory, comparison with the approved CLI specification, inspection of relevant test declarations, and static measurements from the returned Git objects and CLI parser. No authenticated research-subagent runner was available. The repository could not be cloned into this execution environment, so its test suite was not run. Existing tests are identified as coverage to retain or extend, not newly passing results.

## Finding in one sentence

**Reuse the useful primitives, but replace agent-coordinated plumbing with one runtime-owned path per meaningful transition.** Merely renaming 56 helper commands does not remove the orchestrator's bookkeeping.

## What is actually present

| Static observation | Evidence | Interpretation |
|---|---|---|
| 56 top-level helper verbs in the runtime parser | `main()` in [office_runtime.py][runtime] | Interface size, not 56 calls per run and not a token count. |
| Root `SKILL.md`: 16,271 bytes | [Root skill][skill], Git content metadata | Shipped instruction size, not observed loaded context. |
| `office_runtime.py`: 107,960 bytes | [Runtime][runtime], Git content metadata | Implementation size; not agent overhead by itself. |
| Four seed adapters: agy, claude, codex, hermes | [Seed directory][adapters] | All declare valid-unverified/pending conformance. Local promotions/bindings were not inspected. No native Gemini seed exists in this directory. |
| Global `office` package/API is specified, not present as the specified package layout | [CLI architecture][cli], [root tree][root] | Root has neither `pyproject.toml` nor `src/office/`; do not report the approved architecture as shipped. |
| Some recording is already automatic | `cmd_start`, `record_run` in [runtime][runtime]; `run_gate` in [verify][verify]; `write_outcome_label` in [review loop][review] | The problem is inconsistent wiring and duplicated paths, not that every record is manually written. |

The 56 verbs were enumerated from the complete parser and counted mechanically. No tokenizer proxy was used.

## Keep / consolidate / hide / retire inventory

This is a source-backed proposed disposition, not authorization to remove useful capabilities. Tests named below exist in the [test tree][tests]; execution remains outstanding.

| Surface and present responsibility | Proposed disposition and future owner | Coverage / migration boundary |
|---|---|---|
| Root skill and lifecycle spokes tell the orchestrator how to coordinate helpers | **Consolidate/hide:** one small entry brief plus just-in-time role/phase instructions, emitted by runtime | Preserve authority and scope rules; replace instruction-text assertions with behavioral conformance where possible. `test_v3_instruction_contract.py`. |
| `check-spoke`, `spoke-digest`, `mark-spoke` and shortcut check/mark | **Retire from normal workflow:** runtime records which instruction version it delivered | `test_spoke_receipt_integrity.py` must be revised intentionally. Delivery is not comprehension. Debug inspection may remain. |
| `new-run` manually accepts identity/hash fields; `start` derives them | **Consolidate:** normal entry uses one start operation; low-level constructor private/debug | Preserve snapshot pinning and restart compatibility in `test_runtime.py`, `test_schemas.py`. Do not retain two normal initialization paths. |
| Packet constructor/validation/invalidation in `office_packets.py` | **Keep/hide:** runtime constructs envelopes from task + selected route + state | Existing replacement and scoped-invalidation semantics are valuable. `test_amendments.py`, `test_schemas.py`. No agent-authored identity boilerplate. |
| `state.json`, `family.json`, projected registry and multiple phase/version writers | **Consolidate authority:** one application service updates canonical state and projections | Family lock covers cooperating writes but not a transaction across all files. `test_families.py`, `test_amendments.py`; reconcile [state-drift issue](https://github.com/Hikari9/auto-office/issues/133). |
| `increment-plan` versus `amend` | **Retire independent mutation path after compatibility handling:** one semantic amendment operation | `increment-plan` changes run state, not family state. Review loop still calls it. Preserve old pinned runs; no live-history rewrite. |
| Family amendment records and version ownership | **Keep/extend internally:** automatic delta construction, durable delivery and application acknowledgement | Current `apply_amendment` persists records/paused scopes but does not deliver a worker message. Do not claim notification from its docstring. |
| Run discovery in `office_shortcut.sh` | **Replace with approved global discovery:** no copied per-repository controller | Current shortcut chooses latest-mtime `.ref`; approved CLI specifies binding/sole-run resolution and ambiguity errors. [Shortcut][shortcut], [CLI architecture][cli]. |
| Spawn identity flags, start receipt, pane ledger, process result files | **Consolidate:** dispatch service derives mandatory provenance; adapter only owns invocation mechanics | Seven optional metadata flags currently jointly control receipt creation. Remove normal paths that spawn first and omit receipts. `test_adapters.py`, `test_monitor.py`, `test_dogfood.py`. |
| Monitor status samples, event IDs, cursors and acknowledgements | **Keep/hide/harden:** runtime tracks delivery; orchestrator sees compact state changes | Current read-check-append and cursor read-modify-write need concurrency protection. Keep raw liveness separate from accepted work. `test_monitor.py`; [delivery issue](https://github.com/Hikari9/auto-office/issues/125). |
| Shell review loop plus externally supplied review file / FIX_COMMAND | **Consolidate behind submit:** runtime drives jobs, local findings and retries | Current loop validates a supplied result; it does not dispatch its own fresh reviewer each round. Preserve independent identity/scope checks. `test_review.py`. |
| `verify.sh` hardcoded project commands and empty runtime/browser gates | **Replace internals, retain public behavior where correct:** repository verification contract + applicable gate runner | Preserve no-command != pass. `test_dogfood.py`, `test_review.py`. Required-but-unavailable must be distinct from not applicable. |
| `gate.log`, validation JSON files and recorder | **Keep evidence, hide storage:** capture immutable per-attempt output once; derive receipt and summary | `gate.log` is overwritten per check, and kind/dispatch input JSON is overwritten on rerun. Hashes without retained source output are weak audit artifacts. |
| Landing/checkpoint persistence | **Keep/hide; clarify semantics:** durability save is not accepted checkpoint | `save_checkpoint` validates/writes a snapshot; acceptance needs current gate results. `record_landing` checks a recorder row but not the whole convergence contract. `test_landings.py`. |
| `record-review`, shell review head checks, `verify-landing` | **Consolidate validation:** one target-aware acceptance evaluator reused by all entrypoints | Shell checks HEAD; dirty work can differ without HEAD changing. `verify-landing` uses plugin ROOT. Avoid accepting a different target/tree through a weaker path. |
| Routing, preferences, trust acts, quota and config merging | **Keep/hide:** runtime resolves and records; orchestrator supplies strategic intent | Preserve explicit reviewer preference and derived trust. No automatic promotion. Trust-act CLI is absent from parser despite function-level machinery; reconcile [trust path issue](https://github.com/Hikari9/auto-office/issues/134). |
| Six gear presets and partly interpreted funding strings | **Hide normal knobs; consolidate concrete gate resolution** | `resolve_gates` resolves risk_forced but leaves acceptance_forced/shallow/cost_bounded/policy_optional as strings. Do not bool-cast them or ask agents to interpret each. Retiring presets needs [mode decision](https://github.com/Hikari9/auto-office/issues/128). |
| Catalog refresh, reward/maturity/replay and proposal machinery | **Keep useful capability, hide routine mechanics; audit missing consumers individually** | Do not declare every setting dead merely because it has no direct Python read; several are advisory. Respect [learning scope decision](https://github.com/Hikari9/auto-office/issues/127) and [refresh work](https://github.com/Hikari9/auto-office/issues/130). |
| Privacy lint, attribution, protected paths, leases and merge authority | **Keep:** hard boundaries are not prompt bloat to delete | Move mechanical enforcement into normal semantic operations. Preserve `test_trust_conformance.py`, `test_worktree.py`, `test_propose.py` and schema rejection cases. |

### Specific corrections to inherited assumptions

`cmd_start` already initializes the configured recorder and records a run. It also silently catches family-registration failure after writing run state. Research must describe both facts; an earlier report of missing telemetry is not proof that the current start path records nothing. [Source][runtime]

`FAMILY_PHASES` now aliases the runtime phase order, but `increment-plan` and family amendments still write different state stores. Shared enum names alone do not settle transactional consistency. [Family module][family], [runtime][runtime]

The approved global CLI spec is a foundation, not a reason to preserve manual spoke marking forever: the new charter expressly replaces that normal ritual. Likewise, historical tests that encode the old plan-review timing must change intentionally, not be treated as immutable product requirements.

## Orchestrator-first responsibility boundary

The orchestrator should supply task boundaries, dependency/stacking intent, routing overrides and meaningful amendments. It should receive concise exceptions and strategic status. It should not allocate event sequence numbers, copy IDs/hashes, assemble gate packets, translate normal reviewer findings, query SQL, retype state directories, acknowledge every heartbeat, or call record-* after every action.

The deterministic runtime should record each operation as part of executing it, provide bounded status/inbox output, route normal findings directly to the owning executor, and rebuild projections on resume. A cheap extra LLM is not the default replacement for bookkeeping ordinary code can perform.

The largest first implementation seam is therefore an **application-service boundary**, not a larger prompt template. `start`, dispatch, submit, amend, and close should each own their mechanical consequences. Exact command names/output remain the [CLI prototype's decision](https://github.com/Hikari9/auto-office/issues/159).

## Operational evidence versus optional analytics

| Data class | Required behavior |
|---|---|
| User authority, ownership, active versions, reviewed revision, required gate result and evidence references | Missing or inconsistent data blocks the dependent acceptance/action; never fabricate success. |
| Dispatch lifecycle, actual command output, amendment delivery/application state | Persist automatically; recover pending work after interruption. A failed database/projection update is a recoverable explicit state, not a chore handed to the orchestrator. |
| Optional cost/quality aggregates, dashboards, deferred learning and refresh statistics | Fail softly with a compact recorded diagnostic. Do not interrupt normal strategy for noncritical analytics. |
| Upward trust changes, overrides and policy adoption | Preserve attributed authorization. Automation of storage is not automation of permission. |

Use one real execution record to derive multiple views; do not create parallel competing ledgers. New storage choices remain a detailed-design decision.

## Baseline and acceptance method, without local history

Available now: exact source/ref, helper count, byte sizes, visible call graph, configuration readers, and test seams. Not available: actual prompt loads, model turns, quota burn, task timing, retries, historical success rates, or cost savings.

Future prospective evaluation can avoid dependence on past transcripts. Run matched, authorized synthetic tasks on pinned v3 and candidate v3.1 with the same fixtures and qualifying model policy. Include a simple code change, relevant UI change, failing check, invalid capture, ordinary amendment, plan defect, process interruption and concurrent completion. Collect usage automatically; distinguish measured tokens/cost from estimates and unknown values. Do not add an orchestrator data-entry step to measure its own simplification.

Report separately: orchestrator tokens/turns/tool calls and telemetry-only turns; executor paperwork; reviewer/capture/escalation usage; time to first dispatch; total time and cost to accepted work; and recovery/evidence correctness. Compare repeated trials rather than one anecdote. Do not declare success merely because the entry skill shrank. The user's local historical baseline remains skipped in this research and must not become a hidden blocker to the CLI design.

## Handoff and unresolved product choices

Research is sufficient to begin the [minimal CLI prototype](https://github.com/Hikari9/auto-office/issues/159) and [rolling-gate semantics](https://github.com/Hikari9/auto-office/issues/160). The final [pruning/migration ratification](https://github.com/Hikari9/auto-office/issues/157) must choose exact removals, compatibility duration, and measurable prospective targets. No new threshold, preset retirement, or feature deletion is ratified here.

Preserve the current 20% balanced-money band. The charter's quota reserve change to 5% must reach its readers, including family resource-demand defaults, without a global text replacement of unrelated 20s. Active pinned policy is not changed during research. [Config][config], [family demand helpers][family]

## Reproducible source index

[runtime]: https://github.com/Hikari9/auto-office/blob/93167420271527f1220700f2eebdff1ac2d1115c/scripts/office_runtime.py
[family]: https://github.com/Hikari9/auto-office/blob/93167420271527f1220700f2eebdff1ac2d1115c/scripts/office_family.py
[verify]: https://github.com/Hikari9/auto-office/blob/93167420271527f1220700f2eebdff1ac2d1115c/scripts/verify.sh
[review]: https://github.com/Hikari9/auto-office/blob/93167420271527f1220700f2eebdff1ac2d1115c/scripts/review_loop.sh
[shortcut]: https://github.com/Hikari9/auto-office/blob/93167420271527f1220700f2eebdff1ac2d1115c/scripts/office_shortcut.sh
[skill]: https://github.com/Hikari9/auto-office/blob/93167420271527f1220700f2eebdff1ac2d1115c/SKILL.md
[adapters]: https://github.com/Hikari9/auto-office/tree/93167420271527f1220700f2eebdff1ac2d1115c/adapters/seed
[config]: https://github.com/Hikari9/auto-office/blob/93167420271527f1220700f2eebdff1ac2d1115c/config/config.default.yaml
[cli]: https://github.com/Hikari9/auto-office/blob/93167420271527f1220700f2eebdff1ac2d1115c/docs/office-cli-architecture.md
[root]: https://github.com/Hikari9/auto-office/tree/93167420271527f1220700f2eebdff1ac2d1115c
[tests]: https://github.com/Hikari9/auto-office/tree/93167420271527f1220700f2eebdff1ac2d1115c/tests
