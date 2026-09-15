---
name: claude-cli
description: Directly invocable Auto Office v3 Claude CLI mechanics primitive. Use when a selected/explicit Claude harness dispatch needs safe background/in-session launch behavior, prompt transport, worktree isolation, tool allowlisting, liveness, remote-control/resume handling, or Claude-specific failure attribution. Do not treat this primitive as a lifecycle office or proof of adapter promotion.
---

# Claude CLI primitive

Load `../../adapters/seed/claude.yaml` plus any current local override. The shipped seed is `valid-unverified`.

```bash
echo "<brief>" | SHELL=/bin/bash claude --bg --remote-control "[ROLE] <repo> — <task>" \
  --model <model> --effort <effort> --add-dir "<pre-created worktree>" \
  --allowedTools "Read Write Edit Grep Glob Bash(git *) <task-specific tools>"
```

- **The prompt must be piped on stdin.** A positional prompt returns `idle — send a prompt to start`: the agent registers but never begins, and checking only the launch id (not `state`) makes you wait forever on an agent never given work.
- **The allowlist carries no MCP tools unless you name them.** `--allowedTools` grants file/shell access and nothing else — every MCP server the parent can reach (Rock, Basecamp, Sheets, any connector) is absent unless listed explicitly as `mcp__<server>__<tool>`. A dispatch that never granted a tool reads back as "delegate can't do this system," when the truth is the launch never carried it. Read the real tool names off your own tool list before launching; a guessed name grants nothing and fails silently.
- **Always pass `--effort` explicitly — omitting it is a silent downgrade, not a default.** `~/.claude/settings.json` carries a top-level `effortLevel` *and* a per-model `modelSettings.<model>.effortLevel` that overrides it, so a flagless launch inherits the model's own configured tier rather than the account default or the routed one. Measured 2026-09-15: `modelSettings["claude-sonnet-5"].effortLevel` was `medium` while the top-level was `high`, so `claude --model claude-sonnet-5` ran a routed `high` executor at `medium` for all 105 turns. Nothing in the launch, `claude agents --json`, or the pane reports the mismatch. This is the Claude analogue of codex inheriting `model_reasoning_effort` from `~/.codex/config.toml`.
- **Read the effort back off the session, never off the command you meant to type.** `claude agents --json` has no effort field. The durable readback is the session transcript: `grep -o '"effort":"[a-z]*"' ~/.claude/projects/<slug>/<session-id>.jsonl | sort -u` — one value, and it must equal the routed tier. Do this after the delegate's first turn, while the dispatch is still cheap to relaunch. A hand-composed launch (herdr `agent start ... -- <args>`, or any wrapper that is not `office_spawn.sh`) drops whatever adapter argv it does not repeat, so it is exactly the path that needs this check.
- **`--add-dir` is not checkout isolation.** A dispatched agent can still `git checkout` the shared tree it was pointed at before adding its own worktree, moving the branch under you. Pre-create the agent's worktree and `--add-dir` that (or dispatch from a tree you are not using yourself); after any dispatch returns, check `git rev-parse --abbrev-ref HEAD` before writing anything.
- **Never pre-emptively fall back to `--in-session`** because CLI "was blocked before" — that's stale evidence. Attempt the CLI launch and read the actual result; only a real refusal in the current run justifies the fallback.
- **`--effort` is required whenever the route names one.** Omitting it is not an error — the session starts and silently runs at medium — so after `agent start` read the pane banner and confirm it names both the intended model and the intended effort (e.g. `Sonnet 5 with high effort`) before prompting. Route identity here is model **and** effort, so a silently-medium dispatch falsifies the route notice and every telemetry row attributed to it.

## Quota

Probe before dispatch: `python3 ../../scripts/claude-usage.py --json` (bare for human-readable, `--percent` for routing math only). Reads the Keychain-stored OAuth token; reports the tighter of the 5-hour/7-day windows. Exit `2` means unknown, not low. Full contract in `../../references/quota-probe.md`.

## The fork gotcha

`--resume <id> --bg` against a live session — busy or blocked — forks unconditionally: it returns a new session id and leaves the original running untouched, so your steering message never reaches it and you now have two writers on one tree. Never use it to steer a live writer.

- For a **blocked question** (a raised menu or free-text prompt), answer in place via `claude attach` rather than forking — never combine digit-selection and text in one send, and never lead a free-text answer with `/` (it gets intercepted as a slash command).
- If a fork already happened: keep the fork (it carries the transcript plus your message), `claude stop` the original, and confirm via `claude agents --json` that exactly one writer remains before continuing.
- `/remote-control` sent into a session already at an input prompt enables Remote Control without forking; never send it into a busy/working turn.

## Liveness — `claude agents --json` is the only read

`pgrep -f "<worktree path>"` cannot see a Claude background agent — its command line is `claude bg-spare --bg-spare /tmp/cc-daemon-501/<id>/spare/....sock`, and the worktree never appears there. A miss is not evidence of death, it's evidence you searched for a string that was never going to be present. `ls /tmp/cc-daemon-501` from the Bash tool is equally blind — the tool runs with a private `/tmp`. Two independent-looking checks that share the one root cause (neither can observe a bg agent) agreeing with each other means nothing.

The authoritative read is the `claude agents --json` row for that id — its `status`/`state`. Corroborate only with signals that actually can see it: `ps -eo pid,command | grep claude` matching that row's `pid`, and artifact mtimes moving in its tree. `claude logs <id>` works only for background sessions, and failing with `connect ENOENT .../control.sock` means the log channel is unreachable, not that the agent is dead.

Attribute invocation/stdio bugs (idle-prompt, missing MCP tools, fork-on-resume) to adapter, checkout/liveness-observability gaps to harness, and logical implementation defects to the producer model/role only when evidence supports it.
