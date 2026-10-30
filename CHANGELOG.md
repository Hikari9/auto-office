# Changelog

One line or short list per version, newest first. The version itself lives only in `src/office/_version.py` (see `AGENTS.md`).

## 3.13.1

- fix(windows): the `office` CLI runs natively on Windows (#915). New `office.portable` replaces `fcntl`, `killpg`, `os.kill(pid, 0)` (which kills on Windows), `start_new_session`, `os.uname` and `os.getloadavg`; binary-safe `os.open` flags; README documents the git install (the PyPI `auto-office` is a different project), Python pinning and Windows notes; D142-7 superseded.

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
