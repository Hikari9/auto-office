---
name: hermes-cli
description: Directly invocable Auto Office v3 Hermes CLI mechanics primitive. Use when a selected/explicit Hermes harness dispatch needs prompt transport, model and effort selection, working directory targeting, launch environment, liveness detection, completion readback, Herdr pane detection, or Hermes-specific failure attribution. Do not treat this primitive as a separate lifecycle office or proof of adapter promotion.
---

# Hermes CLI primitive

Load `../../adapters/seed/hermes.yaml` plus current local override. New routes start `neutral` for every role (#837); inspect shipped allow/deny defaults with `office route policy --effective --role <role>`.

## Launch

Headless is `hermes -z` (one-shot: prints only the final response; exit 0 only for a completed turn, 2 for a failed or partial one). `-z` takes the prompt as the value of `--oneshot`, never from stdin, so Office passes it as one `--oneshot=<prompt>` argv element. The route's invocation model id is `provider:model` (pins `--provider` and `--model`) or a bare model id (pins `--model` only).

- `--ignore-user-config` is always set: Hermes then reads no `config.yaml`, so its `fallback_providers` chain cannot swap the model mid-run. It needs `--model`, which Office always passes.
- `--reasoning` carries the effort: none, low, medium, high, xhigh and max pass through unchanged (Hermes also accepts minimal and ultra; Office does not use them).
- The launch env removes every `HERMES_*` variable except `HERMES_HOME` (headless env, pane shell via `agent.env`, route probes). They steer the model, provider, config and session.
- `hermes -z` has no wall clock of its own (`--run-budget` belongs to `hermes chat`), so the profile's `max_minutes` cap bounds every headless launch.
- Reviewer and vision profiles run `--in <dispatch dir>` with only the `file,terminal` (and `vision`) toolsets and `office review submit` as the completion step. Hermes has no read-only sandbox, so Office fingerprints the reviewed worktree before and after and fails the review as degraded if it changed. Evidence images reach the vision form as paths in the prompt.

## Readback

`--usage-file` makes Hermes write a JSON report (`model`, `provider`, `completed`, `failed`, `failure`, token counts) even when the run fails. Office compares `model` and `provider` with the pinned route: a mismatch, or no report after a clean exit, ends the dispatch as `wrong_model`. A crash with no report is not wrong-model.

## Failure attribution

Hermes exits 2 for every failed turn, so the output decides. "subscription usage is exhausted", "credit balance is too low" and "rate limit" are attributed quota/account, never the model; they feed the usual quota-wall path (route cooldown, task blocked instead of relaunched into the same wall). "quota exceeded" is the same; "model not available" is the harness, "context length exceeded" the model, "invalid API key" environment/network.

## Access and Herdr

- `office.hermes_access.reachable_providers()` lists providers with at least one non-exhausted credential, parsed from `hermes auth list` names and status only; no secret value is read or kept.
- Herdr cannot detect a git-installed Hermes: the PATH `hermes` is a shell wrapper that execs `python3 -I -c '<inline script>'`. The pane form resolves the checkout's launcher script and its interpreter (`hermes_access.resolve_launcher`) and puts a shim that execs `python3 <install>/hermes` first on PATH in the pane's `agent.env`, so Herdr's `hermes` agent kind sees the process.
- `office install --only hermes --item herdr-hermes` runs `herdr integration install hermes` (Herdr's own lifecycle plugin). `office install` with no `--item` and no `--only hermes` never does; Office writes no Hermes config.

## Sessions

Hermes records sessions in `$HERMES_HOME/state.db` (default `~/.hermes/state.db`, SQLite). Office reads it read-only to confirm a pane prompt landed, to find the session id and to read a pane reviewer's final reply. Skill detection uses `skill-md-read` inference since Hermes has no first-class Skill tool.

Attribute invocation failures to adapter, quota failures to quota/account, and logical implementation defects to the producer model/role only when evidence supports it.
