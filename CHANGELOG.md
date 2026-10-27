# Changelog

One line or short list per version, newest first. The version itself lives only in `src/office/_version.py` (see `AGENTS.md`).

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
