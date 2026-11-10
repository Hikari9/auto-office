# Changelog

One line or short list per version, newest first. The version itself lives only in `src/office/_version.py` (see `AGENTS.md`).

## 3.15.62

`office route policy --effective` names each route's per-size states when they differ from the unsized verdict (#1099). A user role rule written after a run pinned its policy reaches that run's next routing decision, and `office route allow|neutral|deny` says so (#1100). `office route clear` says a run that pinned the rule keeps applying it until `office config --run <id> --apply-routing` re-pins it. The pi registration test reads status for the run it started, not a bare status (#1107).

## 3.15.61

Reviewer re-prompt counts a written reply file or an agent reported working as submitted rather than stopping on held scrollback (#1106 H4).

## 3.15.60

A reused route that a user deny now rejects is dropped and the rerun re-picks from the approved plan (recorded as a route change); `amend route --auto` clears a stale reroute on the parent dispatch; status shows the route the next launch takes from the same selection the rerun uses, and a planned route that will not run is shown with its rejection reason, never as `(planned)` (#1106 H3).

## 3.15.59

A lane visual review now needs one OBSERVATION line per screenshot (no bare APPROVED), and the reviewer infers which UI sections must be shown from the acceptance and plan, reporting uncovered ones as findings (#1078).

## 3.15.58

Orchestrator guidance: walk a UI lane yourself while its convergence and visual reviews run, then route your findings with the reviewers' (#1077).

## 3.15.57

Executors on a harness whose adapter sets subagents: false (opencode) are told to do their task and self-review in their own session, instead of starting a subagent that can hang (#1074).

## 3.15.56

A free-tier route that hits its usage limit again the same day, with no known reset time, now gets a doubling cooldown (capped at local midnight) instead of being re-picked every half hour (#860).

## 3.15.55

The self-review revert proof now finds the pytest command in checks like 'uv run --with pytest pytest ...' instead of taking --with's package value as the runner (#1072).

## 3.15.54

The convergence review brief now tells reviewers that a check failing only because their read-only checkout has no provisioned environment (.venv, packages) is not a blocking finding against the work (#1044).

## 3.15.53

In a v3.1 run, a superseded plan-review round's late result no longer queues a review for a revision the orchestrator vetoed with --no-review (#962).

## 3.15.52

A just-queued self-review subagent gate is no longer reported as a stall: its lane's convergence review job owns it (#961).

## 3.15.51

A lane convergence review whose job failed or ended without settling it is now settled UNAVAILABLE, so office resume and the fallback can run it again instead of it staying queued (#1022).

## 3.15.50

A failed scripted visual step (a click that timed out, a state that never appeared) now reaches the lane's vision reviewer as evidence with a screenshot instead of an automatic RECHECK (#384, step 1).

## 3.15.49

- test: retire the exhaustive model-roster and duplicate tests, with a traceability record in `docs/test-retirement.md` and an inventory check (#871 PR C, #1037).
- chore(ecosystem): raise the hub (247 -> 250) and auto-adapter (53 -> 55) skill line budgets to what main ships; the trim is tracked in #1025.

## 3.15.48

Credit-limited routes can carry an opt-in daily dispatch budget (quota.daily_dispatch_budget); at the budget the route reads as quota-exhausted so reviewer chains fall through, with a warning at 80% (#831).

## 3.15.47

A reviewer the orchestrator revokes is no longer recorded as a route failure, and no fallback reviewer is started for it (#1007).

## 3.15.46

office dispatch and inspect warn when a --review-as pin has no effect because the run's gear runs no code review (#727).

## 3.15.45

Orchestrator guidance adds idle-only outcome QA: progress and required gates first, small optional QA slices only while waiting, never a gate (#904).

## 3.15.44

When a contract amendment already covers a worker's scope request but could not be confirmed delivered, status, the undelivered notice and office answer now name office answer as the way to resume the worker instead of contradicting each other (#799).

## 3.15.43

A herdr agent that is working on its brief, with the pointer no longer in its composer, no longer triggers a 'brief pointer did not land, re-prompt it' notice (#583).

## 3.15.42

The amendment authority filter no longer refuses text that names an email field or address; only outbound email phrasing (email the users, bulk email, emails to customers) counts as an authority action (#543).

## 3.15.41

An amendment that changes only a task's visual.url or visual.start keeps the accepted task accepted and reruns its lane's visual capture, with no executor and no round spent (#728).

## 3.15.40

office close --handoff <pr> closes a run whose blocked integration was composed by hand, recording integration_composed_by_hand; the close refusal names the unapplied amendment (#729).

## 3.15.39

office status keeps an open executor's stop signal, or an executor pane idle without submitting, as a blocker: line with its resolving next: until it resolves (#671).

## 3.15.38

A contract amendment's ripple delivery tells each dependant which task's contract changed and that it is not theirs, instead of telling it to apply the amendment (#960).

## 3.15.37

Plan submit now shows a planner route entry that routing dropped as a notice, and office queue help says pausing a whole run also holds its integration checks.

## 3.15.36

A dependent task's base now composes sibling tasks' appends to the same declared shared: registry as their union instead of refusing the dispatch with dependency-conflict (#537).

## 3.15.35

`office model verify <harness>/<model> --evidence <run>/<task>` promotes a model added with office model add to a local-evidence: source on cited acceptances under independent review; explicit only (#656).

## 3.15.34

`office amend route <task> --auto --quote "<words>"` clears a pending task's declared route so it routes automatically again, and records the change (#977).

## 3.15.33

`office config --run <id> --apply-verification --quote "<words>"` re-pins a running run's check timeouts and check concurrency from the config files, and `office config --run <id> <key>` shows the value the run pinned (#755).

## 3.15.32

A task's worktree setup that exits 127 (command not found) now stops the dispatch with an office doctor hint instead of launching an agent without dependencies; other setup failures stay a notice (#364).

## 3.15.31

Resubmitting a task's accepted tree unchanged (for example after an amendment demoted it) is empty work: no self-review ledger is owed, and the revision records an empty exemption (#781).

## 3.15.30

Adapters can set `quota_probe.unknown_is_unlimited: true` so an unknown quota reads as full headroom; routing stops skipping harnesses that have no probe (#1002).

## 3.15.29

- feat(review): narrow grant (Rico-approved). claude and codex reviewers may run exactly `office review submit --file <own dispatch dir>/review-submit.txt` and nothing else: an exact claude allow plus explicit read-only Bash denies, and a per-dispatch codex execpolicy rule. pi gets no grant (#1030).

## 3.15.28

- feat(review): `office review submit` (stdin or --file) lets a reviewer hand back its verdict for any review kind. It validates the reply for its gate, records it at once and ends the reviewer's wait. Reviewers get only a per-dispatch submit secret, no other Office authority (#1030).

## 3.15.27

- feat(brief): one neutral executor-brief line allows the harness's subagents for the task itself at the executor's discretion. Scope, checks and submit stay its responsibility (#1031).

## 3.15.26

- feat(wake): the root-wake prompt is one line, `Run <id> (through event N): <what changed>; next: <action>. Run office --run <id> context.`. The standing rules moved to SKILL.md (#1029).

## 3.15.25

- chore(harness): remove the gemini harness; agy replaces it (#1028). agy's Gemini models, the gemini model-family floor and agy's ~/.gemini token paths stay.

## 3.15.24

- fix(convergence): a lane finding code is unique across its gate kinds, so `office disposition L-T1:F11` cannot miss a same-coded visual finding (#398).

## 3.15.23

- test(convergence): the retract-in-ingest test expects four convergence reviews, since #768 re-reviews a same-tree recomposition after a contract change.

## 3.15.22

- fix(convergence): a same-tree recomposition after a contract or acceptance change is reviewed again, not carried (#768).

## 3.15.21

- fix(convergence): an EVIDENCE_BLOCKED lane whose block is not the visual capture names `office resume`, not a visual capture amend (#870).

## 3.15.20

- fix(preflight): the stale-ledger line never prints two identical shas (#806).

## 3.15.19

- fix(convergence): a full sha matches an abbreviated composed commit (#893), and a stated commit with the composed commit's tree is the same review (#782).

## 3.15.18

- feat(amend): a live run can amend its PR options after the user confirms (#937).
- feat(land): ordered batch landing across runs and PRs composes and runs the full suite on a branch before advancing main (#943).
- feat(gear): opt-in greenfield lean gear with one strong executor and a final convergence review (#936).
- feat(executor): a per-executor context cap (default 200k) signals the orchestrator; Office never rotates on its own, and `office executor-context <task> rotate | continue | threshold` records the decision with a durable handoff (#939).
- fix(plans): a check with command substitution is no longer misreported by the plan validator (#905).
- feat(wake): auto-wake for non-Herdr Claude Code orchestrators through the bound root's Stop hook (#940).
- fix(doctor): fail when headless Chromium cannot launch, and disclose a skipped visual review at closeout (#942).
- feat(closeout): label each acceptance criterion as verified live or against fakes (#945).
- fix(route): `office route policy --effective` agrees with `dispatch --as` (#899).
- fix(quota): retry a 20s quota probe timeout and fall back to the last known quota (#941).
- feat(status): per-task tokens and cost in `office status`, Codex included (#944).
- fix(guide): the `next:` line names a rerun while the worker is still live (#913).
- refactor(review): new runs default to one-round, one-subagent executor self-review with a test-strength lens and an automatic revert-and-fail proof; security review moves into convergence; v3.1-pinned runs are unchanged (#938).
- feat(status): a nonblocking `oversize:` signal when a dispatch's observed context, wall time or cost outgrows its size class (#917).
- fix(preflight): accept a low-risk `fixed` disposition without a test path (#908).
- docs(onboarding): the missing-CLI question is asked once per session, and the origin probe tells GitHub, checkout and index installs apart (#927).
- feat(plugin): the plugin ships a curated 16-skill set with the original full `/auto-office` and a standalone `/auto-review`, generated into `plugin-dist/` (#926).

## 3.15.17

- fix(panes): a vision conformance probe pane is labelled "vision probe" and closed once its reply is read (#910).

## 3.15.16

- feat(queue): `office queue pause run:<id>` holds the run's automatic integration checks until `office queue resume` (#800).

## 3.15.15

- fix(watchdog): a dispatch in a reused pane reads only the output below its own setup line, so a revoked executor's usage limit never stalls it (#753).

## 3.15.14

- fix(review): a FINDING that quotes `verdict ==` no longer hides the reply's VERDICT line (#894).

## 3.15.13

- test(review): three route-refusal tests use kilo, a harness with no reviewer profile, as their builder-only example. #993 gave pi a read-only reviewer profile, so the old pi example launched and failed preflight instead of being refused.

## 3.15.12

- feat(review): reviewers launch like executors. pi reviews in a Herdr pane (its pane form may write only its reply, and the worktree guard still applies), and its read-only headless form is the CLI fallback. A pane-capable reviewer that ran headless is announced in status (#1027).

## 3.15.11

- fix(routing): an exhausted route (0% headroom) is never within the quota reserve, even when a run pins `quota.reserve_percent: 0`, so Codex at 0% weekly no longer stays the first reviewer.

## 3.15.10

- fix(routing): an ineligible planner `route:` entry drops alone with a notice. The eligible ones keep the planner's order and slots (#875).

## 3.15.9

- fix(review): a Herdr pane reviewer that hit its harness quota wall (the adapter's own signature in the pane's last lines, e.g. opencode "Free usage limit reached") moves the chain to another harness before any re-prompt.

## 3.15.8

- fix(checks): a check whose interpreter lacks its own test runner (`python3 -m pytest` with no pytest) is UNAVAILABLE with the fix, not CHANGES_REQUIRED (#739).

## 3.15.7

- fix(review): a reviewer routes from the run's current pin, not the snapshot its job loaded, so an `--apply-routing` re-pin reaches self-review subagents inside an in-flight convergence job.

## 3.15.6

- fix(config): `office config --run <id> --apply-routing --quote` re-pins the `quota` block too, so a lowered `quota.reserve_percent` reaches a live run.

## 3.15.5

- fix(config): a stale preferred_seed entry in another role no longer blocks config writes or `office route allow`. Only seeds the edit changes are validated, and an unchanged stale seed is a warning naming the command that replaces it (#874).

## 3.15.4

- fix(land): a GitHub error while reading required checks (rate limit, 5xx, network) is `checks-unavailable` with a retry and stale-gh-cache hint, never `checks-failed` pointing at `office rerun` (#772).

## 3.15.3

- fix(land): restack reads the pushed task branch head. A failed accepted-revision push leaves an ancestor that is a safe lease (#538). A head pushed by hand per `next:` whose tree is the accepted revision merged with the base is adopted instead of re-conflicting, and `next:` names `office close --landed-externally` (#676).

## 3.15.2

- fix(convergence): a stacked task's in-place edit of a `shared:` file composes when the composition still holds the file exactly as the task's recorded base (#773); sibling edits are still refused. The `next:` hint no longer says to serialize tasks that already depend on each other.

## 3.15.1

- fix(start): `--no-prs` skips the unsafe-run-base refusal, and explicit `--base` is documented and shown in `office start --help` (#730).
- feat(model): `office model add --role` registers a reviewer like an executor; `pi/gpt-6-luna@high` is a read-only reviewer whose whole rendered command is allowlisted (#993).
- fix(wake): stop duplicate "new lifecycle transitions" wakes. A newer actionable wake is held (default 180s, `OFFICE_WAKE_COALESCE_SECONDS`) while the previous one is delivered but unobserved, then one wake carries the latest event id when the root observes or the window ends; queued/running gate progress no longer changes the wake signature. #723's at-least-once delivery is kept (docs/root-wake.md).
- fix(install): Auto Office is not on PyPI, and the `auto-office` name there is an unrelated project. The install check, README, docs and CLI hints install from `git+https://github.com/Hikari9/auto-office`, and `office update --check` reads the release from GitHub main instead of PyPI.
- docs: point run gear executor slates to policy and clarify the truncated Sonnet fallback (#963).
- fix(skill): the install check's consent stays the native **Install the CLI** choice (#924), not a separate approval stop; the legacy release-identity test and the orchestrator reference follow it.
- test: cover route amendments without a worker environment (#963).
- test: cover review-only reruns when the preferred alternate route is unavailable (#963).
- fix(status): show pre-existing check failures alongside the accepted revision (#963).

## 3.15.0

- Orchestrator stalls: office resume arms root auto-wake by default (--wake-off opts out) and status shows whether wake is armed; queued-too-long, stalled and closed-unread reviews raise a wake event with a next: line (#922, #919); no false stalls for panes waiting on monitors or running self-review subagents (#895); a written reviewer reply is ingested even while the pane reads busy (#918); the office CLI exits 75 with a retry message mid-reinstall and records office.reinstalled (#920); route.recovered event and status line when a quota/auth-avoided route is available again (#921, #946).
- fix(visual): refresh proven reviewers immediately, probe eligible fallbacks beyond four, preserve rejection reasons in status, and share lane-review helpers (#910).
- fix(questions): worker brief prefers durable office raise; the real AskUserQuestion menu from rock-pages e789954a T6 is pinned as a wake fixture, since the footer parser already detects it (#911).
- fix(jobs): terminal outbox rows keep their claim token as provenance, restoring the #705 squash-close close-replay contract after #883 (#898).
- test(review): realign the codex Enter-recovery fixtures with #882's composer behavior so the review-reprompt tests pass again (#898).
- fix(plan-review): a stale PASS ends plan review while a stale round keeps open defects and ATTENTION blocking, the round-budget rule is one shared helper, and a round already in flight reviews an amended revision without re-queueing (#892).
- test(gates): run-check fixtures fail with the task's change, not on base, and the #809 M executor slate is read from effective policy instead of a frozen tuple (#892).
- fix(amend): `--as` is refused on every amendment except `office amend route` with nothing recorded, and `amend route` refuses any plan-amendment flag given a value, empty or not, rather than dropping it, with the drift guard derived from the parser; an executor, including one in its task worktree without its dispatch env, cannot record a route change through `amend route` (#896).
- fix(gates): full-suite checks run under `verification.full_suite_timeout_seconds` (default 7200) only when the command is exactly `scripts/validate.sh` or `./scripts/validate.sh`, alone or with `--full`; launchers, `env`, chains and redirections keep `check_timeout_seconds`. A check timeout names its limit and the setting to raise, and only Office's own kill records a timeout (#897).
- feat(plugin): ship the root auto-office skill in the Claude Code plugin, with CLI-detect onboarding that gates the PyPI install on the user's choice (#914).

## 3.14.0

- catalog: `codex/gpt-6.1-terra` low, medium and high are dispatchable on recorded `office doctor --probe-route` passes (codex-cli 0.163.0, 2026-10-27); none, xhigh and max stay probe-eligible.

## 3.13.0

- refactor(policy): externalize executor/planner size slates, benchmark floors, restrictions and authorized exceptions into one validated catalog policy (#871 PR A).
- test(contracts): lean policy-driven runtime contract suite with handwritten metamorphic oracles and fault-injection proofs (#871 PR B).
- test(gate): switch ordinary PR validation to the lean contract suite and affected-domain selection, with truthful scoped attestations (#871 PR C).

## 3.12.0

- fix(routing): demote provider-wide quota pools on usage limits and reject unsafe live reroutes (#860).
- fix(rerun): make stale accepted dependent reruns actionable and serialize restacks (#856).
- fix(close): stop claimed integration checks and prevent retries after an externally landed close (#836).
- fix(review): treat crashed OpenCode reviewer panes without replies as route failures (#827).
- fix(review): resubmit held Codex brief pointers with composer Enter fix and log reviewer fallback reasons (#790).
- feat(economics): add shadow delegation assessment and sampled efficiency reporting with measured win gates (#502).

## 3.11.2

- fix(validation): compare integration failures against the pinned base, report tracked check mutations, fence stale plan reviews and revalidate accepted-task amendments (#629, #819, #802).

## 3.11.1

- fix(probe): accept printed harness-major route IDs in doctor probes; refuse stale majors before launch (#858, #627).

## 3.11.0

- feat(routing): M executors prefer Haiku 5.5 High with exact audited size-scoped floor exception, retaining independent dispatch conformance and Sonnet operational fallback (#861).

## 3.10.2

- fix(routing): honor harness-major user rules for declared routes and register Argon lineage; align release tests with current routing and catalog policy.

## 3.10.1

- test(gate): run changed-file-targeted `--all` tests for each PR and pre-push; attest scoped Office landing checks separately from the explicit full suite (#854).

## 3.10.0

- feat(catalog): move the shipped model seed to strict, normalized TSV routing and benchmark tables plus YAML provenance; keep legacy in-memory objects, run hashes, lineages, user overlays and policy unchanged (#852).

## 3.9.0

- feat(routing): a user role allow (or exact `preferred_seed` entry) makes a route dispatchable with no proof, qualification or trust gate and overrides a catalog or user-overlay `dispatchable: false`; a runtime route failure is logged (`route.failed`), demoted for a TTL and falls back to the next eligible route; `office route policy --effective` shows what dispatches and why, and `office route allow a,b` reshuffles several routes at once (#845).
- feat(routing): committed role policy is size-scoped allow / neutral / deny from one data file, `catalog/role-policy.yaml` (#850 sections 1-3). Executor follows the #809 slate by size (Haiku, Gemini 3.8 Flash, GPT-6.1 Terra, Sonnet 5.5 on claude and agy, XL Opus on claude only, GPT-6.1 Sol, conditional GLM) and denies Astra and 5.6 Sol/Terra; code and plan reviewers are Luna high > DeepSeek max > Opus low (claude, then agy) with Sonnet medium/high and Opus above low denied; the visual reviewer is Gemini 3.8 Flash low/medium and GPT-6.1 Terra low. User `role_rules` accept `sizes`, `office route allow|neutral|deny|clear --size XS,S` writes them and `office route policy --effective --size XL` shows one size.
- feat(routing): the #809 size slates are the default executor routing (#846, #850 §1 and §4). A sized XS-XL run nominates its slate ahead of a flat `roles.executor.preferred_seed` (a per-size seed you set still wins), slate routes that qualify rank ahead of every route outside the slate (the others stay behind as fallbacks), and an unpinned planned slate whose primary is outside the slate re-routes at dispatch. `status` names the preferred route when a task runs on another. The orchestrator plans every gear, including L and XL; a standalone planner runs only on `--planner dedicated`, routed by run gear: M Opus 5.5 low, Astra low, Argon 4 low; L medium; XL high.
- catalog: register Gemini 4 Argon Medium (unscored, #809 L-only exception) and High (AA v4.3.2 score 53) on agy as non-dispatchable rows pending launch-banner conformance; slugs verified via `agy models` (#809, #850).

## 3.8.2

- perf(test): `office wait` uses a monotonic deadline and a clock/sleeper seam; deterministic wait tests run on a virtual clock (#796, #797).

## 3.8.1

PATCH: #809 conformance follow-up (#841).

- An M run executes a task inline only for a one-task, low-risk run with a structured, strictly positive, beneficial `inline=<n> dedicated=<n> <unit>: <evidence>` estimate (evidence of at least 12 characters and 3 words); zero, negative, non-finite, equal, dearer and placeholder estimates delegate, and S follows the same estimate rule. Runs pinned before the XS-XL gear model never go inline.
- New-run help prefers XS-XL; a historical gear alias prints a compatibility notice and keeps its pinned gates.
- Tests: the scripted-fake routing hides every agy row (Gemini as well as Claude), so a fake worker no longer runs as a real process that drains convergence jobs past an in-process monkeypatch (`test_a_failed_subagent_blocks_convergence_and_spends_no_round` failed about half the runs).

## 3.8.0

MINOR: role-scoped routing policy (#837, advances #627). Backward compatible.

- Policy: role-scoped `allow` / `neutral` / `deny` replaces the `proven` / `valid-unverified` / `quarantined` trust gate. New routes are neutral and eligible; a deny excludes a route for that role only. User config outranks shipped defaults; repo config and `--set` cannot write role rules.
- CLI: `office approve trust` is deprecated: it still runs, records nothing, and prints the new `office route allow|deny` pointer (historical trust acts stay as audit evidence). New `office route policy|allow|neutral|deny|clear|override|migrate`. `office route override` is a user-issued, signed, time-boxed one-off deny override bound to a run, role and route; a quote or orchestrator pin cannot lift a deny.
- Config: new `routing.user_policy.role_rules` and `strict_allow_roles` (user tier only); shipped reviewer `strict_seed` is now `false`. Existing `denied_models` and ceilings keep working unchanged. Pinned older runs keep their pinned runtime.
- Migration: `office route migrate` (dry run), `--apply`, `--rollback`.
- Runtime pinning (#843): a later MINOR or PATCH of the same MAJOR now serves a pinned in-flight run (`version.serves`) and moves it onto its own line at the next idle boundary (`upgrade.auto_adopt`); a live executor no longer strands the run. Only a MAJOR bump, or a line in `version.BREAKING_LINES`, needs `office upgrade`.

## 3.7.0

Rolls up everything landed on `main` since 3.6.6 (the version was not bumped per PR before this release).

- Routing: new-model trials on bounded production tasks, pin-loop fix, strict reviewer seed (#815); quoted override effort ceiling (#818); `routing_seed` no longer hashes `plan_version` (#820); seeded reviewer fallbacks cut by the trust floor are named (#825); Claude effort readback for the conformance probe (#832); no free-route substitution for an available paid preference.
- Catalog: GPT-6.1 Terra with effort-aware 6.1-over-5.6 Sol/Terra supersession (#811); `claude-opus-5-5@low` dispatchable on recorded probe evidence (#835); Pi/Kilo MiMo 2.6 Pro and Flash routes retired (#786).
- Runtime fixes: stalled orchestration recovers with current legal actions (#747); route authority preserved and accepted-task reroutes requalified (#745); squash landing provenance verified before closing runs (#748); runs anchored to durable Git repository roots (#746); fetched bases and dependency composition preserved (#743); review authority packets refreshed and foreign-scope repairs blocked (#742).
- Docs: Codex first-launch trust and hook-review screens (#807); reviewers on luna@high with opus@low the only fallback.
- Versioning: single source `src/office/_version.py`; `pyproject.toml` reads it dynamically; the `VERSION` file and manifest versions are removed; mandatory per-PR semver bump in `AGENTS.md`.
