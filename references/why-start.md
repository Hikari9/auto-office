# Why: start-of-run rationale

Rationale for the pinned rules in the "Start or resume a run" section of `SKILL.md`. Read this
only if a rule there is disputed; it is not required to follow the rule.

## Why `start` is the only entry point

Doing the work inline instead of running `office_runtime.py start` is the observed failure mode
this step exists to close: agents that skip it produce runs with no `state_dir`, so every later
`check-spoke`/`mark-spoke` call has nothing to attach to and silently no-ops. There is no other
valid entry point.

## Why the config parenthetical matters

`start` resolves effective config by merging `prompt/CLI > repo > user > plugin default` and
emits `effective_config_hash`. The user tier lives at `~/.config/auto-office/config.yaml`,
outside the repo and invisible to repo search, and routinely sets `preferred_seed` chains the
plugin default lacks. An agent that only reads `config/config.default.yaml` and treats it as the
full answer will route against a stale/incomplete config and produce a route that looks correct
but ignores the operator's actual preference. Check the user-tier file directly.

## Why frozen intent fields are compatibility data, not license

The five orchestrator-owned intent fields (`goal`, `done_criteria`, `blast_radius`,
`named_actions`, `non_goals`) are frozen so downstream stages can rely on them without
re-confirming intent every step. Freezing them is a compatibility contract for the run, not
grounds to treat them as authoritative over the normative spec if the two conflict — the spec
still wins.
