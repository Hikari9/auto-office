# #837 controls #627 — role-scoped routing-policy migration

Status: **partial implementation; do not release this branch yet**. Supersession date: 2026-10-27.  The authoritative requirement is [#837](https://github.com/Hikari9/auto-office/issues/837), not older trust sections in [#627](https://github.com/Hikari9/auto-office/issues/627). This document is an inspectable engineering checklist and is not approval to merge or deploy.

## Policy contract

- Policy states are **allow**, **neutral**, **deny** for each route and role. Unknown route state is **neutral**, not rejected merely because it lacks a historical trust grant. Shipped known allowances are defaults, not permanent security grants.
- A user-owned explicit role rule overrides a shipped allowance and older global denied-model entry for the matching role/route. A role-specific deny is excluded from automatic selection for **that role only**.
- The route match distinguishes harness, model, effort, and (when the harness major is known) its major version. Broad selectors are rejected unless `--wildcard` is explicit; known aliases are canonicalized when edited via CLI. There is no cross-role propagation of a reviewer denial to executor.
- Availability is **not** policy: quota, credit, rate limits, installed harness, conformance status, capabilities, task shape, safety, reviewer independence, CI, and landing authority remain separate gates.
- Do not infer authenticated user authority from an orchestrator-supplied quote. A persistent **user config rule** is not itself a per-dispatch authenticated override receipt.
- Historical `adapter_trust_acts` and old `proven`, `valid-unverified`, and `quarantined` derivations remain as **audit history only**; their permission gate and approval ceremony are retired. Do not migrate audit rows into a deny list by default.

## New CLI

Inspect effective states and origins without starting a run:

```sh
office route policy --effective --role code_reviewer
office route deny claude/opus@high --role code_reviewer
office route allow claude/opus@low --role code_reviewer
office route neutral claude/opus@high --role code_reviewer
office route clear claude/opus@high --role code_reviewer
# Deliberately broad selectors require --wildcard:
office route deny claude/opus --role code_reviewer --wildcard
```

Edits use `routing.user_policy.role_rules` in the *user* YAML file, validated before writing, with atomic writes and a backup when comments would be lost. `office config routing.user_policy.role_rules --show-origin` is a lower-level inspection alternative. Ordinary `--set` run arguments and repository configuration are **not** user policy authority. Known aliases resolve to concrete model identifiers; when a harness is not installed, its major remains unknown and the CLI must not invent an `@major` identity. Install/probe that harness to inspect its major-scoped shipped defaults.

## Acceptance and verification — progress ledger

- [x] `#837` controlling contract distinguished from legacy #627 trust sections; no attempt to complete #682's obsolete approval implementation.
- [x] Role-specific allow/neutral/deny evaluated in the normal router; neutral admission no longer depends on historical `scoring.evaluate_trust_state`.
- [x] Role-scoped deny is enforced before capability/ranking; user role allow can supersede older global deny; role/effort isolation covered by regression tests.
- [x] Shipped `strict_seed` defaults no longer suppress neutral reviewer fallback; remaining technical capability, quota, task-shape, and floor checks retained.
- [x] Global user-policy config writes and read-only CLI inspection; repo-defined role rules and untrusted run `--set` role-policy mutation refused rather than silently taking user authority.
- [x] Historic `office approve trust` now refuses; historical trust evidence remains present for auditing.
- [x] Initial dispatch and discovery paths avoid rechecking the obsolete quarantine permission gate; quota and exact probe checks remain.
- [x] No destructive runs.db changes; existing audit rows are preserved.
- [ ] Authenticated **one-off user** override receipt that bypasses policy deny for its scope, with actor provenance, expiry, replay resistance, and no orchestrator spoofing. Changing user config is supported; a safe one-off bypass is not yet implemented.
- [ ] Complete UI/CLI parity: web control for role policy editing, origin/denial explanations, and refreshed all legacy trust text/help in onboarding, setup, and inspect views.
- [ ] Migrate legacy policy settings and older pinned runs through a documented migration/backfill/dry-run path with rollback and exact replay parity checks.
- [ ] New catalog route conformance for reviewer roles: bounded neutral trials and exact probes without legacy approval, including robust offline, rate-limit, and temporary-credit state.
- [ ] Reconcile dependency PR #810 (XS–XL), #682 (obsolete trust), other overlapping routing/registry/economics work before merging. Reuse independent already-merged registry/harness work.
- [ ] Comprehensive full-suite pass, on an environment with proven process-group isolation; baseline compare and fix any changed-code regressions.
- [ ] Independent final-HEAD/SHA review of the complete diff, all required CI and security gates, installation smoke, and actual release/prod verification.
- [ ] Independently prove every non-superseded #627 criterion (registry lifecycle, catalog lineage, economics, fallback behavior, configuration, harness compatibility, UI and migration). Do **not** close #627 or #837 on this slice alone.

## Testing and rollback

The remote sandbox on 2026-10-27 passed the focused role-policy, config, core-routing suites. A broader test attempt stopped after 12 failures where the sandbox could not confirm probe-process-group teardown (`reason_class=isolation-missing`); one representative failure reproduces unchanged on `main`. This is **not** a green full-suite or release approval. Run `scripts/validate.sh --all` in a compatible, isolated CI environment, independently review the final SHA, then choose release state under repo policy.

Rollback is a code revert of the feature branch. No production or database mutation is performed by this PR. Before upgrading a real install, back up the global user config; the existing historical trust rows remain unchanged. Prefer a staged opt-in/dry-run compare for older pinned runs rather than silently rewriting them.
