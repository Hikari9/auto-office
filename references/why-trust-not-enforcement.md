# Why the office trusts agents instead of enforcing on them

Ratified by Rico on 2026-09-14, closing issue #94. This is the office's position
on mechanical enforcement, and it settles a question five review rounds could not.

## The position

Non-determinism is the cost of using agents. It is an expectation, not a defect
to be engineered away. The roster is intelligent enough to comply with a rule
stated in a prompt; building machinery to make compliance impossible to avoid
buys less than it costs, and the machinery itself becomes the thing that breaks.

Guardrails belong in the brief. Correction belongs to the orchestrator, applied
when something actually goes wrong, not to a classifier applied to every call in
advance.

## Three decisions

**Do not classify Bash.** Pass `--yolo`, or state read-only in the prompt, and
trust the executor. Do not attempt to decide from command text whether a command
mutates. Attempting to nail every case is the error, not an unfinished version of
the right idea. The five-round history of `scripts/hooks/pre_tool_use.py` — eleven
bypasses fixed, a `PLAN DEFECT` on the sixth — is what this decision is reacting
to.

**Do not infer the target repository.** Trust that the executor works where its
brief sent it, and correct its course when it does not. This shifts the burden to
where it belongs: the planner and orchestrator must be on top of worktree
locations. Knowing where every dispatch is working is orchestration, and an
orchestrator that needs a hook to tell it is not orchestrating.

**Do not disambiguate user from orchestrator.** Prompts come from the user
and/or the orchestrator. The orchestrator has authority to decide on its own and
acts on the user's behalf toward subagents. `approve-plan --quote` is an audit
record of what was approved, not a credential, and no authenticated channel is
needed to make it legitimate.

## What still holds

Dropping mechanical enforcement does not drop the rules it was trying to enforce.
These are unchanged, and they bind the orchestrator:

- Merge to `main` is not the orchestrator's to authorize. Only an explicit
  per-run statement from the user lifts it (lifecycle spec §9.1). The
  orchestrator's authority over subagents does not extend here.
- A producer does not approve or review its own work. The orchestrator acting for
  the user is about *who may authorize a dispatch*, not about collapsing the
  producer and reviewer into one agent (lifecycle spec §8).
- Protected paths, blast-radius ceilings and destructive-action confirmation stay
  in every brief. `--yolo` removes the sandbox, which makes the stated ceiling the
  entire safety boundary — so state it.
- Phase order still governs, and the runtime still refuses to write `approved`
  outside `approve-plan`. That is cheap, deterministic, and costs nothing to keep.

## What the hook does now

`scripts/hooks/pre_tool_use.py` lost its classifier — four command tables, the
shell parser, the git sub-command analysis, the approval-command exemption, and
the Bash target inference, about 300 lines. `Bash` came out of the `PreToolUse`
matcher, so shell commands are never inspected.

What remains decides from the tool name and the run's recorded phase, and nothing
else:

| Below approval | Result |
|---|---|
| `Edit`, `Write`, `MultiEdit`, `NotebookEdit`, `ApplyPatch`, any unknown tool | blocked, `approval_required` |
| `Read`, `Grep`, `Glob`, other read-only tools | allowed |
| `Bash`, any shell | allowed, uninspected |
| malformed state while a run is active | blocked, `unreadable_run_state` |

Blocking the structured edit tools survives because it needs no classification:
no false positives, no unbounded surface, no maintenance. It is worth keeping for
the same reason the classifier was not.

A reviewer who finds that `rm -rf src` passes below approval has found the
design, not a bypass. `tests/test_hooks.py::test_pre_tool_use_does_not_inspect_bash`
asserts it on purpose.
