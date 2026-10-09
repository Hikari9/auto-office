# Codex CLI: direct worker or reviewer

Use Codex when its exact installed model and effort are confirmed. Start with `codex --version`, `codex exec --help`, and `codex exec resume --help`. The active CLI is authoritative; a model name in a routing table is not proof of local access.

## Launch a bounded worker

Prepare an exclusive Git worktree and a brief file. Worktree ownership is separate from the CLI sandbox. From a trusted shell, set `WT`, `MODEL`, `EFFORT`, and `BRIEF` to real values:

```bash
sh <skill-dir>/scripts/agent-preflight.sh codex "$WT" "$EXPECTED_BRANCH"
codex exec --sandbox workspace-write --cd "$WT" --model "$MODEL" \
  -c "model_reasoning_effort=\"$EFFORT\"" "$(cat "$BRIEF")" < /dev/null
```

For read-only independent review use `--sandbox read-only`. Do not use `--yolo`, `--dangerously-bypass-approvals-and-sandbox`, or `danger-full-access` without explicit authority and a separately justified isolation boundary. Provide the branch, allowed files, expected output and check commands in the brief. Redirect output to a distinct file and record the PID when running in the background; never mistake a wrapper process for the agent.

**Prompt and route traps:** Passing a positional prompt does not stop `codex exec` from reading stdin; `< /dev/null` prevents a background hang. Pass both model and `model_reasoning_effort` explicitly. Confirm the startup banner/session output reflects the chosen model and effort; `-m` alone inherits the user's effort setting. Never infer completion from process names or an empty output file.

## Resume an ended session

```bash
(cd "$WT" && codex exec resume --model "$MODEL" \
  -c "model_reasoning_effort=\"$EFFORT\"" "$SESSION_ID" "$CONTINUATION" < /dev/null)
```

Unlike the initial `exec`, resume inherits the caller's cwd: verify the resumed banner's `workdir` before any write. Never resume a live writer into a second session. If session identity is missing, start fresh from its preserved worktree and commits. Check the resulting diff and tests before acceptance.

Source: [Codex CLI reference](https://learn.chatgpt.com/docs/cli/reference) and this repo's `skills/codex-cli/SKILL.md`.
