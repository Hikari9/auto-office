---
title: "Run fastlane Herdr reviewers unattended, and fixture PRs need the full run"
author: Rico
date: 2026-11-21
branch: dispatch-snapshot
cost: { minutes: 2, tokens: 47495 }
id: l-df9b1c49
scope: repo
status: active
src: agent
touches:
  - plugin-dist/scripts/validate.sh
  - scripts/validate.sh
  - src/office/verification_coverage.py
  - scripts/select_pr_tests.py
  - tests/v31/test_review_reroute.py
---

## Decided
Fastlane reviewers that run in a Herdr pane are launched so they run without stopping: claude with --permission-mode auto --add-dir /Users/rico/Git. Each reviewer writes its verdict to a file, and one shell wait on that file replaces polling. The fallback order when quota runs out is codex gpt-6-luna high, then opencode deepseek-v4.1-flash-free at max, then claude opus low.

## Tried and ruled out
- Claude in default or plan mode: it stops on read-only approval menus, and plan mode cannot write the verdict file.
- `opencode run --variant max`: that flag doesn't exist in `run`. Put the variant in the model as provider/model#variant.

## Watch out
- Any change to a conftest.py makes the PR gate refuse `--pr` with "Test configuration changed: run full validation explicitly". The pre-push hook runs the same gate, so a fixture PR can't be pushed until a full run, or until it is folded into the batch's one full run.
- The codex TUI shows "5h N% left" at startup. Check it before prompting.

In the code:
- herdr agent start <name> --kind claude --pane <id> -- --model opus --effort low --permission-mode auto --add-dir /Users/rico/Git
- opencode run --model 'opencode/deepseek-v4.1-flash-free#max' "<pointer>"
- scripts/select_pr_tests.py blocks on a conftest.py change
