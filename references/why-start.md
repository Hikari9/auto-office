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

The five planner-frozen intent fields (`goal`, `done_criteria`, `blast_radius`,
`named_actions`, `non_goals`) are frozen at the end of the planner's interactive discovery
(issue-35#decision-1) so downstream stages can rely on them without re-confirming intent every
step. Freezing them is a compatibility contract for the run, not grounds to treat them as
authoritative over the normative spec if the two conflict — the spec still wins.

## Why the tracking issue is filed before planning

The tracking issue is created or reused immediately after `start`, from the raw request before
the planner's interview, so the run has a durable public record even if discovery stalls, quota
is exhausted, or execution stops short. This preserves the useful v2 behavior without moving the
v3 planner's interactive ownership or freezing intent early. The `file-issue` skill owns duplicate
searches and repository/access checks; a missing issue is a blocker, not a reason to invent a
number. Once the issue exists, record its number or URL on the family registry, update its body
after the plan is approved, and leave it open when the run is unresolved; completed work references
it with `Closes #N` in the PR body.
