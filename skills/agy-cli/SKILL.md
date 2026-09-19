---
name: agy-cli
description: Directly invocable Auto Office v3 Agy CLI mechanics primitive. Use when a selected/explicit Agy harness dispatch needs exact launch flag ordering, dynamic model discovery, model-dependent effort handling, workspace pinning, timeout/quota-stall diagnosis, prompt transport, recovery, or Agy-specific failure attribution. Do not treat this primitive as a separate lifecycle office or proof of adapter promotion.
---

# Agy CLI primitive

Load `../../adapters/seed/agy.yaml` plus current local override. The shipped seed is `valid-unverified`.

```bash
agy --dangerously-skip-permissions --print-timeout 45m \
  --model "<exact display name>" --add-dir "<abs worktree path>" \
  --prompt="$(cat <brief>)"
```

- `--print-timeout 45m` — defaults to 5m; always raise it or the run dies mid-task.
- `--model` — resolve from local `agy models` at dispatch; agy publishes no `latest` alias, so a hardcoded slug is pinned to the day it was written. **For a Claude/gpt-oss model routed through agy, pass the display name from the second column, not the slug from the first.** Verified 2026-09-12 on agy 1.2.2: `--model claude-sonnet-4-6` (the slug) is accepted without error and silently falls back to the account default; `--model "Claude Sonnet 4.6 (Thinking)"` selects correctly. There is no unknown-model error, so the only way to catch this is to read the startup banner's model line before prompting — treat a banner mismatch as adapter invocation failure and restart rather than prompting the wrong route.
  For a **native Gemini** model, the opposite convention verified correct: pass the exact combined
  model+effort slug from `agy models`' first column as a single `--model` value (e.g.
  `--model gemini-3.8-flash-medium`), with **no separate `--effort` flag**. Verified 2026-09-19 on
  agy 1.2.7: this launched correctly (banner confirmed "Gemini 3.8 Flash · medium") and the
  session went on to correctly complete real work. Splitting it into `--model gemini-3.8-flash
  --effort medium` is the claude/codex convention and is a strong suspect for why earlier Gemini
  rows were marked `dispatchable: false` under routing defect 3fe80434 — that defect's own
  original evidence, though, was a Claude-routed model, so treat the fix as verified per-slug
  (see `catalog/seed.yaml`'s per-row notes), not as a blanket rule across every Gemini row yet.
- **Prefer `--prompt=<value>` (the `=` form) over `--print <value>`.** `--print` must be the last flag immediately before the prompt or the prompt is silently swallowed and you get a greeting/banner back with exit 0 — that is invocation failure, not a routing signal; nothing was done. The `=` form binds the value to the flag and makes ordering irrelevant.
- **Stdin piping does not work.** The prompt must be bound to `--print`/`--prompt=`, never piped.
- `--add-dir` does not reliably select the workspace by itself — pin the absolute workspace root (and forbid the scratch dir) in the prompt text itself.
- `--effort low|medium|high` is **Gemini-only**. Passing it with a Claude or gpt-oss slug hard-fails the launch (`--effort is not supported for model "<slug>"`). Probe the model's effort support before assuming a house effort tier applies; if the slug doesn't support it, say so rather than substituting a tier.
- After a start call returns, confirm the prompt actually landed before counting the task as in flight. Observed 2026-09-14: a prompt sent immediately after `agent start` returned did not land at all — agy's readiness semantics differ from codex's, and its reported `idle` does not mean "ready for input" the way codex's does. The receipt is an observed transition to `working` plus real output appearing in the pane, never the start call's return value.

## Liveness — do not trust agy's reported status alone

Observed 2026-09-14: after a re-prompt, `herdr agent get wf` reported `idle` while the pane simultaneously showed `Searching...` and `esc to cancel` — the agent was demonstrably working. For agy, the harness status field is not a liveness signal. Reading `idle` proves nothing — not readiness, not completion, not death. The receipt is the pane content, never the status field. An orchestrator that polls agy status alone will both dispatch into a busy agent (believing it idle) and declare a working agent finished (believing it done). This is the agy analogue of `pgrep -f "codex exec"` matching the wrapper shell instead of the process — a green status reading is not proof of the state it claims.

## Quota and recovery

Probe before dispatch: `python3 ../../scripts/agy-usage.py --json` (bare for human-readable, `--percent` for routing math only). Live OAuth read against the CloudCode quota endpoint; exit `2` means unknown, not low. Full contract in `../../references/quota-probe.md`.

A stall after a few narration lines with nothing in `git status`/`git log` is quota, not slowness — confirm before believing an external cause.

- Clarifying-question stall or focused correction: `agy --continue` or `--conversation <id>`, same flag-ordering rules apply.
- Greeting/swallowed prompt: fix the flag form (prefer `--prompt=`) and relaunch; nothing was done.
- Quota stall: relaunch fresh or fall back to another brand for the remainder.
- **Feedback-survey stall:** agy interrupts a turn with an interactive survey ("How's the CLI experience so far? [1] Good [2] Fine [3] Bad [0] Skip"). Observed 2026-09-12 on 1.2.2: it appears mid-turn, the agent stops having written nothing, and the harness reports `done` rather than `blocked` (or cycles `idle`/`working`), so a status poll reads as success. Dismiss the survey specifically with `send-keys 0` (Skip), then re-prompt explicitly ("nothing was written, resume from where you left off") — dismissing the modal does not restore the task, the agent has lost it and needs the full re-prompt, it does not resume on its own. Confirm progress against the worktree with `git status --porcelain`, never against the reported status.

Launch as a background task with no pipes on stdout (never `tail`/`head` — both buffer until exit and defeat live tailing); read only the tail of the output file after completion. Never edit the tree it is writing to while it runs.

Diagnose a greeting/swallowed prompt or a banner model mismatch as adapter invocation failure, an `--effort` hard-fail as adapter/invocation, a status field that disagrees with the pane (idle-while-working, or a survey stall unreported as blocked) as adapter false-positive/false-negative liveness, and narration-with-no-work under exhausted allowance as quota/account when evidence confirms it.
