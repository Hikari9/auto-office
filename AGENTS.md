# AGENTS.md

Rules for every agent and model working in this repo. `CLAUDE.md` has the commands and architecture.

## Versioning (mandatory)

Every PR that lands on `main` bumps the release version in that same PR, following semver:

- **MAJOR:** breaking CLI, config or policy changes.
- **MINOR:** new features or behaviour, such as routing or catalog additions.
- **PATCH:** fixes, docs, tests and chores.

- The version is written in exactly one file: `src/office/_version.py`. Bump it there and nowhere else. `pyproject.toml` reads it dynamically, the plugin manifests carry none, and everything else imports `office._version` or `office.version`. Never hardcode a version string; `tests/test_version_single_source.py` fails if one appears.
- Add one line for the new version at the top of `CHANGELOG.md`.
- Parallel PRs that bump the same version conflict. Rebase, take the next free version, and update the `CHANGELOG.md` line.
- The bump is part of the change, not a separate release step. Do not skip it, and do not stop on a classifier block: report the exact command that was blocked.

## Routing contract (3.9, #845)

- **A user allow is sufficient.** `office route allow <route>[,<route>...] --role <role>` (or an exact entry of a role's `preferred_seed`) makes the route dispatchable at once. No conformance probe, vision proof, trust act or benchmark floor gates it, and it outranks a catalog or user-overlay `dispatchable: false`. Only a deny, quota, review independence, the adapter's own capabilities and a launch profile for the role still apply. For a visual route the allow stands in for the per-route `vision` proof (the adapter must still have a vision launch profile), per the #845 mandate.
- **Office owns the invocation.** The adapter and catalog row give the command for each harness (claude, agy, codex, opencode, pi) at each model and effort. A row with no `invocation_model_id` launches its `model_id`. `tests/v31/test_issue_845_invocation_table.py` pins the commands: change them there when an adapter changes.
- **Runtime failure falls back.** A launch failure, timeout, unsupported model or effort, auth failure or transport death logs a `route.failed` event (route, reason, stderr tail), demotes that route for `ROUTE_FAILURE_TTL_MINUTES` (never permanently; an auth failure demotes the harness) and dispatches the next eligible route (`route.fallback`, `route.changed` kind `runtime-fallback`). A route the user declared with `--as` is logged and demoted but blocks the task instead of being swapped.
- **No hidden overlays.** `office route policy --effective [--role <role>]` shows what dispatches and why, and lists legacy `denied_models` and the user catalog overlay. A role allow overrides both.

