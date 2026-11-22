# OpenCode seed live smoke — 2026-10-15

This is live launch evidence for the exact shipped `adapters/seed/opencode.yaml`
in the T3 worktree. It records **launch evidence only**. Neither an exit code nor
a completed response establishes which model served a turn, so this is **not
model-identity conformance**. The effort actually dispatched in both forms was
`xhigh`.

## Seed validation and pin

- `office harness validate --file adapters/seed/opencode.yaml` — passed:
  `harness opencode (file, adapters/seed/opencode.yaml): valid`.
- OpenCode version: `opencode v2.0.29`.
- `opencode models` listed `opencode/muse-spark-1.3-contributor-free`.
- The seed maps Office `xhigh` to the `#xhigh` variant. The dispatched model
  argument in both live forms was exactly
  `opencode/muse-spark-1.3-contributor-free#xhigh`.

## Headless worker form

Launched by hand in the throwaway git repo
`/Users/rico/.local/state/auto-office/runs/980a839f-8478955456ee343344287671/tmp/opencode-headless-s4TYbp`:

```text
opencode run --model opencode/muse-spark-1.3-contributor-free#xhigh "Reply exactly: OPENCODE T3 HEADLESS COMPLETE"
```

- Started: `2026-10-15T05:51:12Z`; completed: `2026-10-15T05:51:16Z`.
- Exit code: `0`.
- Stdout: `OPENCODE T3 HEADLESS COMPLETE` (30 bytes).
- Stderr: `> build · muse-spark-1.3-contributor-free` (with terminal reset
  control bytes).
- The command returned and its process was no longer alive; completion and
  termination were observed.
- No `--auto` permission flag was passed.

## Herdr interactive form

Started in Herdr pane `w1M:pR`, agent `office-t3-opencode`, in scratch git repo
`/Users/rico/.local/state/auto-office/runs/980a839f-8478955456ee343344287671/tmp/opencode-herdr-BgGA2j`:

```text
herdr agent start office-t3-opencode --kind opencode --pane w1M:pR --timeout 30000 -- mini --model opencode/muse-spark-1.3-contributor-free#xhigh
```

Herdr returned the launched argv as:

```json
["opencode", "mini", "--model", "opencode/muse-spark-1.3-contributor-free#xhigh"]
```

The pane reported `agent: opencode`, `interactive_ready: true`, and
`agent_status: idle`. Prompt delivery was attempted with:

```text
herdr agent prompt office-t3-opencode 'Reply exactly: OPENCODE T3 HERDR COMPLETE' --wait --timeout 120000
```

That command returned `agent_prompt_stalled` because it did not observe a
working or blocked transition within five seconds and the current state was
`idle`. The pane transcript nevertheless showed the delivered prompt
`Reply exactly: OPENCODE T3 HERDR COMPLETE`, followed by the exact response
`OPENCODE T3 HERDR COMPLETE`; a subsequent `herdr agent get` reported `idle`.
This transcript is the prompt-delivery and completion evidence; the CLI's
transition timeout is recorded rather than treated as a clean command result.

The pane was closed with `herdr pane close w1M:pR` at
`2026-10-15T05:52:12Z`; a subsequent pane listing no longer contained `w1M:pR`.
The OpenCode processes started in the scratch pane and headless command were
terminated. A separate `opencode serve --service` process with an observed
uptime of about 19 hours predated this smoke and was left untouched.

## Limits

- These runs show that the seed's pinned launch forms accepted the selected
  model and `xhigh` variant and completed the prompts. They do not read back or
  establish model identity, provider-side routing, or model conformance.
