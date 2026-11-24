# Changelog

One line or short list per version, newest first. The version itself lives only in `src/office/_version.py` (see `AGENTS.md`).

## 3.31.14

The `office` CLI runs natively on Windows (#915, rebased from #916). New `office.portable` replaces `fcntl`, `killpg`, `os.kill(pid, 0)` (which kills on Windows), `start_new_session`, `os.uname` and `os.getloadavg`; binary-safe `os.open` flags; README documents the git install (the PyPI `auto-office` is a different project), Python pinning and Windows notes; D142-7 superseded. Windows then gets the rest of the lifecycle: a Toolhelp process table and a process-parameters working-directory listing let route probes and discovery-trial recovery attribute and confirm workers (recovery falls back instead of blocking); route probes run in kill-on-close job objects; npm `.cmd` shims (`codex`, `claude`, `opencode`) launch by name; `office` re-executes in UTF-8 mode and git output decodes as UTF-8; CRLF probe and untracked-file output is normalized; read-only git objects no longer stop worktree removal; replacing an open file retries; `cmd.exe`'s "is not recognized" maps to exit 127; scope and env-file paths reject drive-anchored entries; `tzdata` is a Windows dependency. The test suite runs natively on Windows (fake `#!` harnesses get console launchers, `HOME` mirrors to `USERPROFILE`, `core.autocrlf` is off); POSIX-only tests skip with a stated reason. On Linux, discovery-trial recovery no longer fails closed on every host: a process hiding its `/proc/<pid>/cwd` is skipped when it belongs to another account (init, a system daemon) or started before the dispatch began (`systemd --user`, `(sd-pam)`); any other that hides it still blocks. Four tests that only passed on a particular host now pass anywhere: the sandbox-profile test reads the real profile builder, the Hydrogen seed test pins `agy` 1.x, the Playwright doctor test expects Linux's `--with-deps`, and the real Claude/Codex plugin-install tests launch npm `.cmd` shims by full path. `scripts/office_monitor.py` no longer probes liveness with `os.kill(pid, 0)` (a Ctrl+C on Windows). `install_hooks.sh` configures the harnesses under the `HOME` it is given (Windows Python resolves `~` from `USERPROFILE`, so it edited the real user's settings when a caller pointed `HOME` elsewhere), writes native `C:/...` paths, and shell-quotes each hook command (`C:/Program Files/nodejs/node` split at the space). On Windows, a reviewer's Claude Code permission rules name paths in the POSIX form Claude Code matches (`/c/Users/...`; a `C:\...` rule never matched, so the read and write fence did not apply), and plan checks, land verify and deploy commands, worktree setup and the visual dev server run under Git for Windows' bash with Git's tools on PATH (cmd.exe could not run `scripts/validate.sh --pr`, `&&` chains or `VAR=1 cmd`; it remains the fallback without Git), each check in a job object so a timeout reaches its grandchildren. On every OS, `office disposition|prompt|answer ... -- <text>` works on Python 3.11 and 3.12, whose argparse leaves the words after `--` unparsed (it was a usage error), and a missing browser's `evidence_blocked` remedy names the same Playwright install as `office doctor` (`--with-deps` on Linux). Also on Windows: a plan check that runs `python3` gets a shim to the real `python` when PATH has none (Windows installs ship no `python3`, and WindowsApps' one opens the Store); `scripts/validate.sh` uses the venv's `Scripts/python.exe` there and strips CRLF from the PR test selection (a Windows-written selection collected no tests), and `select_pr_tests.py` writes LF (the scoped-gate digest pins follow); a codex transcript is matched to its brief through the JSON-escaped path (a Windows path's backslashes are doubled in JSONL, so session-id backfill and external-reviewer replies never found it); a benchmark snapshot file no longer has a `:` in its name; and deploy rollback archives copy file modes without `os.fchmod`; `office web start` recognises its server by a launch token rather than the pid (on Windows the server runs as the venv redirector's child, so start always timed out); `office preflight` shell-quotes the `agent.env` it tells an executor to source; and the install-drift check compares POSIX relative paths and reads a `file:///C:/...` install source correctly (every packaged resource read as drift). Without a load average, Windows never classes a check timeout as host load. Tests: a child given an isolated `HOME` on Windows gets the same `USERPROFILE`; the `agy` keychain, Node-less hook install, Claude sandbox and quoted submit-path expectations follow the host; tests close the SQLite connections they open, signal themselves with `signal.raise_signal` (`os.kill` on Windows terminates the pytest worker), delete a fake harness's launcher with it, stop the root-wake supervisor their run started, and skip lsof/process-group port reclaim on Windows; the pinned-gate fixture builds a Windows-layout venv and enables Git symlinks, and the revoke test no longer probes or reaps pids the POSIX way; test check and deploy commands name paths in POSIX form, lock probes use `office.portable`, the env teardown never stops its own worker (a `proc` root-wake binding records the in-process hook's pid), and POSIX-only cases (zombies, inherited flock fds, mode bits, newline file names, load average) skip on Windows. Upstream code added since is ported too: route-probe cleanup checks a holder with `portable.pid_exists` and tells which tracked processes still run from the Windows process API (there is no `ps` and no zombie state), and `agent.env` is written with LF so a shell can source it. Probe cleanup on Windows also ends a job it still holds after the leader exited: the leader-ownership check from #1296 stops signalling the group id then, and a child the tracker never saw would otherwise survive (Windows shows no process environments and has no `lsof`). The hand-off to a run's pinned runtime no longer calls `os.execve` on Windows, where it ends in an access violation, splits an argument at a space and loses the exit code: `portable.execve` runs the pinned runtime as a child and exits with its code. The new Hermes, route-probe and onboarding tests use native paths, the platform path separator and quoted shell paths, and the dispatched-run snapshot restore removes read-only git objects. Two tests read the host: `test_hydrogen_visual_rung_is_conditional_and_not_dispatching_without_the_user_allow` failed wherever `agy` is not installed and `test_route_policy_inspection_by_size` wherever `claude` is not, so each now reads its harness as installed. Windows containment follow-up: suspended launches fail closed on job setup failure, a separate worker lock survives supervisor loss, unconfirmed checks preserve their recovery guard, and headless agent jobs require verified containment before discovery fallback.

## 3.31.13

The agy quota probe now reads the `daily-cloudcode-pa` host agy 1.5 itself uses, falling back to `cloudcode-pa` only on failure (#1339). The production host reported idle 100% windows and no Gemini 4 bucket, so Hydrogen passed the reserve gate with about 0% left. The probe also reports a `gemini-4` family, which reads as exhausted when no bucket is reported, and agy `gemini-4*` routes gate on it.

## 3.31.12

Orchestrators run `office wait` as a background command (#1340). SKILL.md, the orchestrator reference and the `office context` turn rule now say to start it in the background (Claude Code: `run_in_background`), end the turn at that waiting point and act on the completion notification by exit code, so a queued prompt such as `/compactable` lands at the waiting point instead of whenever a foreground wait returns. Harnesses with no background command that wakes the turn keep the foreground wait. The reference documents compaction during a background wait and makes armed root wake the single waiter, so a background wait never wakes the root a second time.

## 3.31.11

`office close` on a run closed with `--handoff` and later merged by `office land --merge` now closes as landed (#1333). The terminal replay reused the stored handoff, so it tried to mark the merged PR ready, printed "handed off" and kept the worktrees. When the recorded merge covers the accepted integration, the replay clears the handoff, records the landed state and runs base sync and worktree removal. A handoff chosen after the run had already landed stays a handoff, and a merge record for another integration tree keeps the handoff path.

## 3.31.10

`tests/v31/test_landing_recovery.py` (added by #1312) gets its `keep` row in the test-retirement inventory and the contract-suite legacy table, so the inventory and record tests pass on main again.

## 3.31.9

Hermes panes start again when `hermes` on PATH is an installer wrapper that forwards to the git checkout's own wrapper (#1321). The launcher resolver stopped at the outer `exec <path> "$@"` script, so Office wrote no launcher shim, Herdr never detected the pane's Hermes, and every `herdr agent start --kind hermes` timed out into the headless fallback. The resolver now follows forwarding wrappers (bounded, loop-safe). `office harness smoke` no longer passes a clean exit that never answered: a line of the reply, or of the text inside a JSON reply, must be the READY marker alone, else the result is `no_answer`.

## 3.31.8

A convergence RECHECK on an orchestrator-inline (fastlane) or other live task session no longer loops on `worker-live` (#1318). Acceptance released the live session's lease, so its repair submit was refused as `lease-lost` and `office revoke` changed nothing. Now the RECHECK re-lets a fresh lease to the live session when no overlapping scope is held, and `office revoke` also fences a live session's released lease and records the end of an orchestrator-inline dispatch, so `office rerun --fresh` and `office dispatch` relaunch it.

## 3.31.7

Main is green again: three tests that 3.31.4 broke now pass (stale test expectations, no runtime change). Making `claude/claude-opus-5-5@high` dispatchable moved two routing outcomes the tests assumed. A planned slate whose routes are all dispatchable but out of quota or overkill now stops on `no-route` instead of falling through to ranked fallbacks, and the overkill test asserts that refusal. The automatic executor for the wait tests moved to codex, where Office deliberately ignores herdr's `working` status, so the tests that rely on `working` pin a claude dispatch (the weekly-warning test failed intermittently because the harness pick varies per run).

## 3.31.5

`office close --landed-externally` no longer fails a run whose run-level check is `scripts/validate.sh --pr` (#1312). The squash-landing proof re-ran every check on the merged default-branch commit, where the PR selector's diff against its merge-base is empty, so the gate always failed closed with `landing-checks-failed`. The PR selector's integrity-checked integration attestation on the reviewed composed commit is now carried instead, recorded under `squash_proof.checks.carried` and labeled scoped. Every other run and task check still re-runs on the merged commit. An unattested scoped check is not carried, and an unmerged PR still refuses.

## 3.31.4

Route probes no longer fail `isolation-missing` on a zombie they do not own (#1296). A long-lived Claude hook daemon outside the probe (TokenEconomizer's te-hookd) starts a child for the probe's Claude session and never reaps it. The exited child stayed a zombie in the probe's lineage, and cleanup counted it as a live process, so every claude probe made while the daemon ran failed. Cleanup now confirms only processes that still run, and fails closed when process states cannot be read. With the fix, `claude/claude-opus-5-5@high` passed its exact probe (attempt e1e142e6) and is dispatchable again, so XL executor rung 1 is back.

## 3.31.3

- The orchestrator judges new work at intake (#1300). `office start --new-work yes|no "<why>"` records `gates.greenfield_detection` (`judged_by: orchestrator`, `new_work`, `why`), shown by `office inspect run`; Office counts nothing and has no detection config. New work with no `--gear` is refused as `greenfield-candidate` with no run created, and `next:` names `--gear greenfield` and `--gear XS|S|M|L|XL`. `--gear greenfield --new-work no` warns once, records and starts. `--gear greenfield --fastlane` is now allowed: the orchestrator leads the one cohesive build at the XL floor and the convergence review stays required. A greenfield fastlane overflow hands off with `office rerun` only, since its one-task plan cannot be split. SKILL.md Start step 3 tells the orchestrator to always pass `--new-work`.

## 3.31.2

- Herdr landing test pins its reviewer harness: the reviewer re-prompt Enter test now asserts the configured retry count for a non-Codex reviewer, and a new test keeps the Codex one-Enter cap (#790). Routing drift had moved the fixture onto codex, failing main.

## 3.31.1

Removes the Auto Office 3.0 runtime and the six old gear presets, with no backward compatibility.
- **Gears:** `direct`, `direct+review`, `light`, `quick`, `express` and `full` are gone from config, code, docs and skills. `office start --gear direct` is now an invalid choice. A run still pinned to one fails on resume, status and dispatch with the `gear-removed` error (`config.require_supported_gear`), which says to start a new run with `--gear XS|S|M|L|XL`. `office close --abandon` and `office inspect` still work on it.
- **3.0 runtime:** `office raw`, `legacy.py`, `compat.py`, 3.0 run discovery in `office list` and `office resume`, and the doctor and install lines about it are removed. `runtime.new_runs: "3.0"` now errors that 3.0 was removed. The `compat_calls` table stays in the schema, unused.
- **Scripts:** `office_runtime.py` and the 3.0 helper scripts are deleted, along with the root tests that only exercised them. `validate.sh` now validates seed adapters with `office harness validate --file`.
- **Faster tests:** Tests start from a dispatched-run snapshot (#1302). A test marked `@pytest.mark.dispatched("builder")` restores the state the module function `builder(env)` leaves (trust, start, approve, dispatch), built once per module, instead of driving the lifecycle itself. Applied to the revoke and review-only cases in `test_lane_gate_revoke.py` and `test_review_reroute.py`. `scripts/validate.sh` accepts `VALIDATE_WORKERS` up to 4 as an opt-in for an idle host; the default stays 2 (`docs/validation.md`). The validate.sh digest in `verification_coverage.py` is updated.

## 3.31.0

Executor routing follows the Artificial Analysis Engineering Index (2026-11) table: size floors XS 31, S 37, M 40, L 45, XL 52 and Haiku 5.5 / Sonnet 5.5 / Opus 5.5 first rungs, with conditional Hydrogen at XS low, S medium and M high only, a frontend bias that moves the size's agy gemini-* rung to rung 1, and Hydrogen no longer a code reviewer (Luna high then Opus low).

## 3.30.1

Cheaper per-command lifecycle polls (#1302). An Office process reads its own start time from `ps` once and reuses it (claims, supervisor identity); start times of other pids are still read live on every check. `repo_identity` asks Git for the toplevel and common dir in one `rev-parse`. A lane-gate test that made 467 subprocess calls now makes 242.

## 3.30.0

Hermes is a reviewer again (requirement r3, accepted risk): `adapters/seed/hermes.yaml` restores the reviewer profile with `--yolo --accept-hooks` and no `--in`, and a post-run fingerprint detects, but cannot prevent, worktree writes. The vision profile returns too (7b2a3ffc^ form: `--toolsets file,terminal,vision --in {sandbox_dir}`, headless and pane); capabilities stay [builder, review], so vision needs its conformance proof. The completed=true readback from 7b2a3ffc stays.

## 3.29.0

Onboarding asks one harness-preference round when installed harnesses run the same model (Hermes and claude both run Sonnet): `office onboard --harness-preference office|keep|recommended`. Recommended derives `routing.harness_preference` weights from monthly USD per harness and provider, shows the arithmetic, and records the overlap as seen so it is not asked again. `office onboard --json` lists Hermes providers by name and status only.

## 3.28.19

Fastlane (#1297). The orchestrator can lead a task itself, inline or with its own cheaper subagents, instead of Office routing an executor. An independent code review is always required.
- **Flag:** `office start --fastlane` forces it at any size or risk, and `--no-fastlane` turns it off. The choice is pinned in `gates.fastlane`.
- **Default:** without a flag, Office uses fastlane for XS tasks at low risk. Otherwise it needs all three checks to pass:
  - quality: `inline_quality:` names the orchestrator's route, and that route meets the task size's executor floor
  - time: no tasks could run in parallel
  - cost: a measured `inline_advantage`
  
  If Office is not sure, it delegates. It never chooses fastlane on unknown or elevated risk.
- **Overflow:** the orchestrator context passes `orchestrator_context.threshold_tokens` (now 250k), or the task touches files outside its scope plan. Either one rides the existing `ctx.high` notice and makes every later task delegate. The handoff tells the orchestrator to compact inside Herdr. Elsewhere it only suggests compacting.
- **No executor route:** a task the orchestrator leads does not need an executor route to qualify. In that case Office records the orchestrator's own `inline_quality` route, which is also where work goes if fastlane overflows. If that route is missing too, dispatch refuses and says why.
- **Cost:** the economics ledger reads Claude subagent transcripts as `root-subagent` and counts them toward the led task. Inline route records say whether cost is measured.

## 3.28.17

Gemini 3.8 Flash is retired (Rico, 2026-11-19): a shipped deny covers every effort in all nine policy roles, and its catalog rows stay as history. A user can still lift it with `office route neutral`. The shipped visual reviewer seed is Hydrogen@high > Terra low > claude Opus low > agy Sonnet medium (claude Sonnet medium fails the visual role floor). Hydrogen still needs the user's exact allow, so without it Terra low judges. Tests that used Flash as a generic agy route now use live routes.

## 3.28.16

Gemini 4 Argon low is registered (non-dispatchable, effort confidence unknown). Exact agy probes pass for Argon low, medium and high (#690).

## 3.28.15

Hydrogen@high can review visuals. agy's vision launch uses plan mode, and the vision probe reads a pane-hosted supervisor's captured stdout from the dispatch dir.

## 3.28.14

Main is green again (#1273). A plan-reviewer re-pin records the pin's actor: an unquoted `office review plan --as` is the orchestrator's, as #1115 made it for code reviewers, not the user's. Tests that set routing policy in a run's pinned config now set the live policy (#1120). Tests that submitted an empty tree now make a real change or submit `--no-change` (#1138). Eighteen new test modules get their inventory and legacy-table rows. The plugin install contract test skips system events Claude Code 2.1.326 sends before `init`. Its scoped-gate pin is updated. Two check tests set a quiet-host load factor that the `env` fixture then wiped, so a busy host turned their failing check into UNAVAILABLE; the factor is now set after `env`. The visual readiness 502 probe test uses a fake clock instead of a 0.3s wall-clock window.

## 3.28.13

Convergence verdicts no longer lost or stale (cluster C2, #1242). A lane RECHECK that also has a finding no task owns still sends the owned findings to their tasks (#1220). An ownership amendment assigns that unowned finding to the task that now owns its file (#1135). A lane APPROVED records the contract versions the reviewer saw, and a review still running is not carried over to a same-tree recompose after an amendment (#1036). Revoking a lane review also closes its self-review subagent gates that were not yet supervised, and `office close` names the open gates it waits on (#1202). `close --handoff` and `close --landed` keep the stored convergence and integration state, so a later `office land` still sees them (#1227). In a rebased run, a restack conflict with a dependency launches the executor with the conflict recorded instead of refusing every relaunch (#1035). The convergence brief lists each task's commit and message and marks `office:` commits as Office's own (#360). Also repairs the #1138 regression in the added-task review test (it now submits `--no-change` with a code review).

## 3.28.12

Route probe reads work under any row factory (#1160). The probe and trial counts (`allocation`, the rolling trial window) read rows by position, so `reserve()` failed with `KeyError: 0` on a connection whose row_factory returns dicts. Office's own connection uses `sqlite3.Row` and was not affected. They now read the first column whatever the row type. A reservation test now runs reserve, in-flight refusal and stale expiry under a dict row_factory.

## 3.28.11

Claude reviewers hand back with `office review submit` (#1268). Their grant allows only the exact `office review submit --file <dispatch>/review-submit.txt` command, but the review brief offered stdin too. On 3.28.9, one reviewer piped its review in and was denied, and another had chained commands denied, decided Bash was off, and never tried the submit. Both fell back to the reply file. The brief and prompt now name only the submit file and the exact command, run on its own, and say a denial of any other command does not cover it.

## 3.28.10

Routing pins and quota walls (#1243). A launched reviewer that fails (empty reply, model_not_found, 402, quota) is a no-answer that falls back instead of EVIDENCE_BLOCKED, a failing pinned reviewer is dropped with a notice, and `office review L-T1 --as` recovers a lane or visual gate blocked that way (#1139, #1218, #663). Review pins record who set them and emit a route-change notice (#1115). The live user routing policy wins over a run's pinned copy, so `office route clear` reaches running runs (#1120), and `office config quota.reserve_percent` shows the per-role reserves that apply (#1116). pi pins resolve a bare model alias to its provider/model slug or refuse (#1185). A quoted `amend route` qualifies like a quoted `--as` (#858), RECHECK `next:` no longer offers a `--resume` that requalification would refuse (#752), and an amend refused for its route names `office amend route` instead of `office dispatch` (#814). Regression test for pi's credit-balance wall (#771).

## 3.28.9

Self-review proof and submit gates (bug cluster C4). The revert-and-fail proof keeps pytest option values, drops xdist distribution with `-n`, reverts every non-test source including templates like `.lava`, and names environment failures instead of calling them test failures (#1122, #1124, #1129, #1174, #1241). A check that could not run is never pre-existing (#1175). The combine commit for multi-dependency bases is deterministic (#1113). An unchanged-tree submit on a task with accept criteria needs `--no-change` and independent review (#1138). Blocked hints name the actual preflight refusal, and an open finding at the round cap is the orchestrator's decision (#1130). An ack-only rerun restacked with the same patch keeps its self-review ledger (#1127). The lane review brief says the consumed ledger is never in the tree, so its absence is not a finding (#576).

## 3.28.8

The Claude reviewer add-dir prompt is answered on the live screen (#1260). 3.28.7 stopped passing the state home, but the live pane draws a blank line between "Adding a new directory:" and the path, so Office read no path and left the prompt for the run's own dispatch dir unanswered. The path reader now skips blank lines before the path.

## 3.28.7

Claude reviewers no longer stall on "Adding a new directory" (#1239). Current Claude Code asks once per untrusted `--add-dir`. Reviewer and vision launches added their dispatch dir and all of Office's state and data homes, so a reviewer pane stopped at a prompt Office did not recognise, then at a second one for the whole state home. Reviewer read roots are now the run's own state dir plus the guidance dirs. Office answers the prompt ("Yes, add it", session only) only for a directory it owns for the run, and several in a row. It never answers for the state home or any other directory.

## 3.28.6

Stale root wake shown as detached (#1237). A pane that resumed or started another run moves its orchestrator binding there, but the old run's wake row still named it, so `office status` printed `wake: armed` for an unrelated agent's pane and guidance dropped the `office wait` step. Delivery already refused that pane. Status now prints `wake: detached (...)` with the awaiting-attachment count, and guidance treats the run as unarmed until `office resume` from its orchestrator.

## 3.28.5

Per-task self-review audits on XL only (#1233, Rico 2026-11-17: "Separate panes on XL only"). A new run funds Office's own per-task audit, a separately routed reviewer session before the convergence review, on XL only (`review.self_review_audit_gears`, pinned at start). S, M and L tasks go back to the executor's in-session fan-out: the focused self-review starts one subagent per lens in parallel, with the same ledger and one round. XL keeps its audits and the Goldfish plan review. Runs started earlier keep auditing S through XL.

## 3.28.4

pi reviewers hand back with `office review submit` (#1231). The pi reviewer pane form never gets bash, so every pi review fell back to the reply file. Office now writes a per-dispatch pi extension that registers one tool, `office_review_submit`, which runs exactly `office review submit --file <dispatch>/review-submit.txt` with no shell, the same narrow grant codex and claude reviewers get. bash stays off and the headless pi reviewer keeps its vetted read-only argv. opencode and agy reviewers still hand back by reply file.

## 3.28.3

Orchestrator waiver at any RECHECK round (#1228, Rico 2026-11-17). `office waive <lane> --reason` is accepted at any round whose review returned RECHECK, not only at the round cap, with the same substantive-reason rule; the verdict stays RECHECK and landing still needs the user. Repairs that RECHECK routed and that have not started are recalled to their accepted revisions; a repair already running or submitted is refused (`repair-in-progress`).

## 3.28.2

Dropped lane review recovery (#1228). A lane review that closed stale with no verdict (a member changed while the reviewer was working) is no longer carried by a same-tree recomposition, which left the lane "reviewing" with nothing running: the recomposition starts a new review. When no recomposition is coming, `office status` names the recovery and `office review <lane>` re-runs the dropped review on the current composition. The dropped reply is never recorded.

## 3.28.0

Reviewer dispatch rewrite (#1191). A reviewer that fails to settle is replaced on the next route by one `review_fallback` job and a whole failed chain is re-dispatched once; a revoked reviewer ends `REVOKED`, not as a failure; every reviewer role reads one pin helper; reviewers launch async with a packet, lease and session id and run on a detached snapshot; `office review submit` is the one completion transport. `scripts/review_no_verdict_rate.py` reports the no-verdict share of done review gates from `runs.db`.

## 3.27.1

Quoted route overrides. `office route override` also accepts `--rico-quote "<verbatim words>"` with `--quoted-at "YYYY-MM-DD HH:MM"` (Manila): a non-worker caller relaying the user's real words issues a receipt without a terminal or the 'authorize' prompt. Workers (`OFFICE_DISPATCH_ID`) are still refused, the quote must be non-trivial, not future and within 24h, and every other check is unchanged. Receipts record issuer kind (terminal or quoted), quote, quote time and `HERDR_PANE_ID`, all in the HMAC body (older receipts keep their original body), and `--list` shows them.

## 3.27.0

The agy Hydrogen reviewer launches in a Herdr pane like the worker (#1027 parity), with plan mode as the write barrier. The pane hosts the same headless `agy --mode plan --print` run (`interactive.supervisor: true`, `wall_cap: true`) and Office captures its stdout as the reply, so no agy permission setting is relied on. The profile keeps `routes: [gemini-4-hydrogen-high@high]` and `verify_worktree_unchanged`, passes no permission bypass, `--mode auto` or `accept-edits`, and a stalled pane ends at `max_minutes` as a reported no-verdict result. Headless stays the fallback when Herdr is unusable. The adapter hash changed: rerun `office doctor --probe-route agy/gemini-4-hydrogen@high` after the reinstall.

## 3.26.0

Credit reporting and an agy-wide cooldown (#1195). The agy probe reads agy 1.5.x's keychain token, reports the AI credit balance, and reads low credits as exhausted. An agy `RESOURCE_EXHAUSTED` or AI-credits failure now sets an agy-wide cooldown instead of cooling one route. The PR gate maps hyphenated scripts to their underscore test modules, so `scripts/agy-usage.py` selects `tests/test_agy_usage.py`; a hyphenated script with no matching test still fails closed.

## 3.25.0

H5 (Rico 2026-11-15): Hydrogen also reviews plans. `plan_reviewer` joins `code_reviewer` in the Hydrogen review roles, with the same gates: only the exact `agy/gemini-4-hydrogen@high` route, only under the user's exact allow for that role, behind a fresh exact probe, through agy's route-scoped read-only `reviewer` profile with the before/after fingerprint (a plan review fingerprints the run's repo root). With that allow, `roles.plan_reviewer.preferred_seed` accepts the route. Visual review stays closed: agy's `vision` profile runs with `--dangerously-skip-permissions` and no worktree check, so it has no write barrier. The `office route policy` header and `office route allow` message now say an allowed Hydrogen route also needs a fresh exact probe. Integration review stays closed.

## 3.24.1

Codex visual reviewers can hand back with `office review submit` (#1186 follow-up). The codex `vision` profile now carries the same `submit_grant: codex-rules` and trusted dispatch dir as the code reviewer, so the one exact submit execpolicy rule applies. Claude visual reviewers already had the #1186 shell, and agy visual reviewers run with a full shell.

## 3.24.0

Claude reviewers get a shell (#1186). `--restricted` dropped Bash unless `--tools` named it, so the #1030 `office review submit` allow never applied and reviewers read code without running anything. Reviewer and vision profiles now name their tools and, where Claude Code's OS sandbox runs, pass sandbox settings: built-in read-only commands (git log/diff/show, ls, cat, grep, find) and targeted test runners (pytest, `pnpm test`, `npm test`, `node --test`) run sandboxed with no network and no writes outside the dispatch dir and temp; only the exact submit command runs outside the sandbox. Hosts without a sandbox keep the submit-only shell.

## 3.23.1

H4 follow-up (#1178): a pinned Hydrogen code reviewer (`office dispatch --review-as`, `office review --as`, rerun and the review gate's pin checks) needs only the run, not a task scope, since a reviewer has no task size; it still needs the exact `code_reviewer` allow and a fresh exact probe. An executor still needs its classified task.

## 3.23.0

H4 (#1178): the Hydrogen code reviewer now actually dispatches. agy gains a headless, read-only (`--mode plan`, no permission bypass) `reviewer` profile with `verify_worktree_unchanged`, scoped by the new profile field `routes` to `gemini-4-hydrogen-high@high`, so no other agy model gains a reviewer launch form (candidate building, `office route policy`, `office harness validate`/`smoke` and `build_argv` all honor the scope). An unsized Hydrogen allow no longer carries a size scope that refused size-less reviewer routing. A manual `--review-as` no longer stands in for the user's exact `code_reviewer` allow. `office route policy` reports a missing harness or launch profile for every role, not just executors.

## 3.22.3

Research (#1168): Hydrogen shadow-review results, dispositions and escapes for live PRs #1172, #1173, #1176 and #1177; the shadow lane surfaced #1178.

## 3.22.2

`office route policy` no longer repeats "dispatches (dispatches (" on user-allowed Hydrogen rows. Adds the H3 Hydrogen code-reviewer learning (#1168).

## 3.22.1

H3 follow-up (#1168): a `preferred_seed` may order a catalog-disabled route that the user exactly allows for that role. So `agy/gemini-4-hydrogen@high` can sit in the code-reviewer order, while routing still enforces the Hydrogen exact-probe veto. `office route policy` now shows that route as a voting code reviewer, not as a canary trial.

## 3.22.0

H3 (#1168): `agy/gemini-4-hydrogen@high` may serve as a voting `code_reviewer` when the user exactly allows it, and it still needs a fresh exact probe pass. Every other reviewer, planner and worker role keeps the Hydrogen veto.

## 3.21.0

Hydrogen S executor canary (#1168): `routing.hydrogen_trials.canary_tasks` (0..10, read from the live config, shipped default 0) makes an eligible low-risk S task prefer the user-allowed `agy/gemini-4-hydrogen@medium` until the budget is booked, one per run, then routing reverts to normal scoring; canary rows are hidden from the trial path's window and per-run count (#1169, #1171). agy maps effort `xhigh` (#1169). Research: out-of-band Hydrogen shadow-review lane and planted-bug scorecard in `research/hydrogen-shadow/` (#1170).

## 3.20.2

Research only (#862): AA v5.0.1 evidence sidecar, read-only Office outcome extractor and A/B/C shadow calibration replayed on local outcomes. No winner over a base rate; production v4.3.2 scoring, floors and #1143 policy unchanged (#1144).

## 3.20.1

Honor exact, size-scoped user allows when admitting Hydrogen executor candidates (#1166).

## 3.20.0

An exact user allow of an agy Hydrogen executor route now dispatches as a normal executor route (exact probe pass required, no canary reservation, S/M/enabled/risk checks skipped) and `office route policy` says so (#1163, #1162).

## 3.19.3

Strengthen manual Hydrogen routing and dict row factory regression coverage (#1158).

## 3.19.2

`route_probe` cache and reservation reads now build dicts from `cursor.description` so callers passing a plain `sqlite3.Connection` without `row_factory = sqlite3.Row` (such as `routing.route` -> `hydrogen_trials.manual_conformance` on rerun) no longer crash (#1156).

## 3.19.1

`office route policy` unsized Hydrogen rows now note when the automatic canary is disabled, and `tests/test_routecmd_hydrogen.py` pins `dispatches is False` vs `dispatches is None` (#1151).

## 3.19.0

agy identity readback for exact conformance probes: the probe adds `--output-format stream-json --effort <effort>` (probe launch only), reads the model from the first `init` event, derives effort from the slug's tier suffix and checks the reply from the result event. agy rows no longer refuse with `no-identity-readback`; dispatch argv is unchanged.

## 3.18.1

`office route policy` no longer says `office route allow` makes a Gemini 4 Hydrogen route dispatch (#1147, #1149). Hydrogen rows now name the canary veto: the role, the exact agy effort for each size, the `routing.hydrogen_trials` limits, and the exact conformance probe they still need. The view also says that agy probes currently refuse with `no-identity-readback` (#1148), so enabling the canary alone does not open it. Follow-ups: #1151.

## 3.18.0

#1143: owner-approved XS–XL executor peer rungs, narrow Terra High M policy, verified account/pool quota cooldowns, identical RECHECK escalation and append-only atomic 3-in-20 Hydrogen S/M trial ledger. Hydrogen remains OFF pending real conformance and fallback evidence; XL, visual review and authorization safety are unchanged.

Stage Gemini 4 Hydrogen Low/Medium/High/Xhigh as external-evidence, non-dispatchable Antigravity executor routes with missing pinned AA v4.3.2 scores and owner-approved advisory XS–XL seeds (#1142, #1143). Correct source provenance disclosures so third-party slugs never masquerade as local conformance; add exact-effort/argv, role safety, lineage, benchmark-isolation and distribution-parity tests. No Hydrogen canary, score cutover or visual-review replacement is enabled.

## 3.17.0

Registers Claude Fable 5.2 (`claude/claude-fable-5-2@low|medium|high|xhigh|max`) as a gated, planner-only, non-dispatchable catalog model (#1133). Its AA v4.3.2 scores are null; AA v5.0.1 scores are reference evidence only and never drive selection (#862). A hard role boundary refuses Fable for every non-planner role and for planner until an effort is separately qualified, including under user allows, aliases, `--as` and signed policy overrides.

## 3.16.0

Each run shows live on a display-only herdr-projects board card (`docs/board-card.md`): the key comes from rico-mode (`OFFICE_BOARD_CARD`, `office start --card`), or Office adds its own whenever the board is installed, never two per run. The card carries the run's state (blocked says "answer in rico-mode"), progress, the plan, every executor and reviewer route with its fallbacks and reasons, and every review round with its findings. New `office card status|link|sync`; config `board.card: auto|off`.

## 3.15.63

Fixes from this run: #1087, #1086, #1084, #1083, #1085, #1068, #958, #1081, #1082, #567, #758, a per-role quota reserve default, and SKILL.md back under its size budget.

## 3.15.62

`office route policy --effective` names each route's per-size states when they differ from the unsized verdict (#1099). A user role rule written after a run pinned its policy reaches that run's next routing decision, and `office route allow|neutral|deny` says so (#1100). `office route clear` says a run that pinned the rule keeps applying it until `office config --run <id> --apply-routing` re-pins it. The pi registration test reads status for the run it started, not a bare status (#1107).

## 3.15.61

Reviewer re-prompt counts a written reply file or an agent reported working as submitted rather than stopping on held scrollback (#1106 H4).

## 3.15.60

A reused route that a user deny now rejects is dropped and the rerun re-picks from the approved plan (recorded as a route change); `amend route --auto` clears a stale reroute on the parent dispatch; status shows the route the next launch takes from the same selection the rerun uses, and a planned route that will not run is shown with its rejection reason, never as `(planned)` (#1106 H3).

## 3.15.59

A lane visual review now needs one OBSERVATION line per screenshot (no bare APPROVED), and the reviewer infers which UI sections must be shown from the acceptance and plan, reporting uncovered ones as findings (#1078).

## 3.15.58

Orchestrator guidance: walk a UI lane yourself while its convergence and visual reviews run, then route your findings with the reviewers' (#1077).

## 3.15.57

Executors on a harness whose adapter sets subagents: false (opencode) are told to do their task and self-review in their own session, instead of starting a subagent that can hang (#1074).

## 3.15.56

A free-tier route that hits its usage limit again the same day, with no known reset time, now gets a doubling cooldown (capped at local midnight) instead of being re-picked every half hour (#860).

## 3.15.55

The self-review revert proof now finds the pytest command in checks like 'uv run --with pytest pytest ...' instead of taking --with's package value as the runner (#1072).

## 3.15.54

The convergence review brief now tells reviewers that a check failing only because their read-only checkout has no provisioned environment (.venv, packages) is not a blocking finding against the work (#1044).

## 3.15.53

In a v3.1 run, a superseded plan-review round's late result no longer queues a review for a revision the orchestrator vetoed with --no-review (#962).

## 3.15.52

A just-queued self-review subagent gate is no longer reported as a stall: its lane's convergence review job owns it (#961).

## 3.15.51

A lane convergence review whose job failed or ended without settling it is now settled UNAVAILABLE, so office resume and the fallback can run it again instead of it staying queued (#1022).

## 3.15.50

A failed scripted visual step (a click that timed out, a state that never appeared) now reaches the lane's vision reviewer as evidence with a screenshot instead of an automatic RECHECK (#384, step 1).

## 3.15.49

- test: retire the exhaustive model-roster and duplicate tests, with a traceability record in `docs/test-retirement.md` and an inventory check (#871 PR C, #1037).
- chore(ecosystem): raise the hub (247 -> 250) and auto-adapter (53 -> 55) skill line budgets to what main ships; the trim is tracked in #1025.

## 3.15.48

Credit-limited routes can carry an opt-in daily dispatch budget (quota.daily_dispatch_budget); at the budget the route reads as quota-exhausted so reviewer chains fall through, with a warning at 80% (#831).

## 3.15.47

A reviewer the orchestrator revokes is no longer recorded as a route failure, and no fallback reviewer is started for it (#1007).

## 3.15.46

office dispatch and inspect warn when a --review-as pin has no effect because the run's gear runs no code review (#727).

## 3.15.45

Orchestrator guidance adds idle-only outcome QA: progress and required gates first, small optional QA slices only while waiting, never a gate (#904).

## 3.15.44

When a contract amendment already covers a worker's scope request but could not be confirmed delivered, status, the undelivered notice and office answer now name office answer as the way to resume the worker instead of contradicting each other (#799).

## 3.15.43

A herdr agent that is working on its brief, with the pointer no longer in its composer, no longer triggers a 'brief pointer did not land, re-prompt it' notice (#583).

## 3.15.42

The amendment authority filter no longer refuses text that names an email field or address; only outbound email phrasing (email the users, bulk email, emails to customers) counts as an authority action (#543).

## 3.15.41

An amendment that changes only a task's visual.url or visual.start keeps the accepted task accepted and reruns its lane's visual capture, with no executor and no round spent (#728).

## 3.15.40

office close --handoff <pr> closes a run whose blocked integration was composed by hand, recording integration_composed_by_hand; the close refusal names the unapplied amendment (#729).

## 3.15.39

office status keeps an open executor's stop signal, or an executor pane idle without submitting, as a blocker: line with its resolving next: until it resolves (#671).

## 3.15.38

A contract amendment's ripple delivery tells each dependant which task's contract changed and that it is not theirs, instead of telling it to apply the amendment (#960).

## 3.15.37

Plan submit now shows a planner route entry that routing dropped as a notice, and office queue help says pausing a whole run also holds its integration checks.

## 3.15.36

A dependent task's base now composes sibling tasks' appends to the same declared shared: registry as their union instead of refusing the dispatch with dependency-conflict (#537).

## 3.15.35

`office model verify <harness>/<model> --evidence <run>/<task>` promotes a model added with office model add to a local-evidence: source on cited acceptances under independent review; explicit only (#656).

## 3.15.34

`office amend route <task> --auto --quote "<words>"` clears a pending task's declared route so it routes automatically again, and records the change (#977).

## 3.15.33

`office config --run <id> --apply-verification --quote "<words>"` re-pins a running run's check timeouts and check concurrency from the config files, and `office config --run <id> <key>` shows the value the run pinned (#755).

## 3.15.32

A task's worktree setup that exits 127 (command not found) now stops the dispatch with an office doctor hint instead of launching an agent without dependencies; other setup failures stay a notice (#364).

## 3.15.31

Resubmitting a task's accepted tree unchanged (for example after an amendment demoted it) is empty work: no self-review ledger is owed, and the revision records an empty exemption (#781).

## 3.15.30

Adapters can set `quota_probe.unknown_is_unlimited: true` so an unknown quota reads as full headroom; routing stops skipping harnesses that have no probe (#1002).

## 3.15.29

- feat(review): narrow grant (Rico-approved). claude and codex reviewers may run exactly `office review submit --file <own dispatch dir>/review-submit.txt` and nothing else: an exact claude allow plus explicit read-only Bash denies, and a per-dispatch codex execpolicy rule. pi gets no grant (#1030).

## 3.15.28

- feat(review): `office review submit` (stdin or --file) lets a reviewer hand back its verdict for any review kind. It validates the reply for its gate, records it at once and ends the reviewer's wait. Reviewers get only a per-dispatch submit secret, no other Office authority (#1030).

## 3.15.27

- feat(brief): one neutral executor-brief line allows the harness's subagents for the task itself at the executor's discretion. Scope, checks and submit stay its responsibility (#1031).

## 3.15.26

- feat(wake): the root-wake prompt is one line, `Run <id> (through event N): <what changed>; next: <action>. Run office --run <id> context.`. The standing rules moved to SKILL.md (#1029).

## 3.15.25

- chore(harness): remove the gemini harness; agy replaces it (#1028). agy's Gemini models, the gemini model-family floor and agy's ~/.gemini token paths stay.

## 3.15.24

- fix(convergence): a lane finding code is unique across its gate kinds, so `office disposition L-T1:F11` cannot miss a same-coded visual finding (#398).

## 3.15.23

- test(convergence): the retract-in-ingest test expects four convergence reviews, since #768 re-reviews a same-tree recomposition after a contract change.

## 3.15.22

- fix(convergence): a same-tree recomposition after a contract or acceptance change is reviewed again, not carried (#768).

## 3.15.21

- fix(convergence): an EVIDENCE_BLOCKED lane whose block is not the visual capture names `office resume`, not a visual capture amend (#870).

## 3.15.20

- fix(preflight): the stale-ledger line never prints two identical shas (#806).

## 3.15.19

- fix(convergence): a full sha matches an abbreviated composed commit (#893), and a stated commit with the composed commit's tree is the same review (#782).

## 3.15.18

- feat(amend): a live run can amend its PR options after the user confirms (#937).
- feat(land): ordered batch landing across runs and PRs composes and runs the full suite on a branch before advancing main (#943).
- feat(gear): opt-in greenfield lean gear with one strong executor and a final convergence review (#936).
- feat(executor): a per-executor context cap (default 200k) signals the orchestrator; Office never rotates on its own, and `office executor-context <task> rotate | continue | threshold` records the decision with a durable handoff (#939).
- fix(plans): a check with command substitution is no longer misreported by the plan validator (#905).
- feat(wake): auto-wake for non-Herdr Claude Code orchestrators through the bound root's Stop hook (#940).
- fix(doctor): fail when headless Chromium cannot launch, and disclose a skipped visual review at closeout (#942).
- feat(closeout): label each acceptance criterion as verified live or against fakes (#945).
- fix(route): `office route policy --effective` agrees with `dispatch --as` (#899).
- fix(quota): retry a 20s quota probe timeout and fall back to the last known quota (#941).
- feat(status): per-task tokens and cost in `office status`, Codex included (#944).
- fix(guide): the `next:` line names a rerun while the worker is still live (#913).
- refactor(review): new runs default to one-round, one-subagent executor self-review with a test-strength lens and an automatic revert-and-fail proof; security review moves into convergence; v3.1-pinned runs are unchanged (#938).
- feat(status): a nonblocking `oversize:` signal when a dispatch's observed context, wall time or cost outgrows its size class (#917).
- fix(preflight): accept a low-risk `fixed` disposition without a test path (#908).
- docs(onboarding): the missing-CLI question is asked once per session, and the origin probe tells GitHub, checkout and index installs apart (#927).
- feat(plugin): the plugin ships a curated 16-skill set with the original full `/auto-office` and a standalone `/auto-review`, generated into `plugin-dist/` (#926).

## 3.15.17

- fix(panes): a vision conformance probe pane is labelled "vision probe" and closed once its reply is read (#910).

## 3.15.16

- feat(queue): `office queue pause run:<id>` holds the run's automatic integration checks until `office queue resume` (#800).

## 3.15.15

- fix(watchdog): a dispatch in a reused pane reads only the output below its own setup line, so a revoked executor's usage limit never stalls it (#753).

## 3.15.14

- fix(review): a FINDING that quotes `verdict ==` no longer hides the reply's VERDICT line (#894).

## 3.15.13

- test(review): three route-refusal tests use kilo, a harness with no reviewer profile, as their builder-only example. #993 gave pi a read-only reviewer profile, so the old pi example launched and failed preflight instead of being refused.

## 3.15.12

- feat(review): reviewers launch like executors. pi reviews in a Herdr pane (its pane form may write only its reply, and the worktree guard still applies), and its read-only headless form is the CLI fallback. A pane-capable reviewer that ran headless is announced in status (#1027).

## 3.15.11

- fix(routing): an exhausted route (0% headroom) is never within the quota reserve, even when a run pins `quota.reserve_percent: 0`, so Codex at 0% weekly no longer stays the first reviewer.

## 3.15.10

- fix(routing): an ineligible planner `route:` entry drops alone with a notice. The eligible ones keep the planner's order and slots (#875).

## 3.15.9

- fix(review): a Herdr pane reviewer that hit its harness quota wall (the adapter's own signature in the pane's last lines, e.g. opencode "Free usage limit reached") moves the chain to another harness before any re-prompt.

## 3.15.8

- fix(checks): a check whose interpreter lacks its own test runner (`python3 -m pytest` with no pytest) is UNAVAILABLE with the fix, not CHANGES_REQUIRED (#739).

## 3.15.7

- fix(review): a reviewer routes from the run's current pin, not the snapshot its job loaded, so an `--apply-routing` re-pin reaches self-review subagents inside an in-flight convergence job.

## 3.15.6

- fix(config): `office config --run <id> --apply-routing --quote` re-pins the `quota` block too, so a lowered `quota.reserve_percent` reaches a live run.

## 3.15.5

- fix(config): a stale preferred_seed entry in another role no longer blocks config writes or `office route allow`. Only seeds the edit changes are validated, and an unchanged stale seed is a warning naming the command that replaces it (#874).

## 3.15.4

- fix(land): a GitHub error while reading required checks (rate limit, 5xx, network) is `checks-unavailable` with a retry and stale-gh-cache hint, never `checks-failed` pointing at `office rerun` (#772).

## 3.15.3

- fix(land): restack reads the pushed task branch head. A failed accepted-revision push leaves an ancestor that is a safe lease (#538). A head pushed by hand per `next:` whose tree is the accepted revision merged with the base is adopted instead of re-conflicting, and `next:` names `office close --landed-externally` (#676).

## 3.15.2

- fix(convergence): a stacked task's in-place edit of a `shared:` file composes when the composition still holds the file exactly as the task's recorded base (#773); sibling edits are still refused. The `next:` hint no longer says to serialize tasks that already depend on each other.

## 3.15.1

- fix(start): `--no-prs` skips the unsafe-run-base refusal, and explicit `--base` is documented and shown in `office start --help` (#730).
- feat(model): `office model add --role` registers a reviewer like an executor; `pi/gpt-6-luna@high` is a read-only reviewer whose whole rendered command is allowlisted (#993).
- fix(wake): stop duplicate "new lifecycle transitions" wakes. A newer actionable wake is held (default 180s, `OFFICE_WAKE_COALESCE_SECONDS`) while the previous one is delivered but unobserved, then one wake carries the latest event id when the root observes or the window ends; queued/running gate progress no longer changes the wake signature. #723's at-least-once delivery is kept (docs/root-wake.md).
- fix(install): Auto Office is not on PyPI, and the `auto-office` name there is an unrelated project. The install check, README, docs and CLI hints install from `git+https://github.com/Hikari9/auto-office`, and `office update --check` reads the release from GitHub main instead of PyPI.
- docs: point run gear executor slates to policy and clarify the truncated Sonnet fallback (#963).
- fix(skill): the install check's consent stays the native **Install the CLI** choice (#924), not a separate approval stop; the legacy release-identity test and the orchestrator reference follow it.
- test: cover route amendments without a worker environment (#963).
- test: cover review-only reruns when the preferred alternate route is unavailable (#963).
- fix(status): show pre-existing check failures alongside the accepted revision (#963).

## 3.15.0

- Orchestrator stalls: office resume arms root auto-wake by default (--wake-off opts out) and status shows whether wake is armed; queued-too-long, stalled and closed-unread reviews raise a wake event with a next: line (#922, #919); no false stalls for panes waiting on monitors or running self-review subagents (#895); a written reviewer reply is ingested even while the pane reads busy (#918); the office CLI exits 75 with a retry message mid-reinstall and records office.reinstalled (#920); route.recovered event and status line when a quota/auth-avoided route is available again (#921, #946).
- fix(visual): refresh proven reviewers immediately, probe eligible fallbacks beyond four, preserve rejection reasons in status, and share lane-review helpers (#910).
- fix(questions): worker brief prefers durable office raise; the real AskUserQuestion menu from rock-pages e789954a T6 is pinned as a wake fixture, since the footer parser already detects it (#911).
- fix(jobs): terminal outbox rows keep their claim token as provenance, restoring the #705 squash-close close-replay contract after #883 (#898).
- test(review): realign the codex Enter-recovery fixtures with #882's composer behavior so the review-reprompt tests pass again (#898).
- fix(plan-review): a stale PASS ends plan review while a stale round keeps open defects and ATTENTION blocking, the round-budget rule is one shared helper, and a round already in flight reviews an amended revision without re-queueing (#892).
- test(gates): run-check fixtures fail with the task's change, not on base, and the #809 M executor slate is read from effective policy instead of a frozen tuple (#892).
- fix(amend): `--as` is refused on every amendment except `office amend route` with nothing recorded, and `amend route` refuses any plan-amendment flag given a value, empty or not, rather than dropping it, with the drift guard derived from the parser; an executor, including one in its task worktree without its dispatch env, cannot record a route change through `amend route` (#896).
- fix(gates): full-suite checks run under `verification.full_suite_timeout_seconds` (default 7200) only when the command is exactly `scripts/validate.sh` or `./scripts/validate.sh`, alone or with `--full`; launchers, `env`, chains and redirections keep `check_timeout_seconds`. A check timeout names its limit and the setting to raise, and only Office's own kill records a timeout (#897).
- feat(plugin): ship the root auto-office skill in the Claude Code plugin, with CLI-detect onboarding that gates the PyPI install on the user's choice (#914).

## 3.14.0

- catalog: `codex/gpt-6.1-terra` low, medium and high are dispatchable on recorded `office doctor --probe-route` passes (codex-cli 0.163.0, 2026-10-27); none, xhigh and max stay probe-eligible.

## 3.13.0

- refactor(policy): externalize executor/planner size slates, benchmark floors, restrictions and authorized exceptions into one validated catalog policy (#871 PR A).
- test(contracts): lean policy-driven runtime contract suite with handwritten metamorphic oracles and fault-injection proofs (#871 PR B).
- test(gate): switch ordinary PR validation to the lean contract suite and affected-domain selection, with truthful scoped attestations (#871 PR C).

## 3.12.0

- fix(routing): demote provider-wide quota pools on usage limits and reject unsafe live reroutes (#860).
- fix(rerun): make stale accepted dependent reruns actionable and serialize restacks (#856).
- fix(close): stop claimed integration checks and prevent retries after an externally landed close (#836).
- fix(review): treat crashed OpenCode reviewer panes without replies as route failures (#827).
- fix(review): resubmit held Codex brief pointers with composer Enter fix and log reviewer fallback reasons (#790).
- feat(economics): add shadow delegation assessment and sampled efficiency reporting with measured win gates (#502).

## 3.11.2

- fix(validation): compare integration failures against the pinned base, report tracked check mutations, fence stale plan reviews and revalidate accepted-task amendments (#629, #819, #802).

## 3.11.1

- fix(probe): accept printed harness-major route IDs in doctor probes; refuse stale majors before launch (#858, #627).

## 3.11.0

- feat(routing): M executors prefer Haiku 5.5 High with exact audited size-scoped floor exception, retaining independent dispatch conformance and Sonnet operational fallback (#861).

## 3.10.2

- fix(routing): honor harness-major user rules for declared routes and register Argon lineage; align release tests with current routing and catalog policy.

## 3.10.1

- test(gate): run changed-file-targeted `--all` tests for each PR and pre-push; attest scoped Office landing checks separately from the explicit full suite (#854).

## 3.10.0

- feat(catalog): move the shipped model seed to strict, normalized TSV routing and benchmark tables plus YAML provenance; keep legacy in-memory objects, run hashes, lineages, user overlays and policy unchanged (#852).

## 3.9.0

- feat(routing): a user role allow (or exact `preferred_seed` entry) makes a route dispatchable with no proof, qualification or trust gate and overrides a catalog or user-overlay `dispatchable: false`; a runtime route failure is logged (`route.failed`), demoted for a TTL and falls back to the next eligible route; `office route policy --effective` shows what dispatches and why, and `office route allow a,b` reshuffles several routes at once (#845).
- feat(routing): committed role policy is size-scoped allow / neutral / deny from one data file, `catalog/role-policy.yaml` (#850 sections 1-3). Executor follows the #809 slate by size (Haiku, Gemini 3.8 Flash, GPT-6.1 Terra, Sonnet 5.5 on claude and agy, XL Opus on claude only, GPT-6.1 Sol, conditional GLM) and denies Astra and 5.6 Sol/Terra; code and plan reviewers are Luna high > DeepSeek max > Opus low (claude, then agy) with Sonnet medium/high and Opus above low denied; the visual reviewer is Gemini 3.8 Flash low/medium and GPT-6.1 Terra low. User `role_rules` accept `sizes`, `office route allow|neutral|deny|clear --size XS,S` writes them and `office route policy --effective --size XL` shows one size.
- feat(routing): the #809 size slates are the default executor routing (#846, #850 §1 and §4). A sized XS-XL run nominates its slate ahead of a flat `roles.executor.preferred_seed` (a per-size seed you set still wins), slate routes that qualify rank ahead of every route outside the slate (the others stay behind as fallbacks), and an unpinned planned slate whose primary is outside the slate re-routes at dispatch. `status` names the preferred route when a task runs on another. The orchestrator plans every gear, including L and XL; a standalone planner runs only on `--planner dedicated`, routed by run gear: M Opus 5.5 low, Astra low, Argon 4 low; L medium; XL high.
- catalog: register Gemini 4 Argon Medium (unscored, #809 L-only exception) and High (AA v4.3.2 score 53) on agy as non-dispatchable rows pending launch-banner conformance; slugs verified via `agy models` (#809, #850).

## 3.8.2

- perf(test): `office wait` uses a monotonic deadline and a clock/sleeper seam; deterministic wait tests run on a virtual clock (#796, #797).

## 3.8.1

PATCH: #809 conformance follow-up (#841).

- An M run executes a task inline only for a one-task, low-risk run with a structured, strictly positive, beneficial `inline=<n> dedicated=<n> <unit>: <evidence>` estimate (evidence of at least 12 characters and 3 words); zero, negative, non-finite, equal, dearer and placeholder estimates delegate, and S follows the same estimate rule. Runs pinned before the XS-XL gear model never go inline.
- New-run help prefers XS-XL; a historical gear alias prints a compatibility notice and keeps its pinned gates.
- Tests: the scripted-fake routing hides every agy row (Gemini as well as Claude), so a fake worker no longer runs as a real process that drains convergence jobs past an in-process monkeypatch (`test_a_failed_subagent_blocks_convergence_and_spends_no_round` failed about half the runs).

## 3.8.0

MINOR: role-scoped routing policy (#837, advances #627). Backward compatible.

- Policy: role-scoped `allow` / `neutral` / `deny` replaces the `proven` / `valid-unverified` / `quarantined` trust gate. New routes are neutral and eligible; a deny excludes a route for that role only. User config outranks shipped defaults; repo config and `--set` cannot write role rules.
- CLI: `office approve trust` is deprecated: it still runs, records nothing, and prints the new `office route allow|deny` pointer (historical trust acts stay as audit evidence). New `office route policy|allow|neutral|deny|clear|override|migrate`. `office route override` is a user-issued, signed, time-boxed one-off deny override bound to a run, role and route; a quote or orchestrator pin cannot lift a deny.
- Config: new `routing.user_policy.role_rules` and `strict_allow_roles` (user tier only); shipped reviewer `strict_seed` is now `false`. Existing `denied_models` and ceilings keep working unchanged. Pinned older runs keep their pinned runtime.
- Migration: `office route migrate` (dry run), `--apply`, `--rollback`.
- Runtime pinning (#843): a later MINOR or PATCH of the same MAJOR now serves a pinned in-flight run (`version.serves`) and moves it onto its own line at the next idle boundary (`upgrade.auto_adopt`); a live executor no longer strands the run. Only a MAJOR bump, or a line in `version.BREAKING_LINES`, needs `office upgrade`.

## 3.7.0

Rolls up everything landed on `main` since 3.6.6 (the version was not bumped per PR before this release).

- Routing: new-model trials on bounded production tasks, pin-loop fix, strict reviewer seed (#815); quoted override effort ceiling (#818); `routing_seed` no longer hashes `plan_version` (#820); seeded reviewer fallbacks cut by the trust floor are named (#825); Claude effort readback for the conformance probe (#832); no free-route substitution for an available paid preference.
- Catalog: GPT-6.1 Terra with effort-aware 6.1-over-5.6 Sol/Terra supersession (#811); `claude-opus-5-5@low` dispatchable on recorded probe evidence (#835); Pi/Kilo MiMo 2.6 Pro and Flash routes retired (#786).
- Runtime fixes: stalled orchestration recovers with current legal actions (#747); route authority preserved and accepted-task reroutes requalified (#745); squash landing provenance verified before closing runs (#748); runs anchored to durable Git repository roots (#746); fetched bases and dependency composition preserved (#743); review authority packets refreshed and foreign-scope repairs blocked (#742).
- Docs: Codex first-launch trust and hook-review screens (#807); reviewers on luna@high with opus@low the only fallback.
- Versioning: single source `src/office/_version.py`; `pyproject.toml` reads it dynamically; the `VERSION` file and manifest versions are removed; mandatory per-PR semver bump in `AGENTS.md`.
