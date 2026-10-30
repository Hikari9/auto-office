# #837 controls #627 — role-scoped routing-policy migration

Status: **substantial implementation; release gates pending**. Supersession date: 2026-10-27.  The authoritative requirement is [#837](https://github.com/Hikari9/auto-office/issues/837), not older trust sections in [#627](https://github.com/Hikari9/auto-office/issues/627). This document is an inspectable engineering checklist and is not approval to merge or deploy.

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

Release: MINOR bump per `AGENTS.md` (see `CHANGELOG.md`): new routing policy and commands. `office approve trust` stays as a deprecated no-op, so existing scripts keep working.

- [x] #837 controls legacy #627 trust sections; #682's approval ceremony is not implemented.
- [x] Role-scoped allow/neutral/deny in the router; neutral admission never reads historical trust. Role rules outrank legacy `denied_models`; conflicting equivalent rules resolve to deny; a canonical `@major` deny still matches when the installed harness major cannot be read (never fails open).
- [x] Candidate building is role-aware: a role allow outranks a legacy global deny, a role deny stays role-scoped.
- [x] Strict allow-only routing is an explicit user opt-in (`routing.user_policy.strict_allow_roles`, user tier only). Shipped reviewer `strict_seed` is `false`.
- [x] User config writes, effective inspection, repo/`--set` refusal, `office approve trust` a deprecated no-op (records nothing, points at `office route`), historical trust acts preserved as audit rows.
- [x] Authenticated one-off override: `office route override <route> --role R --run RUN --reason "..."`. Issued only from an interactive terminal (refused for workers, pipes, scripts, and orchestrator quotes), HMAC-signed with a per-user key, bound to run + role + exact route, 1m–24h lifetime, revocable, consumed by the first dispatch or gate that records its id (a second launch is refused), every use appended to `route_override_uses`. A quote or orchestrator `--as` never lifts a deny. Capability, sandbox, reviewer-independence and landing checks still run afterwards. Limits: a process running as the same OS user can read the key file (OS boundary, documented in `route_override.py`).
- [x] Trial gate: a role-denied trial route or fallback blocks a trial (`role-deny` / `no-fallback`); the quarantine veto is gone.
- [x] Migration: `office route migrate` dry run (legacy `denied_models`, what the old gate blocked, user-quarantine acts as optional deny suggestions), `--apply` (adds explicit all-role deny rules, removes nothing, backup), `--rollback`. Historical trust acts, runs and pinned policies are never rewritten. Decision hashes of recorded routing decisions are unchanged (golden test); only the eligibility record shape changed.
- [x] Legacy trust wording removed from CLI help, harness scaffold/validate/smoke/list output, onboarding, SKILL.md, route/adapter/harness skills and docs. `inspect trust` and the trust report are labelled audit-only.
- [x] Version bump does not brick in-flight runs: compatible MINOR/PATCH runtimes serve pinned runs and auto-adopt at an idle boundary; MAJOR needs `office upgrade` (`version.serves`, `upgrade.auto_adopt`, tests in `tests/test_version_serves.py` and `tests/v31/test_discovery_pinning.py`).
- [~] Web: the model registry shows per-role effective state with its source and points to `office route` for edits. Web-side editing of role rules is not implemented.
- [ ] Reviewer-discovery bounded trials for non-executor roles: not implemented (the discovery design keeps trials builder-only; reviewers run neutral routes directly under the same capability and independence checks).
- [ ] Provider-credit exhaustion as a timed availability state: left to #831.
- [ ] #810 (XS–XL) compatibility: not re-verified after rebase; #810 is still a draft.
- [ ] Independent final-SHA review, release and post-deploy verification: see the PR body.

### #627 acceptance walk

| # | Item | Disposition |
| --- | --- | --- |
| 1 | Two named-lineage generations retained | Merged earlier work (#811, `model_lineages`); not re-verified here |
| 2 | Alias binding and no displacement by ambiguous previews | Merged earlier work; not re-verified here |
| 3 | Global deny Sol ≤5.6 vs 6.1, undo, manual-only separate from availability | Existing ceilings/denied_models tests still pass; role rules added |
| 4 | Policy vs technical states have independent reason codes | Role policy and technical gates are separate categories (`role-deny` vs quota/capability/probe) |
| 5 | Atomic validated registry update | Not touched; not verified here |
| 6 | Migration dry-run, reversible, reports conflicts | `office route migrate` (this PR); conflicting hand-edited rules are validated, never overwritten |
| 7 | First Run: continue with defaults / review policy | Onboarding text updated; no trust prompt remains |
| 8 | Planner envelope and whole-wave economics | Not touched; not verified here |
| 9 | Manual route wins; user-denied needs explicit policy change | Superseded by #837: a deny yields only to user config or a user-issued receipt |
| 10 | Soft reservations, queued-task replan | Not touched; budget work is #831 |
| 11 | Telemetry opt-in | Not touched |
| 12 | Web grid/inspector, cards, CLI/web parity | Partial (read-only role policy); web editing open |
| 13 | Pin/replay/rollback identities | Decision hashes stable (golden test); pinned runs keep their pinned runtime and config |
| 14 | #607 remains a linked follow-up | Unchanged |

## Testing and rollback

The remote sandbox on 2026-10-27 passed the focused role-policy, config, core-routing suites. A broader test attempt stopped after 12 failures where the sandbox could not confirm probe-process-group teardown (`reason_class=isolation-missing`); one representative failure reproduces unchanged on `main`. This is **not** a green full-suite or release approval. Run `scripts/validate.sh --all` in a compatible, isolated CI environment, independently review the final SHA, then choose release state under repo policy.

Rollback is a code revert of the feature branch. No production or database mutation is performed by this PR. Before upgrading a real install, back up the global user config; the existing historical trust rows remain unchanged. Prefer a staged opt-in/dry-run compare for older pinned runs rather than silently rewriting them.
