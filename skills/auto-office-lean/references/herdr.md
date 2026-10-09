# Herdr: observable native agents

Herdr is a pane and agent controller, not the lifecycle authority. **Operate only inside the owning Herdr session** (`HERDR_ENV=1`). Read `herdr --help` and the command group help on the installed version; do not call bare `herdr`, which can launch the TUI.

## Discover, launch and verify

```bash
test "${HERDR_ENV:-}" = 1 || { echo 'Not inside Herdr' >&2; exit 1; }
herdr agent list
herdr pane current --current
PANE=$(herdr pane split --current --direction right --cwd "$WT" --no-focus | jq -r ".result.pane.pane_id")
test -n "$PANE" && test "$PANE" != null || exit 1
herdr agent start worker-api --kind codex --pane "$PANE" -- \
  --model "$MODEL" -c "model_reasoning_effort=\"$EFFORT\""
herdr agent prompt worker-api "Read $BRIEF and complete only its scope; report commit, checks and blockers." --wait --timeout 120000
herdr agent get worker-api
herdr agent read worker-api --source recent-unwrapped --lines 120
```

The example uses `jq` to extract the returned pane ID. The shell pane must be free, its cwd verified and its agent name unique. To start another harness, use `--kind claude -- --model "$MODEL" --effort "$EFFORT"` or `--kind agy -- --model "$AGY_MODEL"` after consulting that harness's reference. Prefer a one-line absolute brief pointer to pasting a multi-paragraph prompt, and verify actual progress after `agent prompt` (it can acknowledge input without proving execution).

Inspect `herdr pane get "$PANE"` and the agent's banner/session metadata to confirm **pane ID, foreground cwd, model, effort and permissions**. `agent start` returning `agent_not_ready` can mean a startup trust/approval question, not a failed task. Read before responding. AGY may show `idle` while actively working; corroborate with output and artifacts.

## Blocked input, results and cleanup

```bash
herdr agent get worker-api
herdr agent read worker-api --source visible
herdr agent wait worker-api --until blocked --timeout 120000
# Only if a known selection/input is authorized and the displayed UI matches:
herdr agent send-keys worker-api enter
# After the agent is truly done and its work is preserved:
herdr pane close "$PANE"
```

`herdr agent prompt` is for agent instructions. Never use `herdr pane run` or `pane send-text` to send instructions into an agent composer; those are for ordinary shell panes. If a prompt is visibly typed but unsubmitted, send only `herdr pane send-keys "$PANE" enter`, not the text again. Answer technical implementation questions yourself; obtain user authority for new requirements, permissions, destructive/external actions, or merges.

Track exactly the panes this orchestrator creates and close only completed, **owned** panes by recorded ID. Never close your own `$HERDR_PANE_ID`, an unknown user's pane, or one still `working`/`blocked`. Keep an agent-to-worktree/session mapping so retries cannot land in another task's checkout. If Herdr is unavailable, use an observable CLI fallback and record its PID/session instead of claiming it appeared in Herdr.

Source: [Herdr CLI reference](https://herdr.dev/docs/cli-reference/) and this repo's `skills/herdr/SKILL.md`.
