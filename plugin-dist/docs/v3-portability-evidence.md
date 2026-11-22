# Auto Office v3 — Orchestrator Portability Evidence (T6)

Scope: `issue-35#decision-8`, `issue-93#portability-demonstration`. This document is a sanitized,
public-facing summary of a live scenario. Raw transcripts, filesystem paths, account identifiers,
and full command logs are held privately outside this repository, per the v3 privacy/sanitization
policy; only opaque IDs and content hashes are reproduced here.

## Scenario

Three bounded, throwaway scratch git repositories (one per harness, never the office-skills repo)
each hosted a live interactive session of Claude Code, Codex CLI, and agy, running as an **Auto
Office v3 orchestrator** — not as a dispatched worker — inside a Herdr-managed pane. Each
orchestrator was given the identical scripted scenario and independently:

1. checked and marked the `auto-execution` spoke against its own run's durable state,
2. asked the human operator one live clarifying question through its own native conversational
   turn (not a scripted transcript) and waited for the real answer before proceeding,
3. dispatched a worker via Herdr (`herdr pane split` + `herdr pane run`) and continued taking
   further orchestrator input while the worker ran in the background,
4. recorded a completion event once the worker's output was independently observed,
5. wrote a checkpoint, deliberately overwrote the working file to simulate an interruption
   *without exiting or forking its own session/pane*, then restored from the checkpoint and
   compared before/after state,
6. recorded and validated a landing packet,
7. reported its own identity, invocation, and every artifact ID back verbatim.

The identity/effort readback rule was enforced by having the **external operator** (not the
harness) read each CLI's own rendered startup banner immediately after launch, before any prompt
was sent — because in two of the three harnesses the model itself has no visibility into its own
terminal-chrome banner and correctly said so rather than inventing a quote. The typed invocation
flags are therefore corroborated by a banner reading captured independently of the harness's own
self-report.

All three legs completed for real, on live infrastructure, with no fabricated step. Two failed
sub-attempts (an event-record call rejected for a schema-derivation mismatch, in two of the three
legs) are preserved in the record below rather than hidden, because the harnesses' own retries and
error-surfacing are part of the evidence.

## Quota probes taken immediately before dispatch

| Brand | Tightest window remaining (self-probe) |
|---|---|
| claude | 50% |
| codex | 50% |
| agy | 10% (below the 20% reserve floor; spending into reserve for this task only was explicitly authorized by the user in advance) |

Observation: the codex CLI's own in-banner quota reading diverged materially from the probe script
and dropped fast during the run (roughly 50% session / high-60s% weekly at probe time, down to
~29%/~17% by the banner at the end of one short scripted leg). The probe script and the harness's
own live meter are not reading the same cache; treat the probe as a pre-flight floor check, and the
harness's own banner as the authoritative in-session reading, consistent with the general rule that
effort/quota must be read from the harness, not the tool that launched it. agy's probe barely moved
(10.1% to 10.0%) across the one authorized run.

## Per-harness record

### Leg 1 — Claude (orchestrator role)

- Resolved executable + version: Claude Code CLI, `--version` → `2.1.272 (Claude Code)`.
- Invocation: `--model claude-sonnet-5 --effort high`. Banner readback (captured by the operator
  directly from the pane before prompting, not from the typed command): startup panel read
  **"Sonnet 5 with high effort"**; the persistent status line read **"Sonnet 5 (high)"**. Both
  match the typed invocation — no silent-medium route defect on this leg.
- Spoke receipt: `check-spoke --spoke auto-execution` → exit 2, `{"loaded": false, "marked_at":
  null, "spoke": "auto-execution"}`; then `mark-spoke --spoke auto-execution` → exit 0,
  `{"marked": "auto-execution", "phase": "intake"}`.
- Live question asked and answered through the harness's own conversational turn: *"T6 scratch
  scenario (claude leg): should the dispatched worker write 'alpha' or 'beta' into result.txt?
  Reply with exactly one of those two words."* → answered **"alpha"**; the orchestrator ended its
  turn and waited for the reply before dispatching anything.
- Herdr identity: pane id `w3N:pT`, agent name `orch-claude`, underlying session id
  `f843ca11-4056-4eef-a4ef-f135798f5c60` (independently corroborated by `herdr pane get` and the
  session's own env var).
- Worker dispatch: split to a sibling pane, ran `sleep 6 && echo 'alpha' > result.txt && echo
  T6_WORKER_DONE`; orchestrator reported the worker's pane id to the operator *before* the worker
  finished (remained responsive), then polled with `herdr pane wait-output --match T6_WORKER_DONE`
  (matched).
- Start-receipt id: `a90a929b-0ed9-4d9f-98ae-171aca5a3ea1`.
- Terminal event: two rejected attempts recorded verbatim (missing `session_id`/`family_id`; then
  an event-id that did not match the runtime's required derivation for the dispatch/sequence pair)
  before a successful call. Final event id `evt-27262f468dfae4ca`, sequence `1`,
  `observed_status=finish`, `terminal_classification=success`, `source=pane-content`.
- Checkpoint id `150f0647-9993-4c14-94f7-7b1aa62b4242` (serialized `2026-09-15T15:47:53Z`).
  Before/after: the working file was overwritten to the literal placeholder `INTERRUPTED` without
  the orchestrator exiting, restarting, or forking (same pane and session id throughout), then
  restored from the checkpoint back to `alpha` — content hash after restore matched the
  checkpoint's recorded evidence hash exactly.
- Landing id `ece1841b-cc80-4ef3-bd9c-4360195b14e4` (created `2026-09-15T15:48:48Z`).
  `validate-landing` → `{"valid": true}` (independently re-run by the operator, not just the
  harness's self-report).
- Evidence hashes: log `sha256:f8acff5953507d2798a54e6eb73a69f34aecf3ab455139f17b5f61fb286eb0e1`;
  worker output `sha256:b6a98d9ce9a2d9149288fa3df42d377c3e42737afdcdaf714e33c0a100b51060`;
  checkpoint packet `sha256:d300266e686d27d4de2e3cbf9f45788d2c5a0d3020422ca6ce5a1012b539e1b2`;
  landing packet `sha256:fd064fb4aab892a6b39e449545267d69df8d3bcf7f3aead7ef5ff34b9d9a6b15`;
  start-receipt packet `sha256:916e8263e59840ab55d482361dcda6c319d061418a49188d91f5ce239edb2036`.

### Leg 2 — Codex (orchestrator role)

- Resolved executable + version: Codex CLI, `--version` → `codex-cli 0.154.0`.
- Invocation: `-m gpt-5.6-luna -c model_reasoning_effort="medium"` (deliberately overriding the
  local config default of `xhigh`, to make the readback meaningful). Banner readback (captured by
  the operator from the pane before prompting): the model panel read **"gpt-5.6-luna medium"**,
  matching the override, not the config default — confirming the effort actually took effect
  rather than silently inheriting `xhigh`.
- Spoke receipt: same pattern as leg 1 — exit 2 then exit 0 on `check-spoke`/`mark-spoke`.
- Live question asked and answered: *"T6 scratch scenario (codex leg): should the dispatched
  worker write 'north' or 'south' into result.txt?..."* → answered **"north"**.
- Herdr identity: pane id `w3N:pV`, agent name `orch-codex`, underlying session id
  `01a0a5c2-053a-70c2-8f53-4bc59365e4b9`.
- Worker dispatch and remain-responsive behavior mirrored leg 1, using a distinct worker pane;
  wait-output matched.
- Start-receipt id: `a9658497-8f55-4e1c-89f2-f6e35ad1baf1`.
- Terminal event: one rejected attempt (event id did not match the required derivation for the
  dispatch/sequence pair) recorded verbatim, then a successful call. Final event id
  `evt-2e50357b50e46db9`, sequence `1`, same observed/terminal classification as leg 1.
- Checkpoint id `ca08d9cb-f918-4136-aec3-5046483c5281` (serialized `2026-09-15T15:54:56Z`).
  Before/after: `INTERRUPTED` → restored to `north` without the orchestrator exiting or forking.
- Landing id `648dfde8-21ee-42da-a691-ba690a51dab3` (created `2026-09-15T15:56:04Z`).
  `validate-landing` → `{"valid": true}` (re-run independently).
- Evidence hashes: log `sha256:08d5010bbff4b00daf7f5d5a54bbc7ec3b933576d013530c4f245d642dd191c0`;
  worker output `sha256:0de53ab3c043ea1f5a860e05a12b4319c6900d6df81c11c9894ed4103ab512b9`;
  checkpoint packet `sha256:99caa2ca5eb050c6894fd96125aa597675b3e918825987ba3d09f5ffc009ecbe`;
  landing packet `sha256:97b3d15e14606610e86122f264096d68f91f2765c747acc43b1c032e2cf00aff`;
  start-receipt packet `sha256:b496d376eb6d1e8bf6c795f56f73d1b9d8aed15fab8583aab2e1ec31562814d3`.
- Codex could not see its own startup banner from inside its own transcript either (same
  constraint observed on the Claude leg) and said so rather than inventing a quote; the banner
  reading above was captured by the external operator, per the readback rule.

### Leg 3 — agy (orchestrator role) — the one authorized reserve-floor run

- Resolved executable + version: agy CLI, `--version` → `1.2.3`.
- Invocation: `--model "Gemini 3.6 Flash (Low)"` (no `--effort` flag — the model name already
  encodes the tier for this brand, and mixing the two is not accepted for this model family).
  Banner readback (captured by the operator before prompting): the splash panel read **"Antigravity
  CLI 1.2.3"** and **"Gemini 3.6 Flash (Low)"**; the persistent status line read **"Gemini 3.6
  Flash · low"** — exact match to the intended tier.
- Spoke receipt: same pattern — exit 2 then exit 0.
- Live question asked and answered: *"T6 scratch scenario (agy leg): should the dispatched worker
  write 'east' or 'west' into result.txt?..."* → answered **"east"**.
- Herdr identity: pane id `w3N:pW`, agent name `orch-agy`, underlying session id
  `d51c9c69-9ab0-4e90-9872-c471e4de65de`.
- Liveness caveat honored: `herdr agent prompt --wait` reported `done` immediately after both the
  initial brief and the answer to the live question; per the known agy liveness defect (status
  reads do not reliably reflect real state), the operator did not trust either report and instead
  read the pane's actual rendered content each time. On the first prompt, the pane genuinely showed
  the orchestrator stopped at its own live question — a true idle, not a false one. On the second
  prompt, the pane showed the full remaining scenario (dispatch through final report) already
  complete by the time it was read, and every file on disk was independently re-verified afterward
  (worker output, checkpoint, landing, hashes) and matched the self-report exactly — so this
  particular `done` reading was accurate, not a repeat of the false-done pattern. A separately dated
  live reproduction of the false-done defect against this same agy account (a blocking `--wait`
  call returning settled while the pane still showed an active spinner) is already on file from an
  earlier dispatch in this run and is not re-derived here to avoid spending a second, unauthorized
  agy invocation.
- Worker dispatch and remain-responsive behavior mirrored the other two legs.
- Start-receipt id: `7c8b94dc-483e-454d-8a93-99646d3aa7cf`.
- Terminal event: recorded on the first attempt, no rejected retries. Event id
  `evt-5e68ea4ffec66a73`, sequence `1`, same observed/terminal classification as the other legs.
- Checkpoint id `ac5adb02-830a-487e-8db7-30a4263f73e4` (serialized `2026-09-15T15:58:50Z`).
  Before/after: `INTERRUPTED` → restored to `east` without exiting or forking.
- Landing id `ee4f7825-94f3-4594-a289-1e6bb866b6e9` (created `2026-09-15T15:59:05Z`).
  `validate-landing` → `{"valid": true}` (re-run independently).
- Evidence hashes: log `sha256:ec12787aa9080af1b3b53606c435bd51586a8308bba261eb1ccb0a434fd747fc`;
  worker output `sha256:80ea4fbde0e345a2aff8c388b8c1c45d2fa47c56e67631e7d56d224db12926b9`;
  checkpoint packet `sha256:38a94c30449e76e086f4acbe08edc8d04b90dcfb22580f42115030fe41ea65b6`;
  landing packet `sha256:80d3d87bc90985c87a4d5bbcd0490940f11f611272213c668995c98d1d8996c5`;
  start-receipt packet `sha256:8dd9badc5982a2696e60ef394a7530ff21df10e76587c785aec0e32979df3772`.
- Post-run probe: agy's tightest-window remaining moved from 10.1% to 10.0% for this single
  authorized run — negligible reserve consumption, and no second attempt was needed or made.

## Independent re-verification (not the harnesses' self-report)

For all three legs, the operator independently re-ran, outside any harness's own turn:
`validate-checkpoint` and `validate-landing` against the artifacts each orchestrator produced — all
six calls returned `{"valid": true}` — and recomputed every sha256 hash quoted above directly
against the files on disk.

One accurate, non-defect finding surfaced by this independent check: each terminal event above used
`--source pane-content`, a single-sample evidence source. The runtime's own
`completion-status` command classifies such an event as `"terminal": false` even though
`observed_status`/`terminal_classification` read `finish`/`success`, because `pane-content` is
correctly excluded from the trusted, corroborated sources the runtime requires before treating a
completion as durable. This is the runtime working as designed — the same discipline this task's
brief asks orchestrators to apply to harness status reads — not a defect in this evidence.

## Interruption/blocked outcome without losing the pane

Each leg's live user-question step put the orchestrator into a stopped, input-waiting state that
Herdr's own agent detection recognizes distinctly from mid-work `working` status. Each leg was
answered and continued in the *same* pane and *same* underlying session id it started with — no
pane was closed or recreated to recover, and no session was forked. Step 7 (overwrite-then-restore)
additionally exercised a destructive-state interruption on the same live pane/session, with an
explicit before/after comparison recorded per leg above.

## Result

All three required harnesses — Claude, Codex, and agy — completed the full orchestrator-role
scenario for real, with no deterministic fake standing in for a success-path step. No leg needed to
be reported as a blocked acceptance failure; the one thin-reserve brand (agy) was rehearsed on the
other two harnesses first and then run exactly once, as authorized.
