# Route discovery

Draft for #494: this page describes the agreed policy and CLI contracts. Final default activation, command behavior and live-trial evidence must be reconciled with the integrated implementation and T6 evidence before acceptance. It does not assert that discovery is enabled in the shipped config or that a live trial has passed.

Use this runbook when explaining a discovery decision, diagnosing a probe or trial launch, changing user routing preferences, or disabling discovery. The routing authority is [protocol/routing.md](../protocol/routing.md); [adapter trust](../protocol/adapters.md) and [learning](../protocol/telemetry-learning.md) remain separate authorities for their respective evidence.

## What a route can prove

| Evidence | Meaning | Boundary |
| --- | --- | --- |
| Invocation support / probe pass | This exact invocation and required conformance worked | No quality inference, model-family inference or trust grant |
| Learned effectiveness / eligibility | Comparable accepted task outcomes support task fitness under maturity and replay rules | Does not promote adapter trust or clear quarantine |
| Adapter trust | Baseline or an explicit recorded trust act grants `proven` | Automatic discovery and learning never mint a trust act |

A catalog route becomes probe-able only when explicitly marked discovery-eligible with a concrete invocation model ID and documented or local-evidence provenance. `dispatchable: false` alone does not establish that it can run. Benchmark-only, confirmed-unsupported and archived retired rows stay excluded. Aliases carry the target's availability and reason. Unknown support is distinct from a confirmed unsupported invocation.

Availability states are `available`, `discovered-unconfirmed`, `probe-pending`, `probe-passed`, `temporarily-unavailable` and `confirmed-unsupported`. Availability still leaves role, permission, capability, trust, task and quota gates to check.

## Policy and config

Config precedence is explicit run/prompt > repo > user > shipped defaults. `office config` exposes the effective values and their source tiers; the run pins those values, provenance and policy digest at start. Use the supported config interface rather than creating another preference store.

| Key | Policy contract |
| --- | --- |
| `routing.discovery.enabled` | Switch for automatic builder discovery; final shipped value awaits T6 reconciliation |
| `routing.discovery.roles` | `executor`, `worker` only; other roles are invalid |
| `routing.discovery.max_probes_per_run` | 2 new probes |
| `routing.discovery.max_trials_per_run` | 1 live builder trial |
| `routing.discovery.max_trial_percent_rolling_20` | 15% of the rolling last 20 builder decisions |
| `routing.discovery.trial_size_classes` | `S`, `M` |
| `routing.discovery.trial_blast_radius` | `local`, `repo` |
| `routing.discovery.require_known_fallback` | `true` |
| `routing.discovery.probe_ttl_days` | Cache expiry; consult effective config for its value |
| `routing.discovery.probe_timeout_s` | Bounded probe lifetime; consult effective config for its value |
| `routing.user_policy.denied_models` | User-only hard denial; shipped list empty |
| `routing.user_policy.overkill_rules` | User-only automatic-selection preference, optionally scoped by role/size; shipped list empty |
| `routing.adaptive.budget_ceiling_usd` | Hard ceiling only when explicitly supplied at run, repo or user tier; `null` disables |

Denial entries accept `harness/model[@effort]`, `model` or `harness:<name>`. Overkill entries use `{route, roles?, size_classes?}`. A denial blocks automatic routing, probing and convenience manual paths, including plan `route:`, `--as`, `--review-as`, `amend route` and `--route`. Only an explicit user policy change re-enables the route. An overkill rule excludes only matching automatic selections; explicit manual selection and automatic selection outside its scope remain valid, subject to factual gates. The system never invents overkill from cost, age or missing evidence.

Without a genuine user ceiling, expected cost to success ranks routes. A shipped economic scale cannot act as a cost blacklist for provenance-aware runs. A hard ceiling discloses its run/repo/user source. Unsupported invocation, unsafe or invalid adapters, quarantine, missing required capability or isolation, protected quota exhaustion and documented safety/permission violations still block execution.

## Probe before a trial

The exact probe fingerprint includes **harness, full harness version, adapter hash, launch profile, invocation model ID and effort**. This is stricter than the major-version trust/history identity. Changing any field or reaching `probe_ttl_days` makes the cache stale. A pass for one effort or profile cannot qualify another.

`office doctor --probe-route <harness/model@effort>` requests a bounded manual conformance probe. A probe may invoke the provider; run it only within the task's authorization. When bound to a run it counts against that run's probe cap. An unbound probe still records manual provenance, effective policy and exact fingerprint, with absent run/plan/task/dispatch/primary/fallback fields explicitly null. It grants no route override or trust.

The probe checks model identity readback and a well-formed response using the worker's launch profile. A write-capable profile also proves a permitted write in a disposable isolated git worktree and no write outside it. It installs nothing and escalates no permission.

| Failure class | Interpretation |
| --- | --- |
| `unsupported-model-effort` | This exact model/effort invocation is unsupported |
| `auth-quota-blocked` | Authentication, provider/account or quota prevents invocation |
| `transient` | Temporary transport/environment failure |
| `isolation-missing` | Required permission or worktree isolation is missing |
| `conformance-failed` | Identity, response or required behavior failed conformance |

A failed probe never becomes a live trial. Failure is exact-route scoped: other efforts and harnesses remain independently routable. Reserve a probe slot atomically before launching; concurrent callers for the same fingerprint share one launch/result. Failed, abandoned and expired reservations consume the run cap. Expiring a dead or timed-out reservation records the expiry and never launches a replacement probe as cleanup.

## Bounded assignment and recovery

Only automatic `executor`/`worker` selection can allocate a trial. The task must be bounded, reversible S/M work with local/repo blast radius in an isolated worktree, with safe permissions, normal submission, tests, independent review and verification. Production, production-data, irreversible, high-risk and protected-write tasks are excluded. Planner, plan reviewer, code reviewer, integration/visual/browser/closeout verifier and final gate roles keep their stricter rules and never trial.

An untried candidate must pass every non-probe gate and the bounded exploration allocation (seeded draw, margin and relative cost bounds). A recorded known-working fallback must be `proven` or `learned-eligible` and currently qualifying; another trial or unprobed route cannot be the fallback. Probe intent keeps that fallback as primary. After a fresh exact pass, routing rechecks current permissions, capabilities, quota, risk, caps and fallback for that same candidate without drawing a different untried route.

Trial reservation and dispatch creation commit atomically after cap rechecks. The caps are 2 probes/run, 1 trial/run and 15% of the rolling last 20 builder decisions; allocation is not a license to prefer every untried route. A failed/refused probe or failed recheck takes the recorded known-working fallback without creating a trial session or lease.

If trial launch fails before meaningful work, record the precise failure and confirm the entire failed worker process tree has exited before releasing its lease/session and starting the recorded fallback under existing authority. Unconfirmed exit blocks replacement. Preserve work that already started and report it for explicit recovery. Never run a second live writer. Explicit manual pins (`--as`, `--review-as`, `amend route`, `--route`, declared or recorded routes) skip discovery and never switch silently.

## Inspect and audit

`office inspect route <task>` explains user-denied, scoped user-overkill, unsupported, untried, pending/passed/failed probe states, preference source, allocation/cap state and recovered launches. `--json` carries the complete decision and per-attempt discovery evidence. `office inspect learner` shows learned task outcomes separately from invocation and trust evidence.

Each attempt receives an `attempt_id` before a dispatch exists. Immutable `route_discovery_events` record every probe reservation, pass/failure, cache reuse, refusal, timeout/abandonment/expiry and every trial lifecycle step. Events carry policy digest/version, exact fingerprint, reason, freshness, allocation snapshot, primary/fallback, outcome and available run/plan/task/dispatch identity. A cached reuse links its producing attempt with `source_attempt_id`; dispatch linking appends an event. Later probes and policy changes append new evidence rather than rewriting old attempts. The probe cache (`route_probes`) and current trial projection (`route_trials`) are mutable and are not the evidence ledger.

An accepted trial revision teaches effectiveness through the existing attribution, maturity and held-out replay rules. Launch/harness/adapter/environment/quota/brief failures are attributed to their actual cause rather than the model. Discovery never writes `adapter_trust_acts`, grants `proven` or clears quarantine.

## Rollback and pinned runs

Set `routing.discovery.enabled: false` at the user or repo tier through the supported config interface to disable automatic discovery for future runs, subject to higher-tier settings. Read back the effective value and source with `office config`. This leaves stored evidence and trust state intact.

Existing runs keep their pinned config. If the user authorizes a live-run routing change, `office config --run <id> --apply-routing --quote "<words>"` explicitly re-pins it; inspect the resulting policy before further dispatch. A config file edit alone does not change a live run or stop a live worker.

Pre-change runs without discovery provenance retain discovery-off behavior and the old hard ceiling (25 unless their pinned config says otherwise). Their 3.0 and v3.1 paths and stored decisions remain readable and replayable. Discovery-off decisions preserve the prior decision hashes. Rolling back discovery does not rewrite history, resurrect retired models, or waive existing safety gates.
