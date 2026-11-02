---
name: opencode-cli
description: Explicit OpenCode CLI primitive for safe selected model/effort invocation, sandboxed working-directory targeting, prompt transport, liveness, and recovery. Not an Auto Office lifecycle or automatic reviewer.
---

# OpenCode CLI primitive

Read the authoritative adapters/seed/opencode.yaml and current local overlay. Check the exact pinned provider/model slug with opencode models immediately before launch, and check current role policy and free-model consent when Office routing is available. A model name or requested variant that the adapter cannot verify must not silently fall back.

- Headless worker: execute from the already isolated worktree, passing the brief as ONE argument, not through a shell interpolation. Adapter argv is [run, --model, "{model}#{effort}"]; the effort variant is model-specific and an unsupported variant fails fast. Do not add --auto or grant permissions by default.
- Herdr pane: the interactive form is opencode mini --model "{model}#{effort}" (TTY required; the adapter probed the #variant suffix on 2026-10-14, but mini --help lists only provider/model, so re-probe before relying on it); the Office reviewer profile instead uses the separate isolated office-reviewer agent with --standalone, --agent and a restricted OPENCODE_CONFIG_DIR. Never assume a builder profile is read-only.
- CWD: launch with the process cwd set to the pre-created worktree; the CLI has no Office cwd flag. Record the absolute path, git HEAD, model, effort, invocation form and whether the CLI or Herdr actually launched.
- Output and liveness: the launcher supervises stdout/exit state and a bounded wall cap (the CLI has no intrinsic timeout). A pane's Herdr ledger/status and cwd identity are required before reporting progress. An empty reply or exit 0 alone is not proof of completed work.
- Recovery: distinguish missing CLI, missing model/auth, variant unavailable, quota/rate limit, permission prompt and transport failure. Disclose actual rejection and the next eligible route; do not silently swap an explicitly pinned model. Resume a pane via the supported mini --session identifier only when confirmed, never invent a headless resume API.
- For independent review, use the adapter's reviewer profile (one exact reply-file edit allowed, all source edits denied) rather than worker mode. No unverified vision claim and no automatic free-model opt-in.

The packaged CLI runtime manifest resolves skills/opencode-cli/SKILL.md. This primitive never installs OpenCode, mutates global permissions or implements another routing engine.
