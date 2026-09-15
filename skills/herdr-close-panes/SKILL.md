---
name: herdr-close-panes
description: "Sweep Herdr panes that this same orchestrating pane spawned and that have finished, using the shared auto-office pane ledger. Use only when the user explicitly asks to close/sweep/clean up done Herdr panes, or when the herdr skill's spawn recipe tells you to. Requires HERDR_ENV=1 and the herdr skill already loaded."
---

# Herdr Close Panes

An on-demand sweep of the panes *this pane* spawned, not a listing sweep. It closes finished
children and reports which survivors want reuse or compaction. It never touches a pane this
orchestrator did not spawn, and it never touches a pane it spawned but that is still `working` or
`blocked`.

This skill assumes the `herdr` skill's ledger convention below is already in effect. If nothing
has ever called `herdr-ledger.mjs add`, there is nothing to sweep — that is not a failure.

## The ledger

This skill only *reads and sweeps* the ledger. Recording a spawn belongs to the `herdr` skill,
which owns dispatch; see its spawn recipe. One JSON object per line, one line per spawned pane:

```
$HERDR_LEDGER, else $OFFICE_PANE_LEDGER, else /tmp/office/panes.jsonl
```

This is the same ledger `scripts/office_spawn.sh` writes and
`scripts/hooks/close_finished_panes.mjs` sweeps — one file, not a private copy. It is deliberately
shared rather than worktree-local: a run's agents sit in several different worktrees, and a
per-worktree ledger would show each agent only its own spawns, so panes spawned from another
worktree would never be swept by anyone.

Fields: `pane_id`, `agent`, `kind`, `session_id`, `worktree`, `spawned_at`,
`orchestrator_pane_id`, `orchestrator_session_id`, `status`
(`working|idle|blocked|done|gone|unknown`), `suggestion`
(`null|closeable|reusable|compactable`), `note`, `closed`, `updated_at`.

`orchestrator_pane_id` is the ownership boundary. `herdr pane list` also shows the user's own
panes and other sessions' — it is never the candidate set. Only a ledger row whose
`orchestrator_pane_id` equals *this* pane's own `$HERDR_PANE_ID` is ever a candidate, which is
exactly what makes this "close panes from the currently orchestrated agent invoking it" instead of
a blanket sweep.

A helper script does the reading, matching, and atomic rewrite so no caller hand-rolls jq/python
against a file another process might be writing at the same time:

```bash
NODE_SCRIPT=<the herdr skill's directory>/scripts/herdr-ledger.mjs
```

## What this skill does NOT do

Recording a spawn, and the brief line that makes a spawned agent self-report, both belong to the
`herdr` skill's spawn recipe. They happen at dispatch time, in the dispatching pane. This skill is
the sweep half only: it reads rows someone else wrote and closes the finished ones.

## Sweeping: `herdr-ledger.mjs sweep`

Run this any time you want to reclaim finished panes right now — after reading a report, at a
natural checkpoint, or right before closeout:

```bash
"$NODE_SCRIPT" sweep
```

For each ledger row owned by this pane (`orchestrator_pane_id == $HERDR_PANE_ID`):

- **Closes** a row whose live status (cross-checked against `herdr agent list`) is `done`, `gone`
  (the agent has disappeared — `agent_not_found`), or another terminal status, **or** whose
  self-reported `suggestion` is `closeable` and whose live status is `idle`/`done`. Removes it from
  the ledger on a successful close.
- **Never closes** `working` or `blocked`, regardless of what the ledger says — a self-report is a
  candidate, not a guarantee, and `blocked` is a question to surface, not a pane to tidy away.
- **Leaves open and reports** any row whose suggestion is `reusable` or `compactable`, so you can
  decide: resume it as-is, or compact it first (`herdr agent prompt <name> "/compact"` for an
  interactive Claude/Codex pane) before sending the next brief.
- **Never touches** a row this pane does not own, or any pane absent from the ledger entirely —
  the listing is never the candidate set, only the ledger is.

Output is one JSON object: `closed` (what it reclaimed), `kept_open_with_suggestions` (panes still
open that flagged themselves reusable/compactable). Use `--dry-run` to preview without closing
anything — it reports what *would* close, and leaves every row and every pane untouched.

```bash
"$NODE_SCRIPT" sweep --dry-run
```

## Reliability without a hook, and with one

The self-report line above works everywhere `herdr` and a shell exist, independent of harness. For
a Claude Code orchestrator specifically, a `Stop` hook is the same backstop-not-substitute pattern
the herdr skill's own pane-hygiene hook already uses: closing/reporting explicitly is the primary
mechanism, a hook is for the one you forgot. If you want that belt-and-suspenders layer, ask
whether one should be wired into `~/.claude/settings.json` — this skill does not install one for
you, the same way the office pane-hygiene hook is opt-in and never a side effect of loading a
skill.

## Safety

- Refuses to run at all without `$HERDR_PANE_ID` set — there is no "sweep everything" mode.
- Never derives candidates from `herdr pane list`/`agent list` directly; those also list the
  user's own panes and other sessions'.
- A pane that moved (its live `session_id` no longer matches the ledger's) is treated as `unknown`
  and left alone, not closed — the same as a stale ledger entry.
- `--dry-run` before a first real sweep in an unfamiliar worktree is cheap insurance.
