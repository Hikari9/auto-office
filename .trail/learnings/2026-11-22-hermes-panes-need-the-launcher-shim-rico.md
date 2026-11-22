---
title: Hermes panes need the launcher shim to see through forwarding wrappers; a stale user override caused the headless no-work runs
author: Rico
date: 2026-11-22
branch: fix/1321-hermes-launch
id: l-901c4f9c
scope: repo
status: active
src: agent
touches:
  - src/office/hermes_access.py
  - src/office/harnesscmd.py
---

## Decided
Hermes pane starts timed out because Office's launcher shim resolver read only one shell wrapper. The installer's PATH hermes is a pure forwarding wrapper (exec <path> "$@") in front of the git checkout's wrapper, which runs python3 -I -c. With no shim, Herdr's hermes agent kind never detected the process. The resolver now follows forwarding wrappers (bounded, loop-safe).
Headless Hermes runs that exited with no work came from a stale bootstrap user override with override_seed: true and a stdin prompt form, not from the seed adapter. The override was retired on the host.

## Tried and ruled out
Pane geometry and a Herdr 0.9.5 screen-manifest mismatch. With the shim on PATH, herdr agent start --kind hermes reports idle and interactive_ready within seconds, even in a 2-row pane. Adding --query-file - to the seed is unnecessary because the seed already passes --oneshot=<prompt>.

## Watch out
A user adapter with override_seed: true silently shadows seed fixes. Check ~/.config/auto-office/adapters first when a harness misbehaves after a seed fix shipped. A debug repro that runs herdr agent start --kind hermes without Office's shim always times out, so it proves nothing about Office.

In the code: src/office/hermes_access.py resolve_launcher and write_launcher_shim; src/office/harnesscmd.py _answered (smoke needs a READY line, JSON-aware); proof via a scratch run with OFFICE_STATE_HOME and XDG_DATA_HOME set to a temp dir and the worktree .venv/bin first on PATH.
