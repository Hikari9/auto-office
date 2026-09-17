---
name: codex-cli
description: Directly invocable Auto Office v3 Codex CLI mechanics primitive. Use when a selected/explicit Codex harness dispatch needs safe model+effort invocation, worktree targeting, prompt transport, liveness/readback, resume safety, or Codex-specific failure attribution. Do not treat this primitive as a lifecycle office or as proof that the Codex adapter is proven.
---

# Codex CLI primitive

Load `../../adapters/seed/codex.yaml` and the current local adapter override. The shipped seed is `valid-unverified`; normal mutable/final-gate authority requires promotion by v3 conformance/evidence or explicit user override.

Always pass explicit model and canonical effort — `codex exec` has no `--effort` flag; effort is a config key. An invocation carrying only `-m` silently inherits `model_reasoning_effort` from `~/.codex/config.toml`, and an unrecognised value is not rejected at launch (the banner prints it and the session runs anyway). Read the effort back out of the launch banner; never infer it from the command you meant to type.

```bash
codex exec --yolo -m <model> -c model_reasoning_effort="<effort>" \
  --cd "<abs worktree path>" "$(cat <brief>)" < /dev/null 2>&1
# run_in_background: true, timeout: 600000
```

- `--yolo` removes the sandbox and every approval stop; the prompt's stated blast-radius ceiling is the entire safety boundary.
- `< /dev/null` is mandatory — `codex exec` reads stdin even with a positional prompt, and without this it blocks forever in the background.
- Pass an explicit `timeout: 600000` on every backgrounded dispatch. The Bash tool's default (`120000`) kills a codex run mid-work — long enough to look like real work, too short for a build plus test suite. Do not misattribute a killed-at-~180s run to an external process cap or a context limit; measure with a throwaway sleep loop before believing that.
- Never pipe through `tail`/`head` — both buffer the entire stream until exit, so the harness's output file reads empty the whole run.

## A pane and a clean environment are not alternatives

A `codex exec` launched from a login shell can die before it runs, on an `_load_nvm`/FUNCNEST fault
inherited from the user's shell profile. The usual mitigation is `env -i`. **`env -i` also strips
`HERDR_ENV` and removes `herdr` from `PATH`** — so the fix for the profile fault silently defeats
the Herdr-pane precondition in the top-level `SKILL.md`, and any hook keyed on `HERDR_ENV` that
would have blocked a bare CLI launch. The dispatch succeeds, does correct work, and is invisible:
absent from `herdr agent list`, from the pane ledger, and therefore from closeout pane accounting,
which closes only panes it finds in the ledger.

`echo $HERDR_ENV` prints the parent's value and empty under an `env -i` child, where `herdr` is
also off `PATH` — so such a dispatch never appears in `herdr agent list`.

Carry the environment through instead of discarding it, and prefer the spawner that records the pane:

```bash
scripts/office_spawn.sh --pane-id <pane> --agent-name <name> …   # pane-hosted, in the ledger
env -i HOME="$HOME" PATH="$PATH" TERM=dumb HERDR_ENV="$HERDR_ENV" codex exec …  # bare, if required
```

An `env -i` allow-list is a decision about which variables matter; dropping `HERDR_ENV` is the
difference between a dispatch the user can watch and one they cannot. If you must drop it, say so
in the route notice so the invisibility is a stated cost. A headless one-shot also cannot be
resumed, which `auto-review` asks for explicitly: round 2 is meant to retain round 1's uncertainty,
and `codex exec` has no session to resume into.

## Quota

Probe before dispatch: `python3 ../../scripts/codex-usage.py --json` (bare for human-readable, `--percent` for routing math only). Reads `~/.codex/auth.json`; reports the tighter of the 5-hour/weekly windows. Exit `2` means unknown, not low. Full contract in `../../references/quota-probe.md`.

## Liveness — do not trust `pgrep -f "codex exec"` or `comm == "codex"` alone

`pgrep -f "codex exec …"` matches the harness's wrapper shell (the Bash tool runs it inside `/bin/zsh -c …`), which can outlive its codex child by tens of minutes. A hit is not evidence of life.

`ps -eo pid,comm | awk '$2=="codex"'` is not universal either — on an nvm-installed CLI the process shows as `node`, so this filter can read empty while the executor is actively working. Safe only in the positive direction: a hit means alive, a miss means nothing.

**Match on the worktree path instead**, and require two independent signals before declaring death:

```bash
pgrep -f "<abs worktree path>" | wc -l    # 0 => dead; >0 => alive
```

the process count must be zero **and** the output file's size must be static across two samples. Never use `pgrep -fl` on a codex dispatch — the entire brief lives in the command line and gets dumped into context.

## Resume

`resume` takes a different flag set from `exec` — `--cd` is rejected, and it inherits the *caller's* cwd, not the session's:

```bash
codex exec resume --dangerously-bypass-approvals-and-sandbox --model <model> \
  -c model_reasoning_effort="<effort>" <session-id> "<continuation prompt>" < /dev/null 2>&1
# run_in_background: true, timeout: 600000
```

Read the resume banner's `workdir:` line before letting it run — a resume issued from a planner sitting in a different repo lands a full-authority writer in the wrong tree, and there is no flag to correct it in place. If wrong, kill it (`pkill -f "<session-id>"`, confirm with `pgrep -f "<session-id>" | wc -l` => 0) and relaunch a fresh `exec --cd` instead of continuing.

`-c model_reasoning_effort=` is required on resume too — a resume inherits the session's context but **not** its effort, and silently degrades to the config default without it.

Attribute invocation/stdio bugs and false liveness reads to adapter, resume/workdir inheritance to harness, quota walls to quota/account, and logical implementation defects to the producer model/role only when evidence supports it.
