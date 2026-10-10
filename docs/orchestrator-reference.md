# Orchestrator operating reference

Detail the root `SKILL.md` links to so the hub stays within its line budget. Authority order when anything here disagrees:
`MANIFESTO.md`, then the runtime (`office ... next:` lines, `docs/review-convergence.md`), then this page. If the runtime
refuses something this page allows, the runtime is right; fix this page.

## Install and update

- `office --version` not found: stop, ask the user to approve installing it, never install silently. On approval:
  `uv tool install "<skill directory>"` (`pipx install` if `uv` is missing), then `office install` (managed harness hooks and
  runtime registration, with a backup of each config) and `office doctor`.
- Release (the part before any `+`) differs from the directory's `VERSION`: tell the user and offer
  `uv tool install --force --reinstall "<skill directory>"`, then `office install`. Runs already started keep their pinned runtime.
- Same release: a wheel reports only its release, so later fixes are invisible to `--version`. Run `office doctor`; on
  `install: STALE`, tell the user and offer the same reinstall.
- At intake, run `office update --check`. If it reports an update, ask in the next native question round. A failed or
  offline check is informational.

## Dispatch recovery

A dispatch failure is a recovery checkpoint, not permission to abandon the run. Inspect the launch notice and
`office inspect task <T> --verbose`. Each dispatch's `launch` lines show the argv Office actually rendered for the Herdr
attempt and any headless fallback, with prompt transport, adapter hash and harness version. Prompts and credential values are
redacted there and in the private `launch.json`. When Office names a failed Herdr pane, read it yourself with `herdr pane read <pane>`
(or the saved `pane-tail.txt` once Office closed it) to find the startup dialog, dead harness, quota wall, or other blocker.
Office answers a folder-trust dialog only for worktrees and dispatch directories it created. Never approve hook trust, other
directories, credentials, irreversible actions, or user authority on the user's behalf. Close a pane only by the id Office
names, never by matching screen text, and never your own (`$HERDR_PANE_ID`). Then follow the printed `next:`: re-prompt a live
pane, `office resume`, `office rerun <task> --resume|--fresh`, `office dispatch <task> --reroute`, or the documented
external/manual launch. Stop only when those are exhausted or a user decision is required.

## Waiting, questions, stalls

- `office wait` exits 0 (act), 3 (stall), 5 (an agent asked a question), 124 (nothing new). Key on the exit code.
- Exit 5 prints a `question:` line (dispatch, pane, question, options, answer command). Decide planning, scope, ordering and
  test detail yourself: amend the contract first if the answer changes it, then `office answer <task|dispatch> <n>` (a number
  presses that option in a selection widget; `office prompt` types text, which a widget ignores) or
  `office answer <task|dispatch> -- "<text>"`. Take it to the user only when it hints at requirements, authority, or an
  irreversible or external action.
- `office status` makes one `herdr agent list` call and prints a `blocker:` line for a live pane herdr reports `blocked` with no
  recorded question. `office wait` records it; status records nothing.
- An executor idle 60s without submitting (or whose process died) is a stall: `office wait` exits 3, and the stall line names the
  dispatch, any refused-submit reason, its `pane-tail.txt` and the next command. Fix the blocker, then
  `office prompt T2 -- "<message>"` or follow that command. A worker refused as lease-lost, superseded-dispatch or
  task-paused is done: never prompt it to retry.
- Check suites share a host-wide cap (`verification.check_concurrency`). A quota wall blocks the worker without a relaunch;
  rerun after the reset or with `--as`. A check or test-runner timeout while host load exceeds twice the CPU count is
  UNAVAILABLE, not a failure: `office resume`.
- A task `checks:` command must install what its tests import (e.g. `uv run --extra visual ...`). A suite that skips every test
  exits 5 and fails the gate.
- Never run commands inside a task worktree yourself: tools leave files (`uv run` writes `uv.lock`) and `office submit` then
  refuses them as out of scope. Reproduce a check in a scratch copy.

## Planning inline

Run the four self-review lenses (`skills/office-submit`) over the plan and write what they surface into tasks' `accept:`
criteria. Plan the seams, not the internals: `scope:` is an ownership envelope (module or domain dirs plus their tests); name
exact files only where tasks collide or depend. Append-only registries several tasks touch (gate manifests, endpoint/grant lists,
policy maps, shared mocks) go under each task's `shared:`. Tasks that must land together share a `lane:`; lanes sharing an
outcome, a `converge:`; acceptance needing another lane's result, `accept_needs: T2`; a risky composition,
`integration_risk: high` (each adds one integrated review).

## Herdr agents

Inside Herdr, Office starts each dispatch as a real interactive agent in a pane beside yours and confirms the brief pointer
landed. A pane closes itself once its result is accepted, after saving `pane-final.txt`; a failed end keeps it open.
`office dismiss <T2|dispatch|--all>` closes kept panes; `OFFICE_KEEP_PANES=1` on `office dispatch` keeps them for debugging.

- A `launch` notice in `office status` means the pane agent never started (the dispatch ran headless) or the prompt never landed
  (re-prompt it). When startup fails before Herdr registers an agent, Office keeps the pane, snapshots its last screen, names
  recognized startup interstitials and prints the pane id.
- Office presses Enter for a prompt left typed but unsubmitted. A notice saying it is still unsubmitted means
  `herdr pane send-keys <pane> Enter`, not a re-prompt, which would send it twice.
- To message a live worker or reviewer, run `office prompt <T2|dispatch> -- "<message>"`: it sends with `herdr agent prompt`,
  confirms it landed and presses Enter for one left typed. Never use `herdr pane run` or `pane send-text` on an agent pane;
  Claude takes their Enter as part of the paste and leaves the text unsubmitted.
- A headless worker (`process-fallback`, no pane) cannot be typed to: `office prompt` queues the message ("queued: ... runs
  headless; it sees this on its next office command"). It is not an amendment and needs no ack. A headless fallback shows in
  `office status` and `office wait` as "runs headless (herdr fallback): <why>". A `rerun --resume` that falls back headless
  resumes the recorded session when the adapter declares `headless_resume_argv`, else it says it started fresh.
- Claude's "Allow external CLAUDE.md file imports?" dialog is named in that notice and never answered by Office;
  `office doctor` warns when CLAUDE.md imports files outside the repo.
- To relaunch by hand: `office revoke T1`, then `office dispatch T1 --external` (plus `--as` for another model); it prints the
  `herdr pane run`, `herdr agent start` and `herdr agent prompt` commands. A prompt has landed when the agent reports `working`
  or its pane shows a running turn. agy reads `idle` mid-turn: judge an agy pane by its footer and `git status`.

## Landing detail

- If the default branch moved after `office start`, run `office land --rebase` first: it re-composes the accepted work onto the new
  head and re-runs the run checks and one review of the rebase. A conflict refuses and prints the steps to compose by hand. When
  run checks fail and the default branch moved past the compose base, Office does this rebase itself once per new head.
- Office rewrites only the block above the PR body's `<!-- office:pr ... -->` line; put criteria that report data below it.
- With task PRs off (local, no GitHub, `--no-prs`), push the integration branch it names, open a PR with `Closes #<issue>`, and
  `office close --handoff <pr-url>`. `office close --abandon "<reason>"` stops early; nothing is deleted until `office prune -f`.
- Landed through a PR Office did not open: `office close --landed-externally <merged-pr-url>`; if that merge lacks an accepted
  revision, ask the user and add `--quote "<words>"`.
- Deploys run in a fresh checkout of the landed commit. Each deploy and verify step reports its cwd (`deploy ok: <cmd> (cwd <path>)`),
  and a failure names the same cwd, so a path a command expects is checked against that checkout.
- `office land --detect` warns when a deploy command names a repo path the fresh checkout lacks (gitignored or untracked, e.g. `.env`
  or `myaccount/.env` passed to a sourced script). Fix it by listing the path under `deploy.env_files` in the config: Office copies each
  listed file into the deploy checkout with its mode preserved, never stages or commits it, and refuses an entry outside the repo.

## Historical material

Everything under `references/OFFICE-SKILLS-V3-*.md`, `docs/v3-*.md` and the 3.0 spokes (`skills/auto-planning`,
`auto-execution`, `auto-review`, `auto-verification`, `auto-loop`) is 3.0-era reference. It explains where rules came from. It is
not operational policy for 3.1+ runs, which follow `office status` and `docs/review-convergence.md`.
