# Auto Office

One adaptive engineering lifecycle, driven through a single `office` CLI. An orchestrator agent
decides strategy; routed specialist agents plan, implement, and independently review; the runtime
owns every mechanical step behind one transactional state store (`runs.db`).

- **Distribution:** `auto-office` · **Python package:** `office` · **Executable:** `office`
- **Version:** 3.2.4 (`office --version` prints the exact identity; source checkouts report a
  PEP 440 local version such as `3.2.4+g1a2b3c4d5e6f`)

## Install

```bash
uv tool install auto-office            # or: uv tool install 'auto-office[visual]' for browser capture
office install                         # idempotent harness hooks (Claude Code, Gemini CLI), runtime registration
office doctor                          # verify the install, hooks, pinned runtimes, known harness defects
```

From a checkout: `uv tool install --editable .` (or `uv venv && uv pip install -e '.[visual,test]'`).

`uv tool install` provides the `office` command; `office install` then adds the managed hooks and registers
the runtime. If the skill runs before either step, it stops at its install check and asks you to approve the
install, and it offers an upgrade when the installed `office` release differs from the skill's `VERSION`.
The visual extra installs Playwright and uses the local Chrome; without it, visual gates report
`CAPTURE_BLOCKED` rather than passing.

`office doctor --fix` refreshes managed hooks, restores exact legacy runtimes from the local
installation source's git history, retires obsolete Office entries from Gemini's unused legacy
hook file, and converts Hermes scalar hook commands to lists of command mappings. Config edits
are backed up first and preserve unrelated settings; Hermes still requires its own hook approval.
For a checkout reinstall, see [Deploy (this repo)](#deploy-this-repo).

## Deploy (this repo)

To put the current checkout on your PATH as the installed `office`:

```bash
uv tool install --force --reinstall --no-cache "auto-office[visual] @ <checkout>"
office install
office doctor                          # expect: install: matches its source <checkout> @ <commit>
```

- `--reinstall --no-cache`: without both, uv can reuse a cached wheel built from an older commit of the same
  version. The install succeeds but lacks your changes, and `office --version` looks current.
- `[visual]`: a reinstall without the extra removes Playwright, and visual gates report `CAPTURE_BLOCKED`.
- `office install` re-registers the runtime and refreshes the managed hooks.
- Verify with `office doctor`. `install: matches` means the installed runtime files equal the checkout. `install: STALE`
  lists the files that differ and means the reinstall did not take effect.

**Cross-release installs (MAJOR.MINOR changes).** Runs are pinned to a release line, and each registered runtime in
`runtimes/<version>.json` points at a Python environment. The uv tool venv is overwritten by the new install, so
runs pinned to the old release would start running new code. Before installing the new release, pin the old one in
its own venv from a checkout at that release:

```bash
uv venv ~/.local/share/auto-office/pinned/<old-version>
uv pip install --python ~/.local/share/auto-office/pinned/<old-version>/bin/python "auto-office[visual] @ <checkout at old release>"
~/.local/share/auto-office/pinned/<old-version>/bin/python -c "from office import frontdoor; frontdoor.register_current()"
```

Then run the deploy commands above. `office doctor` prints `on <line>: N active run(s) ok` per pinned line, or
`RUNTIME MISSING` when no runtime is registered for that line. A PATCH release on the same line needs no pinning.
Moving a run across lines is `office upgrade`.

## Use

```text
office start "<goal>"                 create a run; queues the planner when policy requires one
office resume [run]                   bind this session to a run and show where it stands
office status                         what matters now, ending with the next legal action
office dispatch <task>... [--parallel]
office submit                         planner/executor: submit a plan or work
office amend <scope> -- "<delta>"     ordinary, --contract, or --requirements --quote "<user words>"
office ack <amendment-id>             worker: the delivered amendment is applied
office close                          after acceptance and landing (--handoff <pr>, --abandon "<why>")

office list | inspect [run|task|gate|evidence|events|route] [id] | doctor | prune [-f]
office approve <plan|merge|X1|trust <route>|waive <T2:gate>> --quote "<user words>"
```

Every result is a few lines ending in `next:`. `--verbose`, `--json`, and `office inspect` show the
detail default output hides. `SKILL.md` is the orchestrator brief; executors, planners, and
reviewers receive runtime-generated briefs and never write JSON or receipts.

## How it works

- **State:** SQLite `runs.db` (WAL) is the only lifecycle authority. One semantic transition is one
  transaction; outside work (agent launches, checks, reviews, captures) is queued in an outbox in
  the same transaction and run by short-lived `office _job` processes, so a crash delays work but
  never loses it. JSON files under a run directory are generated read-only views.
- **Version pinning:** every run and every packet carries the `office_version` that created it. The
  front door re-executes a command under the registered runtime for that version, or stops with an
  actionable error; a run never migrates in place.
- **Submission:** `office submit` captures the worktree exactly as it is (dirty edits included) as an
  immutable revision, deduplicates replays, runs deterministic checks, then dispatches independent
  code and (when acceptance involves the UI) visual review on that same revision.
- **Convergence:** verdicts are `PASS | CHANGES_REQUIRED | PLAN_DEFECT | BRIEF_DEFECT | UNAVAILABLE`.
  Findings go straight to the owning worker. Round budgets, a no-progress stop, and one escalation
  bound every loop; nothing unavailable, stale, skipped, or malformed ever counts as PASS.
- **Rolling plan review:** after a first `CHANGES_REQUIRED`, the amended plan launches safe work at
  once while the re-review runs concurrently; a `PLAN_DEFECT` blocks its scope until an independent
  reviewer clears it.
- **Visual evidence:** deterministic Playwright capture (viewports and states from acceptance),
  direct DOM measurements against an approved reference, evidence status
  `COMPARABLE | INVALID_COMPARISON | NOT_APPLICABLE | CAPTURE_BLOCKED`, and judgment only by a
  route whose exact harness/model/effort passed an image-sensitive conformance probe.

Design and contracts: [`docs/v31-implementation.md`](docs/v31-implementation.md),
[`docs/v31-rolling-review-gates.md`](docs/v31-rolling-review-gates.md), [`CONTEXT.md`](CONTEXT.md).

## New in 3.2

- `office submit` prints a plan diagram: parallel waves, what each task stacks on, a route preview
  with its why, and the checkpoint chain through landing. Amendments print only the delta.
- Each task gets a draft PR stacked on GitHub (dependents target their parent's branch). Executors
  push work in progress; the runtime pushes the reviewed revision at submit, posts one-line
  verdicts, and marks the PR ready on acceptance. `office start --no-prs` keeps work local.
- The plan's `end_state:` (asked at intake) decides how far `office land` goes: ask, preview
  deploy, merge, or merge + prod deploy and verify. `office land --detect` proposes deploy commands.
  `office land --rebase` moves an accepted run onto a default branch that moved since start.
- Runs pinned to 3.1 keep 3.1 behavior; they run under their registered 3.1 runtime.

## Migrating from 3.0

- Runs started by 3.0 stay on 3.0. `office list` shows them as `3.0 legacy`; `office resume <id>`
  points at the exact retained runtime (materialized from git history for the run's pinned
  commit). `scripts/office_runtime.py` forwards a 3.0 state directory there automatically.
- `scripts/` is the retained 3.0 helper surface. It was slated for removal in 3.2.0 and stays through
  3.2.x; its removal is a separate change. `office raw <subcommand>` reaches it with a deprecation warning; every call is recorded
  (`office doctor` lists remaining consumers). Legacy helpers can never write a 3.1 run.
- New runs use the installed runtime (3.2) by default. Roll new runs back without touching existing ones by setting
  `runtime: {new_runs: "3.0"}` in `~/.config/auto-office/config.yaml`.
- The default quota reserve is now 5% (the balanced-routing money band stays 20%).

## Development

```bash
uv venv && uv pip install -e '.[visual,test]'
.venv/bin/python -m pytest -n 2              # default: unit smoke run
.venv/bin/python -m pytest -n 2 --all        # everything: unit, integration, legacy, slow
.venv/bin/python -m pytest -n 2 -m integration   # just the integration group
.venv/bin/python -m pytest -n 2 -m legacy        # just the 3.0 legacy group
python3 scripts/check_ecosystem.py
```

Tests are tiered by marker. The default `pytest -n 2` is the unit smoke run (in-process, no repo, subprocess, browser
or fake harness). Use it while iterating and on per-PR runs. `integration`, `legacy` (3.0 `scripts/` surface) and `slow`
(dogfood, real `verify.sh` gates) tests stay in the repo and are deselected by default. Run everything once, at the final
integration merge, with `pytest -n 2 --all`. `-m integration` and `-m legacy` run just those groups, and any explicit `-m`
expression overrides the default deselection. Tests that use the `env` fixture are marked `integration` automatically.

There is no remote CI. Validation runs locally as a pre-push hook; enable it once per clone:

```bash
git config core.hooksPath .githooks   # pre-push: PR guard + scripts/validate.sh
scripts/validate.sh                   # run by hand; VALIDATE_BUILD=1 also builds the wheel
```

Tests run every agent through scripted fake harness binaries in isolated data and state homes;
they never touch `~/.local` or a real model.
