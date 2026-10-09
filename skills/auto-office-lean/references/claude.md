# Claude CLI: background or interactive agent

Check `claude --version`, `claude --help`, and `claude agents --help` against the installed CLI before launching. Use an exclusive worktree. `--add-dir` is additional workspace access, **not** a substitute for starting in the correct checkout.

## Start a background agent

Set `WT`, `MODEL`, `EFFORT`, `BRIEF`, and `TASK_LABEL` to real values. The repository's verified background-agent recipe sends the brief via stdin:

```bash
sh <skill-dir>/scripts/agent-preflight.sh claude "$WT" "$EXPECTED_BRANCH"
(cd "$WT" && SHELL=/bin/bash claude --bg --remote-control "$TASK_LABEL" \
  --model "$MODEL" --effort "$EFFORT" --add-dir "$WT" \
  --allowedTools "Read Write Edit Grep Glob Bash(git *)" < "$BRIEF")
```

Scope `--allowedTools` to the actual task. Explicitly name any required MCP tools after checking their current names; this allowlist does not inherit the orchestrator's connector permissions. Do not enable unrestricted tool permissions by default. An interactive Herdr Claude pane is preferable if an approval dialog needs human action.

**Background traps:** A positional initial prompt can leave `--bg` registered but idle: stdin delivery is intentional. Always pin `--effort`; per-model settings can silently override omitted effort. After the first turn, check the model/effort actually used and `claude agents --json` for liveness. `pgrep` by worktree path is not reliable for Claude's background daemon.

## Continue without duplicate writers

Read `claude agents --json` and the session identity. Use `claude attach` to answer a blocked live agent rather than `--resume <id> --bg`, which can fork another writer. For an ended session, inspect the installed resume syntax before use, then verify cwd, model and effort again. Treat external-import and folder-trust dialogs as startup/permission failures, never as a producer review verdict.

Source: this repo's `skills/claude-cli/SKILL.md`.
