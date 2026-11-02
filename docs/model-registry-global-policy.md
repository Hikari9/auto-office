# Model Registry foundation — issue #610 (global-first, partial implementation)

## Scope and decisions

This is the **first, deliberately reviewable slice** of
[implementation #610](https://github.com/Hikari9/auto-office/issues/610).
The [Wayfinder #601](https://github.com/Hikari9/auto-office/issues/601)
and its decision tickets #602–#606 remain authoritative. This work does not
yet implement the full catalog publish/rollback transaction, bound per-task
routing envelopes, soft shared quota reservations, first-Run prompt or the
approved desktop/mobile registry interface.

The canonical lineage map at `catalog/lineages.yaml` separates a **named
model series** (e.g. `openai/gpt-sol`) from the broader
`candidates.model_family()` vendor group, which is used by independent
review routing and **must not be changed**. For every listed generation there
is one stable concrete identity, numeric version, release status and explicit
provenance. Harness and effort variants of one generation share a slot in
the **two-newest-released generations** calculation. This is a staging
calculation until catalog publication is implemented.

Only versioned entries **explicitly classified released** may take an active
window slot. A new unclassified model ID, preview, rename or missing
provenance is pending and cannot displace a known released generation.
Existing shipped names are migrated with `shipped-catalog` provenance:
this labels existing records; it **does not** claim a new upstream or
local-harness verification. New identities must be reviewed with verifiable
provider release evidence before they are added.

`office model registry --json` reports the read-only staged
active/pending/archive grouping. It is **not** a publish action and does
not grant invocation permission or conformance trust. `office model list
--json` includes independently observable per-route technical
`dispatchable`, user denial reason and exact known generation/lineage.
`catalog/lineages.yaml` now contributes to the pinned catalog hash. Old
runs pinning earlier versions remain under their older runtime contract.

## Global user-owned version ceilings

A global user-level key, independent of `catalog/seed.yaml` and current
quota/benchmark/conformance, expresses an *inclusive* maximum denied
version for a specific lineage:

```yaml
routing:
  user_policy:
    version_ceilings:
      openai/gpt-sol: '5.6'
```

Thus `gpt-5.6-sol` **and older** are filtered, and `gpt-6.1-sol`
is **not** disabled. Explicit user model denial and harness filters in
`denied_models` continue to take precedence. A denied route stays denied
for automatic **and** manual routing until the user removes the denial.
`overkill_rules` remain advisory for automatic routing only, never a
system-authored disable. Actual conformance and availability are separate
hard gates; a new permitted generation is not necessarily runnable.

Commands (mutations write the user config, not shipped resources):

```shell
office model policy
office model registry --json
office model ceiling openai/gpt-sol --through 5.6
office model unceil openai/gpt-sol
office model enable codex/luna
office model add pi/openai/gpt-6-luna --effort high --role reviewer   # register + allow code/plan review
```

`office model add ... --role executor|reviewer` registers the catalog rows and writes the matching
role `allow` in one step. A reviewer runs on the adapter's `reviewer` launch form, or one derived from
its worker form plus `reviewer_readonly_args` (read-only tools only, headless, never vision). Derived
forms set `verify_worktree_unchanged`: a review that changed HEAD, the tracked diff or untracked files
fails that route as degraded and the next reviewer runs. The check does not cover gitignored files or `.git/`, and a plan review runs in the repo checkout, so an
edit by anyone else during it also reads as a change. A directory that is not a git checkout cannot be
fingerprinted, so the route fails as degraded instead of passing unchecked. See `skills/auto-adapter/SKILL.md`.

The `enable` command removes **legacy user-overlay** disables set by
`office model disable`; it never bypasses an existing technical
`dispatchable: false` gate or a separately configured user denial.
Use `office config --user routing.user_policy.denied_models` to inspect
the existing explicit model/harness deny list; use the config command's
validated mutation path to edit other preference arrays.

The CLI writes global version ceilings only. Historical repository-level
`denied_models` / `overkill_rules` remain supported by the old routing
configuration. General repository-level user-policy layering, source
precedence, and repository UI are the separate
[phase-two #607](https://github.com/Hikari9/auto-office/issues/607).
This code does not change those legacy rules.

## Safety and compatibility

- Do not infer a concrete generation from `model_id` regex/string sort.
  Legacy custom aliases not present in the canonical lineage map still
  use their former resolver only for backwards compatibility. They never
  classify an active canonical lineage.
- Keep `invocation_model_id` harness-specific. Scores, discoverability and
  user allow flags are never proof that exact adapter+model+effort conformance
  passed.
- All unknown future versions must be staged with provenance before being
  promoted by a later atomic publication transaction.
- `catalog/seed.yaml` and existing historical rows are not rewritten.
  No default dispatchability, reviewer/plan trust, benchmark index, telemetry
  setting, or live quota threshold changed.
- The default telemetry policy remains opt-in/off. New code never launches
  a harness, searches online or polls quota as part of staging.
- Synthetic registry test cases and fake harness fixtures provide test
  coverage, but real CLI/model availability must be verified separately
  before #610 is considered complete.
