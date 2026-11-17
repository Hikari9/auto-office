---
title: "Claude reviewers get session-only answers to the add-directory trust prompt, never a pre-trust written to Claude's config"
author: Rico
date: 2026-11-17
branch: main
cost: { minutes: 15, tokens: 107794 }
id: l-e652bbce
scope: repo
status: active
src: agent
touches:
  - src/office/read_scope.py
  - src/office/dispatch.py
  - src/office/startup.py
  - tests/v31/test_reviewer_read_scope.py
  - tests/v31/test_herdr_agent_launch.py
---

## Decided
On 2026-11-17 (#1239) Claude reviewer panes stalled on Claude Code's "Adding a new directory ... Yes, add it" prompt. Claude Code shows it once for every --add-dir it has not trusted. Office now passes only the run's own state dir plus ~/.claude/rules and ~/.claude/skills. It answers "Yes, add it" only when the directory named is Office-owned for that run. That answer holds for the session only. Rico's rule: never trust the parent ~/.local/state/auto-office.

## Tried and ruled out
Pre-trusting each dispatch dir by writing projects["<dir>"].hasTrustDialogAccepted into ~/.claude.json, which would show no prompt at all. Ruled out because every live Claude session rewrites that file, so a concurrent write can clobber or corrupt it for all sessions.

## Watch out
- Trusting a directory covers everything below it, so trusting the state home would expose every run to every reviewer.
- Measured with a pty probe of the installed claude: ~/.claude/rules and ~/.claude/skills do not prompt (they sit under the trusted ~/.claude), while a run dir and the state home each prompt.
- The pane wraps the path mid-token. Rejoin the lines without spaces before comparing it.

In the code: read_scope.run_state_dir/add_dirs(output); dispatch.ADD_DIR_MARKER, _add_dir_target, _confirm_trust(owned=), _answer_startup_trust(owned=), MAX_TRUST_ANSWERS=4; tests/v31/test_issue1239_add_dir_trust.py.
