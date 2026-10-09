# AGY CLI: Gemini and other routed agents

Use `agy --help` and `agy models` at launch time. Native Gemini variants include effort in the **model slug** (`gemini-3.8-flash-medium`); do not split the known-good native Gemini slug into a base model plus `--effort`. For non-Gemini models through AGY, the exact display name may be required instead of the slug: confirm against the startup banner.

## Launch in a named worktree

Set `WT`, `AGY_MODEL`, and `BRIEF` to real values. Work from the target checkout as well as declaring `--add-dir`; that flag alone does not guarantee the intended cwd.

```bash
sh "$AUTO_OFFICE_LEAN_DIR/scripts/agent-preflight.sh" agy "$WT" "$EXPECTED_BRANCH"
(cd "$WT" && agy --model "$AGY_MODEL" --add-dir "$WT" \
  --print-timeout 45m --prompt="$(cat "$BRIEF")")
```

`--prompt=...` reliably binds the prompt, unlike misordered `--print` arguments that can produce an idle greeting with exit 0. Do not pipe the brief to stdin in ordinary print mode. `--print-timeout` defaults to 5 minutes, which is too short for many engineering tasks. On supported Gemini routes, effort is already encoded in the slug; do not add an unsupported `--effort` flag for other model families.

Headless write permissions may require additional approval. Prefer an interactive, observable Herdr agent if approval is needed; only use `--dangerously-skip-permissions` when the user has explicitly authorized unattended mutation in a truly isolated worktree. Do not silently relax the permission boundary.

## Readback and recovery

- Confirm the selected model banner and actual worktree diff. AGY's `idle`/`done` status can coexist with a working turn, a survey, or an unsubmitted prompt; use pane content and file changes as evidence.
- On a greeting or swallowed prompt, fix prompt transport and relaunch once. On a genuine quota stall, reroute instead of repeated blind retries.
- For a known ended conversation, inspect `agy --help` then use `--continue` or `--conversation <id>` with the same prompt/route rules; do not send a second writer to a live checkout.

Sources: [AGY headless CLI](https://www.antigravity.google/docs/cli/headless/) and this repo's `skills/agy-cli/SKILL.md`.
