# The run on a herdr-projects board card

The herdr-projects board ("wherdr") shows each Office run live on one **display-only card**. The
card is never delegated. It has no board worktree, plan, review or merge: Office owns its whole
pipeline (plan, routes, review and merge gates) exactly as without a board. The board only shows
it. Code: `src/office/board_card.py`. Tests: `tests/v31/test_board_card.py`.

## Which card

| How the run starts | Card |
|---|---|
| rico-mode created a card and started the pane with `OFFICE_BOARD_CARD=<id>` | that card |
| `office start --card <id>` | that card (wins over the variable) |
| no key, the board's CLI found (in herdr or any other shell, also a run started by hand) | Office adds its own card: `herdr-projects add "Office <run>: <goal>" --repo <repo>`, never `--delegate` |
| no key and no board CLI | no card; Office works exactly as before, and never calls the board |
| `--no-card`, `OFFICE_BOARD_CARD=off`, or config `board.card: off` | no card of Office's own (a given key is still used, except with `--no-card` or `off`) |

The key is read once, at `office start`, and kept in the run (`runs.landing_json.card`). Later
commands, background jobs and the wake supervisor all read it from there, not from the
environment. `office card link <id>` attaches a card to a run that is already going, and
`office card link --create` adds the run's own card.

**Never two cards for a run.** Only `office start` and `office card link --create` add a card.
Both work under a per-run lock, read the run again inside it, and first look for a card already
titled `Office <run>:` (`list --all --query`). A card found there is adopted, never added
again. That also covers a crash between the board's `add` and Office recording it.

The CLI is `HERDR_PROJECTS_BIN`, else `$HERDR_PROJECTS_DATA_DIR/bin/herdr-projects`, else
`~/.herdr-projects/data/bin/herdr-projects`, else `herdr-projects` on `PATH`. Under pytest only
`HERDR_PROJECTS_BIN` counts: the suite does not move HOME, and a test must never add a card to the
developer's real board.

## What the card shows

The card's state line (`herdr-projects report <card> --state … --msg … --progress …`) follows the
run:

| Run | Card |
|---|---|
| intake (no plan yet) | working · `Intake: <goal>` · 5% |
| plan submitted or in plan review | working · `Plan p<n> review r<k>: <verdict>` · 10% |
| a step only Rico can take: plan approval, product intake (SO38), plan questions, intake gap, a plan defect to redirect, merge-ready under end state `ask`, a waiver | **blocked** (kind `permission` for approvals, merges and waivers, `question` otherwise) · `<one-line reason> · answer in rico-mode` |
| executing | working · `Executing: a/n accepted, r running, b blocked · review T2 r1: RECHECK (1 high)` · 15–85% |
| every task accepted, `office land` next | working · `Merge-ready: landing` · 90% |
| closed | done · `Done: <landing>` |
| abandoned | error · `Abandoned: <reason>` |

Answers never come from the board. A blocked card says `answer in rico-mode`, and the answer
reaches Office the way it always has (rico-mode, then the orchestrator's `office approve …
--quote` or `office amend …`).

The card's **description** (`herdr-projects describe`) is the readable view, written by Office:

- the run line (phase, requirements and plan versions, end state) and the state line;
- the plan: its full text while it waits for approval, then its tasks and who authorized it;
- **Routes**: every executor, planner and reviewer dispatch as `harness/model@effort`;
- **Fallbacks**: each route chain with its reasons, for example
  `T1 code_reviewer: codex/luna@high → opencode/deepseek@max (usage_limit) → claude/opus@low (auth)`.
  Office keeps choosing routes; the card only shows them;
- **Reviews**: every plan and task review round with its verdict, its reviewer's route, the
  findings by severity, and one line per finding with its state;
- **Kept from the card**: text Office did not write. A rico-mode brief already on the card, or
  an edit made on the card later, is kept here verbatim and never dropped. (The board has no
  compare-and-swap, so an edit saved in the milliseconds between Office's read and write is the
  one case that can still be lost.)

The description is capped at 16k characters: the finding lines of the oldest rounds go first,
and a line says how many are in the run folder.

## When it updates

Every run event (`state.emit`) moves a per-run `card` cursor. New events queue one
`card_publish` outbox job (a queued one is reused), which renders from `runs.db` and sends only
what changed: an unchanged state sends no `report`, and an unchanged description no `describe`.
The cursor is read after each `office` command, after each job and on the root-wake supervisor
tick. Close and abandon publish the final state at once, since a closed run's jobs no longer
run.

A board failure never fails the run. It is recorded once per distinct error as
`card.publish_failed`, shown by `office card status`, and retried on the next event or with
`office card sync`.

## Notes

- Each `working` report restarts the card's elapsed time on the board.
- A board automation that fires `--when added` also fires for Office's own cards.
- rico-mode's side (adding the card and exporting `OFFICE_BOARD_CARD` before `office start`)
  lives in the rico-mode repo.
