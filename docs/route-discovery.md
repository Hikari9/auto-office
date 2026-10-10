# Route discovery

Discovery (#494) lets one explicitly discovery-eligible, unverified executor or worker route be proven by one exact conformance probe and then tried once, bounded, with a known-working fallback. This page is the operating runbook. Routing authority is [protocol/routing.md](../protocol/routing.md); [adapter trust](../protocol/adapters.md) and [learning](../protocol/telemetry-learning.md) stay separate authorities for their own evidence. The tests, replay and live run behind the statements below are in [docs/benchmarks/issue-494-evidence.md](benchmarks/issue-494-evidence.md).

## Shipped state

- `routing.discovery.enabled` is **`true`** in the shipped config, for `executor` and `worker` only. A run pins its policy at `office start`, so only runs started after this release discover.
- Runs pinned before #494, `v3.1` review-contract runs and 3.0 legacy runs keep discovery off and never discover. A config without the key also reads as off.
- Replay of the maintainer's recorded history (696 executor/worker decisions, 27 runs, all pinned before discovery) found discovery-off decisions identical to the recorded hashes. With discovery on, none of the 64 recorded dispatch decisions would have drawn a probe: most tasks failed the risk gate, and the rest were stopped by the margin bound or the cold rolling window.
- Live evidence is partial. The first live attempt (T6 evidence, "Live evidence") passed one real exact probe (`codex/gpt-6.1-sol@medium`, 14 s) and ran a real trial in an isolated Office, but that trial row ended **`abandoned`**, not accepted. A headless launch (no Herdr pane) did not write the dispatch's `agent.env`, which the brief and `office preflight` tell the executor to source before `office submit`, so the executor could not submit. The operator wrote `agent.env` and submitted by hand, and Office's normal review and acceptance took it from there. So that attempt is not a clean automatic trial from launch to `trial-accepted`. That path is covered by the scripted scenario suite only, as are a failing probe and the fallback launch. The headless `agent.env` fix and a second isolated live trial are owned by task T8 of the #494 run, and their outcome is recorded in the evidence doc, which holds the full history of both attempts.
- Probing is limited today. A real probe needs an OS-enforced write boundary, which is implemented only with macOS `sandbox-exec`, and an authoritative model and effort readback, which only the `codex` adapter exposes. Other platforms and the `claude` and `agy` eligible rows refuse with `no-write-boundary` or `no-identity-readback` and route as before.
- Nothing here promotes trust, trials reviewers or planners, or removes a route for cost.

## What a route can prove

| Evidence | Meaning | Boundary |
| --- | --- | --- |
| Invocation support / probe pass | This exact invocation and required conformance worked | No quality inference, model-family inference or trust grant |
| Learned effectiveness / eligibility | Comparable accepted task outcomes support task fitness under maturity and replay rules | Does not promote adapter trust or clear quarantine |
| Adapter trust | Baseline or an explicit recorded trust act grants `proven` | Automatic discovery and learning never mint a trust act |

A catalog route becomes probe-able only when the catalog marks it `discovery: eligible` with a concrete invocation model ID and a documented or local-evidence reason. `dispatchable: false` alone does not establish that it can run. Benchmark-only, confirmed-unsupported and archived retired rows stay excluded. Aliases carry the target's availability and reason. Unknown support is distinct from a confirmed unsupported invocation.

Availability states are `available`, `discovered-unconfirmed`, `probe-pending`, `probe-passed`, `temporarily-unavailable` and `confirmed-unsupported`. Availability still leaves role, permission, capability, trust, task and quota gates to check.

## Policy and config

Config precedence is explicit run/prompt > repo > user > shipped defaults. The run pins the effective values, their provenance and a policy digest at start. Read and set keys with `office config` (`office config <key>`, `--show-origin` for the source tier, `--repo` for the repo file, `--edit` for list values such as the denial list). Do not create another preference store.

| Key | Shipped value and meaning |
| --- | --- |
| `routing.discovery.enabled` | `true`. Switch for automatic builder discovery |
| `routing.discovery.roles` | `[executor, worker]`; no other role is valid |
| `routing.discovery.max_probes_per_run` | 2 |
| `routing.discovery.max_trials_per_run` | 1 |
| `routing.discovery.max_trial_percent_rolling_20` | 15 percent of the last 20 builder dispatch decisions across all runs |
| `routing.discovery.trial_size_classes` | `[S, M]` |
| `routing.discovery.trial_blast_radius` | `[local, repo]` |
| `routing.discovery.require_known_fallback` | `true` |
| `routing.discovery.probe_ttl_days` | 7 |
| `routing.discovery.probe_timeout_s` | 120 |
| `routing.user_policy.denied_models` | `[]`. User-only hard denial |
| `routing.user_policy.overkill_rules` | `[]`. User-only automatic-selection preference, optionally scoped by role and size |
| `routing.adaptive.budget_ceiling_usd` | `null`. A hard ceiling only when a user, repo or run supplies it |

Denial entries accept `harness/model[@effort]`, `model` or `harness:<name>`. Overkill entries use `{route, roles?, size_classes?}`.

**User-denied is hard.** A denied route is blocked from automatic routing, probing (including `office doctor --probe-route`), plan `route:`, `--as`, `--review-as`, `amend route` and `--route`. Nothing is reserved or launched. A denial written after `office start` still stops a probe, because probing reads the live user and repo policy. Only an explicit user policy change re-enables the route.

**Overkill is automatic-only and scoped.** A rule removes a route only from automatic selections that match its role and size. Manual selection and automatic selection outside the scope stay valid, subject to the factual gates. The system never invents overkill from cost, age, benchmark absence or missing verification.

**Ceiling provenance.** Without a genuine user ceiling, expected cost to success ranks routes and removes none. The shipped `cost_scale_usd` ranks only and is never a ceiling. A hard ceiling discloses its tier (`run`, `repo` or `user`) in the decision and in inspect. A run pinned before #494 keeps the ceiling its own config carried (25 unless it set another). Unsupported invocation, unsafe or invalid adapters, quarantine, missing capability or isolation, protected quota exhaustion and documented safety or permission violations still block execution.

## Probe before a trial

The exact probe fingerprint is **harness, full harness version, adapter hash, launch profile (`worker`), invocation model ID and effort**. This is stricter than the major-version trust and history identity. A change to any field is a new key, so a pass for one effort, model, harness version, adapter or profile never qualifies another.

`office doctor --probe-route <harness/model@effort>` runs one bounded manual probe. It uses provider quota, so run it only within the task's authorization. It works with discovery off, but a denied route is still refused. With a run named explicitly (`--run <id>` or `OFFICE_RUN_ID` set) it counts against that run's probe cap and records its plan version. Otherwise it is unbound: manual provenance, effective policy and fingerprint are recorded and the run, plan, task and dispatch fields are null. Exit code 0 means pass. A probe grants no override and no trust.

The probe checks model and effort readback from the harness's own output and a well-formed reply under the worker launch profile. It also proves a permitted write in a disposable isolated git worktree and no write outside it, with an OS-enforced boundary. It installs nothing and escalates no permission.

| Failure class | Interpretation | Cached for |
| --- | --- | --- |
| `unsupported-model-effort` | This exact invocation is unsupported (`confirmed-unsupported`) | Until the fingerprint changes |
| `conformance-failed` | Identity, response or required behavior failed (`confirmed-unsupported`) | `probe_ttl_days` |
| `isolation-missing` | Required permission or worktree isolation is missing | `probe_ttl_days` |
| `transient` | Temporary transport or environment failure (`temporarily-unavailable`) | At most 1 hour |
| `auth-quota-blocked` | Authentication, account or quota prevents invocation (`temporarily-unavailable`) | At most 1 hour |

A pass is cached for `probe_ttl_days`. A pending reservation is fresh for `probe_timeout_s` plus a 30 s grace, then expires. A failed probe never becomes a trial. Failure is exact-route scoped: other efforts and harnesses stay routable. Probe slots are reserved atomically before launch, concurrent callers for one fingerprint share one launch, and failed, abandoned and expired reservations still count against the run cap. Expiring a dead reservation records the expiry and never launches a replacement as cleanup.

A probe that is not allocated records `probe-refused` with one of these reasons: `route-denied`, `policy-unreadable`, `archived`, `unknown-route`, `not-eligible`, `already-available`, `no-profile`, `not-installed`, `no-fingerprint`, `no-identity-readback`, `no-write-boundary`, `discovery-disabled`, `role-not-eligible`, `quota-reserve`, `probe-in-flight`, `probe-cap`, `db-busy`. Each leaves the known-working route in place.

## Bounded assignment and recovery

Only automatic `executor`/`worker` selection can allocate a trial. The task must be bounded, reversible S/M work with local or repo blast radius in an isolated worktree, with safe permissions, normal submission, tests, independent review and verification. Production, production-data, irreversible, high-risk and protected-write tasks are excluded. Planner, plan reviewer, code reviewer, integration, visual, browser and closeout verifier, and final gate roles keep their stricter rules and never take a trial.

Discovery draws only when ordinary exploration is not active in the same decision. An untried candidate must pass every non-probe gate and the allocation bounds: a seeded draw at the rolling rate, then the margin and relative-cost bounds. The first decision can only choose a probe candidate (`intent: probe`) and keeps the known-working route as primary. After a fresh exact pass, the preflight recompute rechecks permissions, capabilities, quota, risk, caps and fallback for that same candidate and may then make it the trial primary. It never draws a different untried route.

The recorded fallback must be the top-ranked qualifying route, `available`, not quarantined, and either `proven` or `learned-eligible`, and it must differ from the trial route. Another trial or an unprobed route cannot be the fallback. Without one the decision is blocked `no-fallback`.

Caps are 2 probes per run, 1 trial per run and 15 percent of the last 20 builder decisions. The rolling window counts the decision being made, so a cold history allows no trial until six builder decisions are recorded (inspect shows `warming up`). The trial reservation and the dispatch row commit in one transaction after the caps are rechecked, so two concurrent dispatches cannot both take the last rolling slot. Block tokens in inspect include `risk`, `trial-cap`, `rolling-cap`, `no-fallback`, `exploration-active`, `probe-cap`, `margin`, `cost`, `ceiling`, `probe-stale`, `quarantined` and `quota-reserve`. A failed or refused probe or a failed recheck takes the known-working fallback without a trial session or lease.

If a trial launch fails before meaningful work, Office records the precise failure and confirms the failed worker's entire process tree has exited before releasing its lease and session and starting the recorded fallback under existing authority. Unconfirmed exit blocks replacement. Work that already started is preserved and reported for explicit recovery, and Office never runs a second live writer. A trial whose worker ends without a submission and without a recovery is recorded `abandoned`. Explicit manual pins (`--as`, `--review-as`, `amend route`, `--route`, declared or recorded routes) skip discovery and never switch silently.

## Inspect and audit

`office inspect route <task>` shows, per decision, the discovery intent, primary, fallback, probe state and freshness, the three caps, the preference source tiers (denied, overkill, budget ceiling) and candidates by category: `denied`, `overkill`, `unsupported`, `untried`, `probe-candidate`, `probe-pending`, `probe-passed`, `probe-failed` and `trial-eligible`. It then lists `trials:` (route, fallback, status, outcome, and whether the fallback ran after a recovered launch) and `attempts (route_discovery_events):` with each attempt's events in order. `--json` adds `audits` (the full decision disclosure), `trials`, `attempts`, `effective_route` and `route_changes`. `office inspect learner` shows learned task outcomes and, separately, trial evidence with the route's adapter trust beside it.

Each attempt gets an `attempt_id` before a dispatch exists. Immutable `route_discovery_events` record every probe reservation, result, cache reuse, refusal, expiry and abandonment and every trial lifecycle step, with policy digest and version, exact fingerprint, reason, freshness, allocation snapshot, primary and fallback, outcome and the available run, plan, task and dispatch identity. A cache reuse links its producing attempt with `source_attempt_id`, and dispatch linking appends an event. Later probes and policy changes append evidence and never rewrite earlier attempts. The probe cache (`route_probes`) and current trial projection (`route_trials`) are mutable and are not the evidence ledger.

An accepted trial revision teaches effectiveness through the existing attribution, maturity and held-out replay rules. A single accepted trial does not grant learned eligibility. Launch, harness, adapter, environment, quota and brief failures keep their actual cause and are not charged to the model. A trial row that ended `abandoned` records no `trial-accepted` event even if someone else later submits and the task is accepted. The learner still reads that dispatch as landed. Discovery never writes `adapter_trust_acts`, grants `proven` or clears quarantine.

## Rollback and pinned runs

Set `routing.discovery.enabled: false` at the user tier (`office config routing.discovery.enabled false`) or the repo tier (add `--repo`) to disable discovery for future runs. A repo-tier value wins over the user tier, so read back the effective value and its source with `office config routing.discovery.enabled --show-origin`. This leaves stored evidence and trust state intact and needs no code change.

Existing runs keep their pinned config. If the user authorizes a live-run routing change, `office config --run <id> --apply-routing --quote "<words>"` re-pins it from the current config files. Inspect the result before further dispatch. A config edit alone does not change a live run or stop a live worker.

Pre-#494 runs retain discovery-off behavior and their old ceiling. Their 3.0 and v3.1 paths and stored decisions stay readable and replayable, and discovery-off decisions preserve their prior hashes. Rolling back discovery does not rewrite history, resurrect retired models or waive a safety gate.
