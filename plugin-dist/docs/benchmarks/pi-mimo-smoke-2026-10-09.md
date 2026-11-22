# pi / MiMo-V2.6 live Office smoke — 2026-10-09

Live Auto Office dispatches to the pi routes registered in #479, from dogfood
run `6e7a9432` (issue #478). This records launch evidence only. Exit 0, a
landed prompt, or a running pane is **launch evidence, not model conformance**:
Office reads no model identity back from pi yet, so nothing here shows which
model actually served a turn. Only `medium` effort was dispatched; nothing here
claims any other `--thinking` level works or changes behavior.

## How Office launches pi

Rendered by the installed build (3.5.0, `adapters/seed/pi.yaml`) for the
dispatched candidate. The dispatch's launch record (`launch.json`) does not
store the argv, so this is the adapter's rendering, not a captured process line.

- Headless worker form (prompt on **stdin**, output on stdout, 60-minute cap):
  `pi --print --no-approve --no-extensions --no-mcp --model xiaomi-token-plan-sgp/mimo-v2.6-pro --thinking medium --no-session`
- Herdr pane form (`herdr agent start <name> --kind pi --pane <pane> -- <argv>`),
  where Office delivers the brief with `herdr agent prompt` after start:
  `pi --no-approve --no-extensions --no-mcp --model xiaomi-token-plan-sgp/mimo-v2.6-pro --thinking medium`

Before either launch, the adapter preflight runs `pi --offline --list-models`
(the pinned slug must be listed) and `pi auth check --model <slug>`.

## pi/mimo-v2.6-pro@medium (T1)

Route triple `pi@1/mimo-v2.6-pro@medium`, declared by the user with `--as`.

| Dispatch | Form | Result |
|---|---|---|
| `D6ead2f79` | headless (process), brief on stdin | Ran about 15 minutes with an empty output log. Revoked (SIGTERM) to relaunch in a Herdr pane after the missing pane form was found and fixed (commit "pi pane-hosted interactive form for Herdr delegation"). |
| `D2d81bc31` | Herdr pane `w7:p13` | Plain `office dispatch T1` followed the declared route ("declared route; user authority covers adapter trust"). The prompt landed (`prompt_landed: true`), and pi reported `mimo-v2.6-pro • medium` in its status line. Over 12.5 minutes it made 38 assistant turns and 56 shell calls, and wrote no file. Revoked on the user's request. |

Why `D2d81bc31` wrote nothing: the brief asked for "the exact argv form Office
used", the launch record does not store it, and the worktree was cut from
`b15dcc7`, before pi was registered, so it had no `pi.yaml` to read. The worker
spent its turns reconstructing the launch from `src/office/dispatch.py`. That is
a brief defect plus an evidence gap (no recorded argv), not a launch failure.

## pi/mimo-v2.6-flash@medium (T2)

Not dispatched through Office: the run was taken over before T2. The only flash
evidence is the direct probe recorded in #479 (outside Office):
`printf <prompt> | pi --print --no-approve --no-extensions --no-mcp --model xiaomi-token-plan-sgp/mimo-v2.6-flash --thinking medium --no-session`
created and ran `hello.py` in a throwaway git repo and exited 0.

## Follow-ups

- Record the rendered argv in each dispatch's `launch.json`, so evidence and
  briefs can quote it instead of reconstructing it.
- An Office-routed flash dispatch is still owed.
- Quota stays unknown for pi routes until #497.
