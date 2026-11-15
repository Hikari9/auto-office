---
title: Claude reviewers need Bash named in --tools and a settings-level sandbox fence
author: Rico
date: 2026-11-15
branch: fix/claude-reviewer-shell
cost: { minutes: 49, tokens: 357847 }
id: l-6d7d48f1
scope: repo
status: active
src: agent
touches:
  - tests/v31/test_issue_1186_reviewer_shell.py
  - plugin-dist/adapters/seed/claude.yaml
  - plugin-dist/docs/office-cli-architecture.md
  - CHANGELOG.md
  - adapters/seed/claude.yaml
  - docs/office-cli-architecture.md
  - src/office/_version.py
  - src/office/adapters.py
  - src/office/read_scope.py
  - src/office/review_submit.py
---

## Decided
Claude reviewer/vision launches name their tools explicitly and, where Claude Code's OS sandbox runs, pass a strict sandbox through `--settings`. Bash then runs read-only builtins, targeted test runners and the exact `office review submit` (the only unsandboxed command). Hosts without a sandbox keep the submit-only shell (#1186, PR #1187).

## Tried and ruled out
- `--tools default` under `--restricted`: Bash stays removed. Only an explicit list (`Read,Grep,Glob,Write,Bash,...`) brings it back.
- Relying on CLI `--disallowedTools "Edit(//path/**)"` to fence sandboxed writes: test code still wrote files in cwd. Only `sandbox.filesystem.denyWrite` in `--settings` blocked it.
- Granting opencode reviewers an exact bash allow: its deny-all agent hides the bash tool entirely, and opencode has no OS sandbox, so test runners there are unsafe.

## Watch out
- Argv-string tests missed the original bug. Check the real tool list from the `init` event of `claude -p --output-format stream-json --verbose` with a nonexistent model: no model reply, no cost.
- The data home and state home are `--add-dir`s, so they are sandbox-writable unless denied. Deny beats allow, so a root holding the dispatch dir is fenced by denying its other children.
- `excludedCommands` needs every command in a compound call to match, so `submit && x` stays sandboxed.
- The PR selector ignores multi-name imports (`from office import a, b`). Import a changed module on its own line, or the gate blocks with "No tests mapped".
- Editing `adapters/seed/` or `docs/` needs `scripts/build_plugin_distribution.py --sync`, or the ecosystem check fails on stale copies.

In the code: adapters/seed/claude.yaml reviewer/vision argv; src/office/read_scope.py claude_sandbox_settings, sandbox_write_denials; src/office/review_submit.py REVIEWER_TEST_BASH, REVIEWER_BASH_DENY; tests/v31/test_issue_1186_reviewer_shell.py (live test gated by OFFICE_LIVE_CLAUDE=1).
