---
name: hermes-cli
description: Directly invocable Auto Office v3 Hermes CLI mechanics primitive. Use when a selected/explicit Hermes harness dispatch needs prompt transport, model and effort selection, working directory targeting, launch environment, liveness detection, completion readback, Herdr pane detection, or Hermes-specific failure attribution. Do not treat this primitive as a separate lifecycle office or proof of adapter promotion.
---

# Hermes CLI primitive

Load `../../adapters/seed/hermes.yaml` plus current local override. New routes start `neutral` for every role (#837); inspect defaults with `office route policy --effective --role <role>`.
- Headless is `hermes -z` (exit 0 only for a completed turn, 2 for any failed one). The prompt is one `--oneshot=<prompt>` argv element, never stdin. The model id is `provider:model` (pins `--provider` and `--model`) or a bare id (pins `--model`).
- `--ignore-user-config` is always set, so `config.yaml`'s `fallback_providers` cannot swap the model. `--reasoning` takes none, low, medium, high, xhigh, max unchanged.
- The launch env drops every `HERMES_*` variable except `HERMES_HOME` (headless env, pane shell via `agent.env`, route probes). `hermes -z` has no wall clock, so the profile's `max_minutes` bounds it.
- Worker, reviewer and vision (requirement r3, accepted risk): `--yolo` plus the terminal toolset can write the worktree and `--in` only moves the cwd, so a reviewer or vision fingerprint detects a write but cannot prevent one. Vision adds the vision toolset and reads images from prompt paths; capability stays builder and review, so vision needs its conformance proof.
- Readback: `--usage-file` writes JSON (`model`, `provider`, `completed`). A model or provider mismatch, no report, or `completed` not exactly true after a clean exit ends the dispatch as `wrong_model`; a crash with no report does not.
- Failure text decides attribution (exit 2 is every failure): "subscription usage is exhausted", "credit balance is too low", "rate limit", "quota exceeded" are quota/account and feed the quota-wall path; "model not available" is the harness, "context length exceeded" the model, "invalid API key" environment/network.
- `office.hermes_access.reachable_providers()` parses `hermes auth list` names and status only; no secret is read.
- Herdr cannot detect a git-installed Hermes (PATH `hermes` is a shell wrapper running inline `python3 -I -c`). The pane form resolves the checkout launcher and interpreter (`hermes_access.resolve_launcher`) and puts a shim first on PATH in `agent.env`.
- `office install --only hermes --item herdr-hermes` runs `herdr integration install hermes`; no other install path does. Office writes no Hermes config.
- Sessions live in `$HERMES_HOME/state.db`; Office reads it read-only. Skill detection uses `skill-md-read` inference.
Attribute invocation failures to adapter, quota failures to quota/account, and logical defects to the producer only when evidence supports it.
