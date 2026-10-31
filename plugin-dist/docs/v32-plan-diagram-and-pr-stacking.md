# 3.2: plan diagram, stacked task PRs, intake end state

Decided with the user on 2026-09-30. Runs pinned to 3.1 keep 3.1 behavior.

## Decisions

- D1 Before plan approval, `office` renders an ASCII diagram: tasks, parallel vs
  stacked, predicted route (harness/model@effort) with a one-line why, and the
  checkpoint chain (task accepts, integration review, PRs, merge, deploy per the
  chosen end state). The orchestrator shows it in the approval question and the
  runtime writes it into PLAN.md.
- D2 Routes in the diagram are a preview. Dispatch re-resolves; if the pick
  differs, dispatch reports the delta and reason.
- D3 Parallel vs stacked derives from `depends`: no unmet dependency means a
  parallel root off the run base; a task with a dependency stacks on it. No new
  plan field.
- D4 One PR per task. Base is the parent task's branch when stacked, the run
  base (main) when a root.
- D5 Executors commit and push WIP to their task branch; the first push opens a
  draft PR. At submit the runtime commits leftovers onto the same branch and
  pushes, so the reviewed revision is the branch head.
- D6 Task PRs merge bottom-up with merge commits, unless the repo enforces
  another method.
- D7 Intake asks the end state: stop at PRs and ask after, preview deploy only,
  merge only, or merge + prod end-to-end.
- D8 Deploy route is detected (vercel.json, CI workflows, repo deploy skill),
  proposed at intake, confirmed by the user, and stored as named actions.
- D9 Squash- or rebase-only repos: after a parent merges, the runtime rebases
  the child `--onto` the new base, force-pushes with lease, retargets the PR.
  Gates re-run only if the tree changed.
- D10 A task rerun from scratch reuses its PR: force-push with lease plus a
  comment that attempt N replaced N-1; children restack.
- D11 Choosing merge + prod end-to-end at intake is the merge and deploy
  authority. No further confirmation.
- D12 GitHub content: route/why in the PR body, a one-line verdict and gate
  status comment per revision (no evidence), draft until the task is accepted.
  The plan diagram is not posted to GitHub.
- D13 The local integration branch and integration review stay. Passing them
  gates any merge. The integration branch never becomes a PR.
- D14 After approval, the diagram is shown again only on amend/replan, as a
  diff against the previous plan (for example `T4 route: codex/gpt-5.5@high ->
  claude/sonnet-5-5@medium`).
- D15 Version 3.2.

## Defaults not asked

- A1 Repos with no GitHub remote, or runs with blast_radius `local`, keep the
  3.1 flow: no push, no PRs.
- A2 Every task PR says `Part of #N`. The runtime closes the issue after the
  last merge, or leaves it to GitHub when the end state stops at PRs.
- A3 Before each merge, wait for required GitHub checks to pass. If branch
  protection requires an up-to-date branch, update it and re-check.
- A4 "Ask after" happens at closeout, listing PR links and gate status.

## Delivery

Stacked PRs on this branch family:

1. Plan diagram + route preview + amend diff (D1-D3, D14).
2. Push and stacked task PRs (D4-D6, D9, D10, D12, D13, A1-A3).
3. Intake end state + deploy detection + merge/deploy execution (D7, D8, D11, A4).
4. Skill/doc updates and 3.2 version bump (D15).
