# Root continuation: Herdr (#723) and native Claude Code (#940)

## Scope and safety contract

Office supports two distinct event-to-idle-root delivery transports: verified Claude/Herdr and
verified native Claude Code `Stop`/`asyncRewake`. Herdr's original transport is retained; the native
transport added by #940 does not use a Herdr pane. Both share the same authoritative SQLite
lifecycle ledger, coalesced receipt table and action signatures. It does not borrow `/goal` or any slash-goal runtime, create agents, restart dead orchestrators, or bypass the Office lifecycle. SQLite `runs.db`, including its committed `events`, tasks, reviews, authorizations and outbox, remains the only authority. The notification transport is a **receipt**, not a new task or an approval. Independent code/visual reviews, risk gates, scope leases, explicit user decisions, and the final merge boundary stay unchanged.

The standalone [silent-stall watchdog](../src/office/watchdog.py) handles stalled spinners and quiet reviewers. An independent slow watchdog loop (15-second cadence, default progress thresholds 10 minutes for executors and 15 minutes for reviewers) samples panes/worktrees, persists a deduplicated `watchdog.stall` Office event, and wakes the idle root through the **same event notification ledger**. The root completion wake-up does not wait for this expensive watchdog's longer detection threshold. Stall alerts do not revoke or retry any agent or reviewer automatically.

## Attach and turn behavior

From an already bound, verified **Claude agent in Herdr**, `office resume` arms wake by default
(#922). `--auto-wake` is still accepted:

```sh
office resume
# On the root's normal model turn, follow Office's "next:" and wait when nothing requires you.
# Check the current event and receipt counters at any time:
office status --json
# Stop future automatic prompts without deleting the durable receipts:
office resume --wake-off
```

The runtime checks `HERDR_PANE_ID` against the active root `session_bindings` row, queries `herdr agent list/get`, and insists on exactly one named Claude agent in that pane. An executor or reviewer, a stale/reused pane, an ambiguous agent, a headless shell, and a detached root **cannot** opt in. `--auto-wake` does not authorize a task, mint a reviewer verdict, or silently change the run's identity. Outside Herdr, `office resume` does not arm a native root unless the caller is inside a verified
Claude process with a bound `proc` session and the managed native hook is present. Missing transports
report unavailable rather than claiming that an event will wake the agent.

## Native Claude Code Stop / asyncRewake (#940)

Install the managed Stop hook (one-time per host/runtime installation), reload or approve Claude Code's
hook configuration when prompted, then resume the run **from within the Claude Code root**:

```sh
office install --only claude --item turn.stop
office resume
# When the root finishes the current turn, Claude's native Stop hook starts the watcher.
office status --json
# Disable both Herdr and native wake until an explicit re-arm:
office resume --wake-off
# Re-enable on a later turn:
office resume --auto-wake
```

The managed `Stop` command has `asyncRewake: true` and a 24-hour bound. It starts **no model**: its
background Python process watches Office's committed events and checks the same canonical action,
review and human-decision fences as Herdr. Only when the root is bound to that exact Claude process
and native session does the hook write a context instruction to stderr and exit **2**. Claude Code's
native hook runtime then resumes the original idle model session. A successful exit-2 notification
is a *delivered receipt*, not proof of observation or task completion. `office context` records
observation and later Office commands record separate activity receipts. If a worker, foreign
session, shell, uninstalled or disabled hook, changed binding, cancelled listener or terminal run
cannot be verified, the hook exits 0 without waking anything. A user prompt invalidates the old
idle listener; the next Stop re-arms it. The listener is nonblocking and singleton per run; no
cross-session prompt, duplicate wake loop, synthetic keepalive or fallback headless agent is used.

Native Claude Code is a supported **mechanism**, not an unconditional delivery guarantee: the
host may refuse new/changed hooks, terminate background hooks, or close/suspend the session.
`office status` shows whether the native path was armed and whether a listener is active. When
it is unavailable (or the listener has expired), use `office wait`/manual `office resume`;
no transport invents an acceptance or human approval. Codex, agy and other non-Herdr roots remain
manual until their own verified idle-wake adapter is implemented.

## Event-to-action flow

1. Workers, gates, reviewers and integration continue to write Office's authoritative transactional state and `events` as before.
2. A detached, *non-model* supervisor scans only committed event sequences every 0.5 seconds and records one unique `(run_id, event_seq)` intent for an actionable transition. Informational `launch.session`, `setup.done`, timeouts and `wait.*` are ignored. A burst is coalesced into **one** root notification. Event kinds only select candidates: the notifier validates fresh canonical `guide._action_signature` plus authoritative gate/finding/amendment status (or an active worker-stop/stall signal) before claiming **and immediately before sending**. Already settled events, duplicate guidance and pre-existing human-only blockers are recorded as `settled` without another model turn. A newly reached human-only decision may wake the root only to ask the user and stop—never to grant authority.
3. The notifier obtains a durable lease and checks the exact run binding. Herdr verifies one named Claude agent in an idle/done pane before sending a prompt; native Claude verifies the bound root process and Stop-hook session, then returns code 2 with a system reminder. A root actively being used or a changed binding is not retargeted. If it is working, the wake remains queued for idle without consuming a retry budget.
4. The prompt instructs the existing root to run `office --run <id> context`, then honor its next authorized action. **Only this successful root context call** marks `observed`. A later root-issued Office command changing the event ledger records `root_activity_observed` with its command and event sequence, **not** completion of the pending event's work. Only Office's authoritative task/gate state proves completion. Successful Herdr delivery alone records only `delivered`.
5. The root continues its own strategic loop through Office's existing dispatch/check/review/landing operations. No synthetic model turns, keepalives, or agent re-creation occur.

Under normal Herdr availability and low host load, the event-loop plus idle delivery should notify the root within **five seconds**. This is a **normal-conditions target**, not a guarantee across a hung Herdr CLI, a resource-saturated machine, a busy root or network outage. Inspect the last delivered event latency in `office status --json` at `root_wake.last_delivery_ms`; `root_wake.last_delivery` also exposes durable `delivered_at`, `observed_at`, and `root_activity_at` timestamps, with `root_activity_command` and `root_activity_event_seq`. The delivery clock begins at committed event creation; observation and generic root activity are distinct measurements, never proof of task completion.

## Ambiguity and recovery

Delivery outcome is **unknown** if Herdr leaves a prompt held, cannot prove it landed, or the supervisor crashes after claiming an intent. The receipt becomes `awaiting_attachment`, not a silent success and **not** an automatically repeated model prompt. Inspect the old root pane first (to avoid duplicate actions), then explicitly `office resume --auto-wake` in a verified root to rearm undelivered intents. Disabling root auto-wake preserves ledger history; manual `office status`, `office context`, and `office wait` always remain available.

Unsupported automatic wake mechanisms: Codex and agy Herdr roots, non-Claude/non-Herdr harness roots, suspended/closed terminals, native provider resume-by-session RPC, and operating-system wake after the Office host itself stops. These do not fall back to a fabricated success or shell injection; use Office's normal manual resume/wait path instead. Adding an adapter requires proving live root identity, idle-safe prompt delivery and an unambiguous receipt before expanding support.

## Cache behavior

Reusing an existing live session can preserve provider-side cached context while it remains valid, but the cache TTL or cost window is **never a liveness guarantee**. The supervisor doesn't prompt for the sake of keeping a cache warm, start model calls for telemetry, schedule artificial turns, or change lifecycle safety decisions to favor a cache hit. A cache miss follows exactly the same state-gated path.

## Verification

- `tests/v31/test_root_wake_723.py` tests opt-in identity fencing, real worker submission, event coalescing, independent `delivered`/`observed`/`root_activity_observed` receipts, a busy root, reattachment, ambiguous delivery, restart idempotence, and a detached background supervisor that wakes a fake Herdr root and triggers a scripted legal Office dispatch **within five seconds**.
- `tests/v31/test_wait.py` covers unchanged blocker deduplication, frozen agent spinner vs real worktree progress, and a running reviewer with no output.
- The simulated Herdr process is a real subprocess boundary, but **not a paid Claude/Herdr live pilot**. A live pilot must be run on a host with an authenticated Herdr + Claude root; if unavailable, the test evidence must not be presented as a live-provider result.

- `tests/v31/test_issue_940_non_herdr_wake.py` exercises the native `Stop` configuration,
  process/session ownership, event-to-exit-2 receipt, deduplication, `--wake-off`, root rebind,
  missing-hook fallback, observer receipt and no accidental worker wake. These are local fake
  sessions, not a live authenticated Claude Code desktop pilot.
