# First-run onboarding (#484)

`/auto-office` offers a short onboarding the first time a user invokes it, and again only when the onboarding schema changes.
The invoking agent presents the questions through its harness's native question tool. `office onboard` owns detection,
eligibility, validation and persistence, so the skill never writes config or judges a route itself.

## Contract

| Command | Effect |
|---|---|
| `office onboard --harness <h> [--json]` | Status only, never prompts or writes: `due`, `reason`, the current harness and its Office integration, every harness with its sign-in state, the three questions with current values, options, eligible and unavailable routes. |
| `office onboard --planner A --executor A --reviewer A` | Validates every answer, then writes the accepted preferences and `onboarding.schema_version` to the user file in one atomic write. An omitted flag means `keep`. |
| `office onboard --skip` | Writes only `onboarding.schema_version`. Every preference stays as it was. |

An answer is `office` (Let Office decide), `keep`, or a full `harness/model@effort` route.

| Question | Writes | Routing semantics (unchanged) |
|---|---|---|
| Default external planner | `roles.planner.preferred_seed` | Stronger advisory seed. Used when a run queues a dedicated planner. Inline planning is the orchestrator's. |
| Preferred executor | `roles.executor.preferred_seed` | Weighted evidence in adaptive routing: preference weight 10% by default, capped at 25%. |
| Preferred reviewer | `roles.plan_reviewer.preferred_seed` and `roles.code_reviewer.preferred_seed` | Stronger advisory seed. `visual_reviewer` is not written, so visual review keeps its own capability-checked routes. |

`office` removes only that role's user-level `preferred_seed`, so the shipped default applies. The repo file is never written,
and a repo override still wins in its repository. `office onboard` reports one when it exists. Running runs keep the policy they
pinned (#440). `office config --run <id> --apply-routing --quote "<words>"` stays the only way to re-pin a run.

User-facing copy says preference, not promise: *Office favors this route according to its routing policy, but may choose another model or
effort when availability, quota, task fit, capability, evidence, or policy calls for it.*

## Eligibility

A route is offered or accepted only if all of these hold:

- It has a catalog row with that effort, and the model is not below a `model_family_floors` entry.
- The harness adapter has a launch profile for the role, and the harness binary is on PATH.
- A sign-in is found. A binary is not a sign-in. Office checks the stored credential each harness uses, without reading any secret: Claude via the credentials file, the keychain entry or an API/cloud env var; Codex via `$CODEX_HOME/auth.json` or `OPENAI_API_KEY`; agy via its OAuth token file; Gemini via `oauth_creds.json` or an API key. Hermes has no check and reads as `unknown`, which is offered.
- It passes routing stages 1-5 (`routing.route`): hard exclusions, derived trust for trust-gated roles (`executor`, `code_reviewer`), required capabilities, and the role floor. Trust is never granted here. An unproven route is listed as unavailable, with the `office approve trust` command only the user may run. Onboarding does not apply adaptive routing's learned eligibility, so it may offer fewer executor routes than dispatch would accept.

A reviewer route must be eligible for both plan and code review. Quota and task fit are not judged at onboarding; the router decides
them at dispatch. Unavailable routes appear in `unavailable` with a reason, as diagnostics only.

Seeded options are the shipped `preferred_seed` entries of the question's roles, each resolved to its first eligible route. The executor
ships no seed (adaptive routing decides), so its question offers Let Office decide, the current value, and custom routes.

## Versioning

`office.onboarding.SCHEMA_VERSION` (currently 1) is independent of the package version and of the config `schema_version`.
Onboarding is due when the user file's `onboarding.schema_version` is lower than it. Bump the constant only when the questions change
enough that existing users should answer again. They then see their current preferences prefilled. A repo file cannot set
`onboarding` (`office config --repo` refuses it and resolution ignores it). `office config onboarding.schema_version 0` makes onboarding due again.

## Current-harness integration

`office onboard` reads only the current harness's integration, using `install.integration_status`. The harness comes from `--harness`,
then `OFFICE_HARNESS`, `CLAUDECODE`, `GEMINI_CLI`, then the process tree. The result is `installed`, `missing`, `partial`, `stale`,
`unsupported`, `harness-missing`, `unreadable`, or `unknown`. Only `missing`, `partial` and `stale` offer the three choices:

- **Install recommended**: `office install --only <harness>`.
- **Review individually**: show each item's `change`, then `office install --only <harness> --item <id>` for each accepted item.
  Items: `session.start`, `prompt.submit`, `tool.pre`, plus `read-rules` on Claude (the read-only `permissions.allow` rules reviewers need).
  Items not accepted are left exactly as they are.
- **Skip**: write nothing. Core commands work without hooks.

Install stays idempotent and backs up the settings file before a write. It never touches unmanaged entries. Legacy entries are only
reported, and removed only with `--migrate-legacy-hooks`. `office uninstall` removes every Office-managed entry. Claude and Gemini are
the only verified hook mappings. Codex, agy and Hermes report their known limitation (`unsupported`), and so does any other harness
(Pi included) until it has one. Onboarding never writes another detected harness's config.

## Headless and failures

`office onboard` never prompts. A session that cannot ask the user does not wait and does not `--skip`. It continues the
request on the current preferences and tells the user to run `office onboard`. Any refused answer or failed write leaves the user file
unchanged and onboarding still due. If `office onboard` itself fails (for example, an unreadable user config), report the error and continue the request. Onboarding never blocks intake.
