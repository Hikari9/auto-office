---
name: herdr-close-panes
description: "Sweep Herdr panes that this same orchestrating pane spawned and that have finished, using the worktree-local herdr ledger. Use only when the user explicitly asks to close/sweep/clean up done Herdr panes, or when the herdr skill's spawn recipe tells you to. Requires HERDR_ENV=1 and the herdr skill already loaded."
---

# Herdr Close Panes

An on-demand sweep of the panes *this pane* spawned, not a listing sweep. It closes finished
children and reports which survivors want reuse or compaction. It never touches a pane this
orchestrator did not spawn, and it never touches a pane it spawned but that is still `working` or
`blocked`.

This skill assumes the `herdr` skill's ledger convention below is already in effect. If nothing
has ever called `herdr-ledger.mjs add`, there is nothing to sweep — that is not a failure.

## The ledger

One JSON object per line, one line per pane this orchestrator spawned:

```
$HERDR_LEDGER, else <git toplevel or cwd>/.herdr/ledger.jsonl
```

It lives in the orchestrator's own worktree, not in `/tmp` — a `git status`/`ls .herdr/` in that
worktree is enough to see what it's still holding open. Add `.herdr/` to that worktree's
`.gitignore`; it is run state, not a committed artifact.

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
NODE_SCRIPT=<this skill's directory>/scripts/herdr-ledger.mjs
```

## Recording a spawn (do this at spawn time, not later)

Right after `herdr agent start` succeeds and you have read back `session_id` (see the herdr
skill's spawn recipe), record the row in the same step:

```bash
"$NODE_SCRIPT" add --pane <pane-id> --agent <name> --kind <claude|codex|gemini|...> \
  --session <session_id-or-omit>
```

`orchestrator_pane_id` defaults to `$HERDR_PANE_ID` and `orchestrator_session_id` to
`$HERDR_SESSION_ID` — both already set in your own environment as the spawning pane. Only pass
`--orchestrator-pane`/`--orchestrator-session` explicitly when recording a spawn on behalf of a
different pane than the one you're running in.

An entry without `session_id` is incomplete, not merely terse: the session id is the only way back
into that agent after its pane closes. If it wasn't available yet, re-run `add` (it upserts by
`pane_id`) once a follow-up `herdr agent get` returns it.

## Making self-report reliable: every brief asks for one line back

Do not rely on remembering to sweep, and do not rely on the spawned agent remembering either.
Every brief you send to a spawned agent restates one short, literal instruction — the same way the
herdr skill's dispatch rule says every executor brief restates "no in-session subagents":

> Before you end your final turn, run exactly one command to report your own status into the
> ledger:
> ```bash
> "$NODE_SCRIPT" update --pane "$HERDR_PANE_ID" --status done --suggestion <closeable|reusable|compactable>
> ```
> Use `closeable` when your work is fully handed off and nothing about you needs to persist.
> Use `reusable` when the orchestrator may want to resume this same session for a follow-up round
> with no special handling needed first. Use `compactable` when you'd be resumable but your
> context is large enough that the orchestrator should compact before reusing you. When unsure,
> use `closeable` — it is the safe default; an orchestrator that actually wanted to reuse you reads
> your written report either way.

This is a command the spawned agent runs itself, in its own pane, with its own `$HERDR_PANE_ID` —
it works the same for any `kind` that can run a shell command, not just Claude Code. `update`
refuses to guess: passing no `--pane` and having no `$HERDR_PANE_ID` set is a hard error, not a
silent no-op, so a misconfigured pane fails loud instead of writing nothing.

**A self-report is a candidate, never proof by itself.** `sweep` (below) still cross-checks
`herdr agent list`/`herdr pane list` before closing anything — an agent that reported `done` and
then kept working (the same false-done failure mode the office's own pane hygiene hook was built
to catch) is not closed just because the ledger says so.

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
