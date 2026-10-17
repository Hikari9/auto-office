# opencode / muse-spark-1.3-contributor-free live Office smoke — 2026-10-14

Live Auto Office evidence for the opencode route, from dogfood run `99fdcdf3`.
This records **launch evidence only**. Exit 0, a landed prompt, or a working
pane is **launch evidence, not model conformance**: Office reads no model
identity back from opencode, so nothing here shows which model actually served
a turn. Only **`medium` effort was dispatched**; nothing here claims any other
effort level works, changes behavior, or selects a distinct model variant, and
nothing here claims which model served the turn.

## How Office launches opencode (installed user adapter at dispatch time)

Installed adapter: user overlay `/Users/rico/.config/auto-office/adapters/opencode.yaml`
(fronting `opencode v2.0.29` at `/Users/rico/.opencode/bin/opencode`). The seed
adapter in this worktree base (`adapters/seed/opencode.yaml`, T1) maps effort
onto the `--model provider/model#variant` suffix and declares a `mini --model`
pane form, but it was **not** the installed adapter for these launches, so no
`#variant` claim is made here.

- Rendered headless worker form (prompt as one trailing argv element, no shell,
  no `--auto` in any dispatched form):
  `/Users/rico/.opencode/bin/opencode run --model opencode/muse-spark-1.3-contributor-free "<prompt>"`
  (effort is dropped at every level in the installed overlay: `effort_mapping`
  is all-null, so every effort renders this same argv).
- Rendered Herdr pane form (from `Dd82d6f33` `launch.json`
  `rendered_launches.herdr.argv`; brief delivered separately by
  `herdr agent prompt` as a brief pointer):
  `herdr agent start office-dd82d6f33 --kind opencode --pane w1M:p9 -- /Users/rico/.local/state/auto-office/worktrees/99fdcdf3/T3`
  (`herdr_kind: opencode`, `harness_version: 2.0.29`, `prompt_transport: herdr agent prompt pointer`).
  The pane itself starts the bare interactive opencode TUI in the worktree (the
  installed overlay's `interactive.argv` is `["{cwd}"]`: the root command takes
  no `--model` flag, so the routed model pins the headless form only).
- Preflight (installed overlay): `model_check: [/Users/rico/.opencode/bin/opencode, models]`
  (the pinned slug must be listed; every check must exit 0). No auth gate: `opencode auth list`
  exits 0 while reporting `No authenticated integrations` even as free models serve.
  Verified 2026-10-14: `opencode models` lists
  `opencode/muse-spark-1.3-contributor-free`; `opencode auth list` reports
  `No authenticated integrations`.
- Route triple (from `Dd82d6f33` `packet.json`): `opencode@2/muse-spark-1.3-contributor-free@medium`
  (user override `--as opencode/muse-spark-1.3-contributor-free@medium`).

## Headless smoke (throwaway repo)

`office harness smoke opencode --model opencode/muse-spark-1.3-contributor-free --effort medium`
ran 2026-10-14 in a throwaway git repo (`office-smoke-*/repo`, README + `git init`
fixture; the smoke agent acts on no run). Record in
`harness-smoke.jsonl` (`~/.local/share/auto-office/harness-smoke.jsonl`):

- `at: 2026-10-14T14:33:49Z`, `effort: medium`, `evidence: launch`, `kind: worker`,
  `result: launched`, `exit_code: 0`, `duration_s: 2.56`, `output_bytes: 6`,
  `timed_out: false`, `trust: unchanged`, `unsafe_flags: []`, `version: 2.0.29`,
  `catalog_row: true`, `origin: user`.
- Stored argv (prompt redacted by the recorder):
  `["/Users/rico/.opencode/bin/opencode", "run", "--model", "opencode/muse-spark-1.3-contributor-free", "[PROMPT REDACTED]"]`.
- `stderr_tail`: `> build · muse-spark-1.3-contributor-free`.

Exit/classification: exit 0 is process-launch evidence (`launched`), not model
conformance and not a trust or benchmark claim.

## Herdr pane dispatch (herdr_kind opencode)

- Dispatch `Dd82d6f33` (task T3), pane `w1M:p9`, agent `office-dd82d6f33`,
  `herdr_kind: opencode`, worktree `/Users/rico/.local/state/auto-office/worktrees/99fdcdf3/T3`
  (`panes.jsonl`: `w1M:p9` / `office-dd82d6f33` / kind `opencode`, status `working`).
- The prompt landed: the pane agent is live under Herdr
  (`herdr agent get office-dd82d6f33`: `agent: opencode`, `agent_status: working`,
  `interactive_ready: true`, terminal title `OC | Executing brief.md tasks`), and
  the worker acted on the brief — it ran the headless smoke above, inspected
  `launch.json`/`packet.json`/the installed adapter, and wrote this record
  (the file you are reading) inside the dispatch worktree.
- Exit/classification: no exit yet — the pane was still `working` when this
  record was written. A running pane is launch evidence only, same as exit 0:
  it shows the prompt landed and the worker acted, not which model served.

## What is not claimed

- No effort other than `medium` was dispatched by Office here (the installed
  overlay renders the same argv for every effort; the `#variant` effort mapping
  exists only in the not-yet-installed seed adapter and is unproven live).
- No model identity: neither the smoke log nor the pane transcript reads a
  model identity back from opencode, so this record does not state that
  `muse-spark-1.3-contributor-free` served either turn — only that the pinned
  slug was listed at preflight and passed on the headless command line.
- Quota stays unknown for opencode routes (no probe; never treated as unlimited).

## Follow-ups

- Install the T1 seed adapter so `#variant` effort mapping and the `mini --model`
  pane form become the live forms; then re-smoke `xhigh` (the seed row effort)
  and confirm the variant suffix appears in the recorded argv.
- Record the rendered headless argv in each dispatch's `launch.json` (currently
  only the Herdr pane argv is stored there), so future smoke records can quote a
  captured process line instead of the adapter rendering plus the smoke log.
