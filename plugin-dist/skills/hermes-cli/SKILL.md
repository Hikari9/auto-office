---
name: hermes-cli
description: Hermes CLI mechanics for selected Hermes dispatches (launch, readback, failure attribution, access). Not a lifecycle office.
---

# Hermes CLI primitive

Adapter: `../../adapters/seed/hermes.yaml`. New routes start `neutral` (#837); check `office route policy --effective --role <role>`.

- Headless: `hermes -z --oneshot=<prompt>` (one argv element, never stdin). Exit 0 = completed turn, 2 = failed or partial.
- Route `provider:model` pins provider and model; a bare id pins model only. Always `--ignore-user-config --model <m>`; effort via `--reasoning`.
- The launch env drops every `HERMES_*` variable except `HERMES_HOME`.
- Reviewer and vision runs use `--in <dispatch dir>` and `file,terminal` (plus `vision`). Office fingerprints the worktree and fails a changed review as degraded.
- Readback: `--usage-file` JSON. A `model` or `provider` mismatch, or no report after a clean exit, is `wrong_model`.
- Exit 2 covers every failed turn, so read the output. Quota or rate-limit text is quota/account; "model not available" is the harness; "context length" is the model; "invalid API key" is the environment.
- Access: `hermes_access.reachable_providers()` reads `hermes auth list` names and status only.
- Herdr: `office install --only hermes --item herdr-hermes`. Office writes no Hermes config.
- Sessions: `$HERMES_HOME/state.db`, read-only.
