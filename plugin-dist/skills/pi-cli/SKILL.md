---
name: pi-cli
description: Explicit Pi CLI primitive for validated provider/model and thinking effort, stdin prompt transport, isolated worktrees, liveness/readback, and safe recovery. Not an independent reviewer or Office lifecycle.
---

# Pi CLI primitive

Read the authoritative adapters/seed/pi.yaml and any local overlay. Check the requested provider/model using pi --offline --list-models and pi auth check --model for that pinned model. Unknown provider, custom model and unsupported effort are launch failures, not permission to substitute another route.

- Headless worker: from the exact isolated worktree, use pi --print --no-approve --no-extensions --no-mcp --model "{model}" --thinking "{effort}" --no-session. Pass the brief by stdin, not a positional message or a shell-interpolated command.
- Map Office effort none to Pi thinking off; low/medium/high/xhigh/max map to their identically named values. Accepted flags alone do NOT prove effort effectiveness or that the provider served the requested model.
- --approve trusts project resources; it is not a sandbox flag. Never add it to a worktree sourced from untrusted code. --no-approve, --no-extensions and --no-mcp prevent loading untrusted project extensions and MCP connections. AGENTS.md and CLAUDE.md may still load.
- Herdr interactive form: same trust restrictions, with --model and --thinking but without --print or --no-session. Record pane ID, process cwd and session liveness; an interactive pane is not a headless resume contract.
- Capture process exit, stdout/stderr and observable worktree changes. No output or no writes after exit 0 is an invocation failure, not acceptance. Apply a finite launcher wall cap; Pi has no verified native headless timeout.
- An actual auth, quota, wrong-model, transport or liveness failure is attributed to the correct layer and handed back to the caller for explicit recovery. No verified reviewer/vision profile exists, so never dispatch Pi as an independent code reviewer.

The packaged runtime manifest resolves skills/pi-cli/SKILL.md. Do not auto-install Pi, alter user settings, invent unsupported flags or implement a second lifecycle.
