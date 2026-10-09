"""The `office` command line.

Flat semantic verbs. Default output is a few lines ending in `next:`; hashes,
paths, event ids and routing detail are behind `office inspect`, --verbose and
--json. Exit codes: 0 ok, 1 failure, 2 usage, 3 no/ambiguous run,
4 refused by a gate or rule, 5 pinned runtime unavailable.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from office import version
from office.result import Result

PRIMARY = """\
Auto Office {ver}

  office start "<goal>"             create a run; queues the planner when policy requires one
  office resume [run]               bind this session to a run and show where it stands
  office status                     what matters now, ending with the next legal action
  office wait [--timeout S]         block until something needs you (exit 0), a stall (3), an agent's
                                    question (5), or timeout (124)
  office dispatch <task>... [--parallel]
                                    launch tasks (routing, worktrees, leases are automatic)
  office preflight                  executor: read-only checks before submit (ready|fix|wait|stop)
  office submit                     planner/executor: submit your plan or your work
  office raise [--kind question|blocker|scope-request] [--path P ...] -- "<text>"
                                    executor: stop for a decision or a blocker without submitting (office raise --help)
  office amend <scope> -- "<delta>" change the plan (scope: plan, T2, or T2,T3)
  office amend <task> --add-check "<cmd>" | --add-accept "<text>" | --set depends=T1  change the task's enforced contract
  office amend <scope> --no-review --reason "<why>" -- "<delta>"   ordinary amendment, no plan review
                                    (only matters while plan review is open; a closed review never reopens)
  office amend route <task> --as <harness>/<model>[@effort] --quote "<words>" [--restart]
                                    declare a pending task's route, or re-record a live dispatch's model
                                    (same harness); --restart relaunches it
  office ack <amendment-id>         worker: record that you applied a delivered amendment
  office rerun <task> --resume|--fresh [--reroute | --as <harness>/<model>[@effort]] [--review-as ...]
                                    after a worker ends: continue its session, or start a new one with the findings
  office prompt <task|dispatch> -- "<message>"
                                    message a live pane agent and confirm it was submitted (never herdr pane run)
  office answer <task|dispatch> <n> | -- "<text>"
                                    answer the question a pane agent is waiting on (a number presses that option)
                                    or a worker's `office raise`; a headless worker is queued the answer
  office dismiss <task|dispatch|--all>
                                    close the kept panes of ended dispatches (final text is saved first)
  office close                      finish the run after acceptance and landing
                                    (--landed-externally <pr>: its work merged through another PR)
  office benchmarks brief|submit <f> opted-in runs: one background refresh of missing benchmark scores

  office list                       runs in this repository (--all for every run)
  office inspect [run|task|gate|evidence|events|route|learner|trust|convergence] [id]
  office decide <lane> escalate|continue|waive|stop --quote "<user's words>"
                                    the user's choice once a lane review spent its 3 RECHECK rounds
  office waive <lane> --reason "<why the open findings are acceptable>"
                                    the orchestrator's choice once a lane review spent its RECHECK rounds: accept the
                                    residual risk (the verdict stays RECHECK; not landing authority)
  office disposition <scope>:<F-id> fix|fixed|dismissed|follow-up -- "<note>"
                                    close a non-blocking (APPROVED) finding, or a plan finding the orchestrator
                                    owns after plan review's round cap; fix routes it, no re-review
  office review plan --quote "<user's words>" [--rounds N]
                                    the user's explicit request for another plan review (a new bounded cycle)
  office config [<key> [<value>]]   read or set preferences like git config (--list, --unset, --edit, --repo)
  office queue list|add|pause|resume|priority|demote|auto
                                    the machine-level scheduler queue (--run <id> [--task T] for a run)
  office web start|stop|status|serve [--port N] [--fixture small|large]
                                    the local Office web UI (loopback only)
  office setup                      interactive: choose preferred agents, models, and cost policy
  office doctor                     check the installation, hooks, and runtimes
  office update [--check]           update Auto Office itself; --check looks for release/source updates
  office upgrade [run] [--to X.Y]   move a run to a newer release line (dry run; --apply)
  office prune [--run <id>]         show finished runs that office prune -f would remove

Global flags: --run <id>, --json, --verbose. Every command ends with `next:`.
Principles: MANIFESTO.md. Operating contract: SKILL.md and docs/review-convergence.md.
"""

RAISE_HELP = """\
office raise [--kind question|blocker|scope-request] [--path P ...] -- "<text>"

Executor (inside your task worktree): stop on something only the orchestrator resolves, without submitting.
It needs no commit and records a durable event for this run, task, dispatch and session. Your task is
blocked (not live) until the orchestrator answers; `office wait` wakes it once and `office status` keeps it
listed. Use it instead of `office submit` when you cannot finish: submit takes finished work only.

  question       you need a decision (ambiguous spec, a choice between approaches)
  blocker        something outside your control stops you (a missing prerequisite, a broken environment)
  scope-request  finishing needs a file outside your SCOPE: add --path <repo-relative path> for each

The same text from this session again records nothing new. The answer arrives as a message in this session
(a prompt in a pane, your next office command if headless); if the session had ended it is in the next
session's brief. An answer never changes your contract: a scope-request is granted only by a contract
amendment, which also unblocks you. Then continue and run `office submit` when the work is done.
"""

SUBMIT_HELP = """\
office submit

Executor (inside your task worktree): captures the worktree exactly as it is,
committed and uncommitted, and starts every applicable check and review.
Submitting the same tree again is safe; it reports the existing submission.

A submit refused for a file outside your scope blocks the task and the orchestrator sees it. To
ask for more scope instead of reverting:
  office submit --request-scope <path> [--request-scope <path> ...] -- "<reason>"
It records a contract-amendment request with the diff and waits; you are told to resubmit once amended.

Substantive in-scope work needs a current self-review ledger (OFFICE_SELF_REVIEW.md, format in your brief) whose
COMMIT is the HEAD you submit; submit records it against the revision. A stale ledger or uncommitted work it does
not cover is refused. Empty and read-only work is exempt on its own. On a low-risk task (inline self-review tier)
a trivial or mechanical change may submit without a ledger:
  office submit --self-review-exempt trivial|mechanical -- "<reason>"
The exemption is recorded on the revision. Independent review applies either way.

Planner / orchestrator planning inline: submits .office/plans/<run>/PLAN.md (one draft per run). Format:

{fmt}
Checks (task `checks:` and the run-level `checks:` under Requirements):
- Every check must be non-mutating. A check that edits the tree (a formatter
  or `lint --fix`) makes the task's checks STALE; use the check-only form.
- Run-level checks run on a freshly composed integration worktree that holds
  only what is in git, recreated on every compose. Installed dependencies
  (node_modules, a virtualenv) are absent, so a check needing them must install
  them itself, e.g. `pnpm install --frozen-lockfile && pnpm lint`. Otherwise
  it reports "command not found" and integration stops UNAVAILABLE.
- Inline planning: to change a task's contract, edit its entry in
  the run's PLAN.md first, then office amend <T> --contract; an amendment whose
  PLAN.md does not change the named task is refused.

Plan review (convergence-v1 runs) reviews the initial plan only. RECHECK holds only
the tasks its blocking findings name: revise the plan and submit again for the same
reviewer. APPROVED dispatches at once and closes plan review; its findings are fixed
or dispositioned (office disposition plan:P<n> ...) with no re-review. At the round
cap (3, or the user's office start --plan-review-rounds N) a RECHECK closes plan
review unapproved: the orchestrator owns the remaining findings, fixes each in the
plan or accepts it, and records office disposition plan:P<n> fixed|dismissed|follow-up
-- "<rationale>". Once closed, no amendment is reviewed again; only the user reopens
it (office review plan --quote ...). INTAKE_GAP names a user decision: ask the user,
record it with office amend requirements --quote ..., then revise the plan.

Runs pinned to the v3.1 review contract only:
Plan defects (PLAN_DEFECT): trace each to the requirement or assumption behind it.
A plan-only cause is fixed in the plan. A requirement or assumption cause goes to
the user, and the revision that follows their answer is submitted with it:
  office submit --redirect P3 --root-cause "<requirement or assumption>" \
      --quote "<user's words>" [--requirement "<new requirement>"] [--reviewer same|fresh]
A redirect resets the plan-review round budget. --requirement records requirements
r(n+1), authorized by the same quote. --reviewer same resumes the reviewer that raised
the defect; fresh (the default) routes a new reviewer on another route. The defect
still clears only when a reviewer says CLEARED. If the user judges the defect wrong:
  office approve waive P3 --quote "<user's words>" [--root-cause "<why>"]"""


def _redirect_args(s: argparse.ArgumentParser) -> None:
    s.add_argument("--redirect", metavar="P<n>", help="the plan defect the user redirected (with --quote)")
    s.add_argument("--root-cause", help="with --redirect: the requirement or assumption that causes the defect")
    s.add_argument("--requirement", help="with --redirect: the redirected requirement; records r(n+1)")
    s.add_argument("--reviewer", choices=("same", "fresh"), help="with --redirect: who re-reviews (default fresh)")


def _redirect(args) -> dict | None:
    if not getattr(args, "redirect", None):
        return None
    return {"defect": args.redirect, "quote": args.quote, "root_cause": args.root_cause,
            "requirement": args.requirement, "reviewer": args.reviewer}


def _parser() -> argparse.ArgumentParser:
    def flags(**default):
        f = argparse.ArgumentParser(add_help=False)
        f.add_argument("--run", dest="run_arg", **default)
        f.add_argument("--state-dir", dest="state_dir", help=argparse.SUPPRESS, **default)
        f.add_argument("--harness", help=argparse.SUPPRESS, **default)
        f.add_argument("--session", help=argparse.SUPPRESS, **default)
        f.add_argument("--json", action="store_true", **default)
        f.add_argument("--verbose", "-v", action="store_true", **default)
        return f

    # Subcommands repeat the global flags without defaults, so a flag given
    # before the subcommand (office --run X status) is not reset by it.
    common = flags(default=argparse.SUPPRESS)
    p = argparse.ArgumentParser(prog="office", add_help=False, parents=[flags()])
    p.add_argument("-h", "--help", action="store_true")
    p.add_argument("--version", action="store_true")
    sp = p.add_subparsers(dest="cmd")

    s = sp.add_parser("start", parents=[common], add_help=True)
    s.add_argument("goal", nargs="?")
    s.add_argument("--gear", choices=["direct", "direct+review", "light", "quick", "express", "full"])
    s.add_argument("--playbook", default="Change", choices=["Change", "Restructure", "Investigate", "Prototype", "Visual"])
    s.add_argument("--blast-radius", choices=["local", "repo", "production", "production-data"])
    s.add_argument("--size-class", choices=["S", "M", "L", "XL"])
    s.add_argument("--irreversible", action="store_true")
    s.add_argument("--volume", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--interview", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--adversarial", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--set", action="append", help="config override key.path=value")
    s.add_argument("--base", help=argparse.SUPPRESS)
    s.add_argument("--planner", choices=["dedicated", "inline"], help="override who writes the plan")
    s.add_argument("--issue", help="the tracking GitHub issue (number or URL)")
    s.add_argument("--no-prs", action="store_true", help="keep task work local: no pushes or per-task PRs")
    s.add_argument("--benchmark-refresh", action="store_true",
                   help="the user's intake opt-in: refresh missing benchmark scores in the background")
    s.add_argument("--end-state", choices=["ask", "preview", "merge", "e2e"],
                   help="the user's intake answer: how far to go after the task PRs")
    for step in ("preview", "prod", "verify"):
        s.add_argument(f"--deploy-{step}", metavar="CMD", help=f"the user-confirmed {step} command")
    s.add_argument("--plan-review-rounds", type=int, metavar="N",
                   help="the user's intake choice: at most N substantive rounds for the initial plan review "
                        "(default 3)")
    s.add_argument("--review-rounds", type=int, metavar="N",
                   help="at most N substantive rounds per lane or shared-scope convergence review (default 3; "
                        "config review.max_rounds)")
    s.add_argument("--from-run", metavar="RUN",
                   help="start a new run carrying an earlier run's requirements and plan draft (e.g. to move "
                        "v3.1 work onto the current review contract); the earlier run is not changed")

    s = sp.add_parser("resume", parents=[common])
    s.add_argument("target", nargs="?")
    sp.add_parser("status", parents=[common])
    s = sp.add_parser("wait", parents=[common])
    s.add_argument("--timeout", type=float, default=1500.0, help="seconds before exit 124 (default 1500)")
    s.add_argument("--poll", type=float, default=10.0, help=argparse.SUPPRESS)
    s = sp.add_parser("dispatch", parents=[common])
    s.add_argument("tasks", nargs="*")
    s.add_argument("--parallel", action="store_true")
    s.add_argument("--route", help="advanced: override the route (harness/model@effort)")
    s.add_argument("--reroute", action="store_true",
                   help="ignore the plan's route slate and route from current evidence")
    s.add_argument("--as", dest="as_model", metavar="HARNESS/MODEL[@EFFORT]",
                   help="run the executor on this model, bypassing registry, trust and floors (a user override)")
    s.add_argument("--cli", metavar="ARGV", help="with --as: start exactly this agent argv in a herdr pane")
    s.add_argument("--external", action="store_true",
                   help="prepare the dispatch and print how to start it; launch nothing")
    s.add_argument("--review-as", metavar="HARNESS/MODEL[@EFFORT]",
                   help="pin the code reviewer; it always runs as a fresh session, never the executor's")
    s.add_argument("--review-cli", metavar="ARGV", help="with --review-as: start exactly this reviewer argv in herdr")
    s.add_argument("--review-external", action="store_true",
                   help="with --review-as: you start the reviewer; Office reads its review file")
    sp.add_parser("preflight", parents=[common])
    s = sp.add_parser("submit", parents=[common], add_help=False)
    s.add_argument("-h", "--help", action="store_true")
    s.add_argument("--plan", help=argparse.SUPPRESS)
    s.add_argument("--quote", help="the user's words (a defect redirect)")
    s.add_argument("--request-scope", action="append", default=[], metavar="PATH",
                   help="executor: ask the orchestrator to add PATH to this task's scope (reason after --)")
    s.add_argument("--self-review-exempt", choices=["trivial", "mechanical"], metavar="TYPE",
                   help="executor: submit with no self-review ledger because the change is trivial or mechanical "
                        "(low-risk tasks only; reason after --)")
    s.add_argument("reason", nargs="*", help=argparse.SUPPRESS)
    _redirect_args(s)
    s = sp.add_parser("raise", parents=[common], add_help=False)
    s.add_argument("-h", "--help", action="store_true")
    s.add_argument("--kind", default="question", metavar="KIND",
                   help="question (default), blocker or scope-request")
    s.add_argument("--path", action="append", default=[], metavar="P",
                   help="a repo-relative path the raise is about (required for scope-request)")
    s.add_argument("text", nargs="*", help=argparse.SUPPRESS)
    s = sp.add_parser("amend", parents=[common])
    s.add_argument("scope")
    s.add_argument("delta", nargs="*")
    s.add_argument("--contract", action="store_true", help="a contract amendment (planner-owned)")
    s.add_argument("--no-review", action="store_true",
                   help="orchestrator veto while plan review is open: an ordinary amendment queues no plan review "
                        "(needs --reason); once plan review closes no amendment is reviewed")
    s.add_argument("--reason", help="why plan review is vetoed (with --no-review)")
    s.add_argument("--requirements", action="store_true", help="a user-originated requirements change")
    s.add_argument("--quote", help="the user's words (requirements changes, defect redirects, route changes)")
    s.add_argument("--as", dest="route_as", metavar="HARNESS/MODEL[@EFFORT]",
                   help="amend route: the live dispatch's new model/effort (same harness)")
    s.add_argument("--restart", action="store_true",
                   help="amend route: interrupt the agent and relaunch it on the new route")
    s.add_argument("--drop-criterion", action="append", default=[], metavar="TEXT",
                   help="requirements: remove the frozen done criterion this names")
    s.add_argument("--add-criterion", action="append", default=[], metavar="TEXT",
                   help="requirements: add a done criterion")
    s.add_argument("--add-accept", action="append", default=[], metavar="TEXT",
                   help="add an acceptance criterion to the named task's contract")
    s.add_argument("--drop-accept", action="append", default=[], metavar="TEXT", help="drop a task acceptance criterion")
    s.add_argument("--add-check", action="append", default=[], metavar="CMD",
                   help="add a required check (run at the gate) to the named task's contract")
    s.add_argument("--drop-check", action="append", default=[], metavar="CMD", help="drop a required check")
    s.add_argument("--set", action="append", default=[], dest="set_fields", metavar="KEY=VALUE",
                   help="set the named task's depends, scope, or interfaces (scope/interfaces need --contract)")
    _redirect_args(s)
    s = sp.add_parser("ack", parents=[common])
    s.add_argument("amendment")
    s = sp.add_parser("land", parents=[common])
    mode = s.add_mutually_exclusive_group()
    for m in ("merge", "preview", "e2e"):
        mode.add_argument(f"--{m}", dest="land_mode", action="store_const", const=m)
    s.add_argument("--quote", help="the user's words choosing this end state (ask mode)")
    s.add_argument("--detect", action="store_true", help="propose deploy commands for intake")
    s.add_argument("--rebase", action="store_true", help="move the run onto the newer default branch first")
    s.add_argument("--redeploy", action="store_true", help="with --e2e: deploy prod again from the merged tree")
    s.add_argument("--mark-deployed", action="store_true",
                   help="with --e2e: the operator confirms the merged tree is already live in prod")
    s = sp.add_parser("close", parents=[common])
    s.add_argument("--handoff", help="PR URL or branch handed to the user for merge")
    s.add_argument("--abandon", metavar="REASON", help="end the run without landing")
    s.add_argument("--landed-externally", metavar="PR", help="the run's work landed through this merged PR")
    s.add_argument("--quote", help="the user's words, when the merged PR does not contain every accepted revision")
    s = sp.add_parser("list", parents=[common])
    s.add_argument("--all", action="store_true")
    s = sp.add_parser("benchmarks", parents=[common])
    s.add_argument("action", choices=["brief", "submit"])
    s.add_argument("file", nargs="?")
    s = sp.add_parser("inspect", parents=[common])
    s.add_argument("what", nargs="?")
    s.add_argument("ident", nargs="?")
    s = sp.add_parser("config", parents=[common])
    s.add_argument("key", nargs="?", help="dotted key, e.g. roles.code_reviewer.preferred_seed")
    s.add_argument("value", nargs="?", help="new value (YAML); a preferred_seed also takes claude/sonnet@high,codex/luna@xhigh")
    s.add_argument("--user", dest="tier", action="store_const", const="user", help="the user file (default for writes)")
    s.add_argument("--repo", dest="tier", action="store_const", const="repo", help="this repository's .auto-office/config.yaml")
    s.add_argument("--unset", action="store_true")
    s.add_argument("-l", "--list", dest="list_", action="store_true", help="what the files set (--all: every effective value)")
    s.add_argument("--all", dest="all_", action="store_true")
    s.add_argument("-e", "--edit", action="store_true", help="open the file in $EDITOR, then validate it")
    s.add_argument("--path", action="store_true", help="print the config file path(s)")
    s.add_argument("--show-origin", dest="origin", action="store_true")
    s.add_argument("--force", action="store_true", help="set a key the shipped config does not define")
    s.add_argument("--apply-routing", action="store_true",
                   help="with --run: re-pin roles and routing from the current config files into that run")
    s.add_argument("--quote", help="with --apply-routing: the user's words authorizing the re-pin")
    s = sp.add_parser("queue", parents=[common])
    s.add_argument("action", choices=["list", "add", "pause", "resume", "priority", "demote", "auto"])
    s.add_argument("target", nargs="?", help="queue item id, issue ref (add), priority (priority) or on|off|status (auto)")
    s.add_argument("value", nargs="?", help="the new priority (priority)")
    s.add_argument("--task", help="with --run: one task of the run")
    s.add_argument("--title")
    s.add_argument("--priority", choices=["urgent", "high", "normal", "low"])
    s.add_argument("--reason")
    s.add_argument("--command-id", dest="command_id", help="idempotency id; a repeat returns the first receipt")
    s = sp.add_parser("web", parents=[common])
    s.add_argument("action", choices=["start", "stop", "status", "serve"])
    s.add_argument("--host", default="127.0.0.1", help="loopback address to bind (127.0.0.1, localhost or ::1)")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--fixture", choices=["small", "large"],
                   help="serve a synthetic workspace with fake GitHub and launcher (never the real runs.db)")
    s = sp.add_parser("setup", parents=[common])
    s.add_argument("--user", dest="tier", action="store_const", const="user")
    s.add_argument("--repo", dest="tier", action="store_const", const="repo")
    s.add_argument("-y", "--yes", action="store_true", help="write without the final confirmation")
    s = sp.add_parser("doctor", parents=[common])
    s.add_argument("--fix", action="store_true")
    s.add_argument("--probe-vision", action="store_true", help="run image-capability probes on visual routes (uses quota)")
    s = sp.add_parser("update", parents=[common])
    s.add_argument("--check", action="store_true", help="check PyPI and a source checkout's upstream without installing updates")
    s = sp.add_parser("upgrade", parents=[common])
    s.add_argument("target", nargs="?", help="the run (default: this session's run)")
    s.add_argument("--to", metavar="X.Y", help="the release line (default: this runtime's)")
    s.add_argument("--apply", action="store_true", help="commit the upgrade (default is a dry run)")
    s = sp.add_parser("prune", parents=[common])
    s.add_argument("-f", "--force", action="store_true")
    s = sp.add_parser("install", parents=[common])
    s.add_argument("--only", dest="only", action="append", help="limit to one harness")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--migrate-legacy-hooks", action="store_true")
    s.add_argument("--shell-guard", dest="shell_guard", action="store_true", default=None,
                   help="Claude: add a Bash hook that rewrites `sed -i` to `sed -i ''` on macOS")
    s.add_argument("--no-shell-guard", dest="shell_guard", action="store_false", help="remove that hook")
    s = sp.add_parser("uninstall", parents=[common])
    s.add_argument("--purge", action="store_true")
    # Authority and compatibility: discoverable, never in role briefs.
    s = sp.add_parser("approve", parents=[common])
    s.add_argument("target")
    s.add_argument("extra", nargs="*")
    s.add_argument("--quote")
    s.add_argument("--root-cause", help="with waive P<n>: why the user judged the defect wrong")
    s.add_argument("--by", help="approve visual: the reviewer route that wrote --report (harness/model[@effort])")
    s.add_argument("--report", help="approve visual: the review file to record as the visual gate result")
    s.add_argument("--as", dest="actor", choices=("user", "orchestrator"), default="user",
                   help="waive: who waives (orchestrator only with delegated landing authority; needs --reason)")
    s.add_argument("--reason", dest="waive_reason", help="waive: why the unmet gate is accepted (required)")
    # Convergence contract (#337): the operator's round-cap decision, finding
    # dispositions, and the orchestrator's degraded fallback review.
    s = sp.add_parser("decide", parents=[common])
    s.add_argument("scope", help="a lane (L-T1) or shared scope (S-T1+T3)")
    s.add_argument("choice", choices=("escalate", "continue", "waive", "stop"))
    s.add_argument("--quote", help="the user's own words")
    s.add_argument("--reason", help="waive: why the unmet gate is accepted")
    s = sp.add_parser("waive", parents=[common])
    s.add_argument("scope", help="a lane (L-T1) or shared scope (S-T1+T3) that spent its round cap")
    s.add_argument("--reason", help="why the open findings are acceptable (substantive, durable)")
    s = sp.add_parser("disposition", parents=[common])
    s.add_argument("finding", help="<scope>:<code>[,<code>] (e.g. L-T1:F2 or plan:P1)")
    s.add_argument("how", choices=("fix", "fixed", "dismissed", "follow-up"))
    s.add_argument("note", nargs="*")
    s = sp.add_parser("review", parents=[common])
    s.add_argument("target", help="<scope>:convergence|visual, or plan (the user's request for another plan review)")
    s.add_argument("--report", help="<scope>:<kind>: your review, in the reviewer reply format")
    s.add_argument("--quote", help="plan: the user's own words asking for another plan review")
    s.add_argument("--rounds", type=int, help="plan: at most this many substantive rounds (default 3)")
    s.add_argument("--inspected", nargs="*", default=[], help="visual: every screenshot you inspected")
    s = sp.add_parser("revoke", parents=[common])
    s.add_argument("task", help="a task id, a dispatch id, or `integration` (its reviews)")
    s.add_argument("--reason", default="orchestrator revoke")
    s = sp.add_parser("rerun", parents=[common])
    s.add_argument("task")
    s.add_argument("--resume", action="store_true", help="continue the ended session (native harness resume)")
    s.add_argument("--fresh", action="store_true", help="start a new session with the open findings in its brief")
    s.add_argument("--reroute", action="store_true",
                   help="with --fresh: route from current evidence instead of keeping the original route")
    s.add_argument("--as", dest="as_model", metavar="HARNESS/MODEL[@EFFORT]",
                   help="run on this route (a user override, as dispatch --as); --resume needs the same harness")
    s.add_argument("--cli", metavar="ARGV", help="with --as: start exactly this agent argv in a herdr pane")
    s.add_argument("--external", action="store_true",
                   help="prepare the rerun and print how to start it; launch nothing")
    s.add_argument("--review-as", metavar="HARNESS/MODEL[@EFFORT]",
                   help="pin the code reviewer for this task (as dispatch --review-as)")
    s.add_argument("--review-cli", metavar="ARGV", help="with --review-as: start exactly this reviewer argv in herdr")
    s.add_argument("--review-external", action="store_true",
                   help="with --review-as: you start the reviewer; Office reads its review file")
    s = sp.add_parser("prompt", parents=[common])
    s.add_argument("target", nargs="?")
    s.add_argument("message", nargs="*")
    s = sp.add_parser("answer", parents=[common])
    s.add_argument("target", nargs="?")
    s.add_argument("message", nargs="*")
    s = sp.add_parser("dismiss", parents=[common])
    s.add_argument("target", nargs="?")
    s.add_argument("--all", dest="dismiss_all", action="store_true")
    s = sp.add_parser("raw", add_help=False)
    s.add_argument("rest", nargs=argparse.REMAINDER)
    s = sp.add_parser("hook", add_help=False)
    s.add_argument("event")
    s.add_argument("--harness", dest="hook_harness", default="claude")
    s.add_argument("--office-managed", action="store_true")
    s = sp.add_parser("_job", add_help=False)
    s.add_argument("job_id")
    s.add_argument("--attempt", dest="job_attempt", default=None)
    s = sp.add_parser("_supervise", add_help=False)
    s.add_argument("dispatch_id")
    return p


# ------------------------------------------------------------------ output

def emit(res: Result, args, ok: bool = True) -> int:
    if getattr(args, "json", False):
        print(json.dumps({"ok": ok, "lines": res.lines, "notices": res.notices, "next": res.next, "data": res.data,
                          **({"final": res.final} if res.final else {})},
                         indent=2, sort_keys=True, default=str))
        return res.exit_code
    out = list(res.lines)
    if getattr(args, "verbose", False):
        out += res.verbose
    out += res.notices
    if res.next:
        out.append(f"next: {res.next}")
    if res.final:
        out.append(res.final)
    if out:
        print("\n".join(out))
    return res.exit_code


def emit_error(err: OfficeError, args) -> int:
    if getattr(args, "json", False):
        print(json.dumps({"ok": False, "error": {"category": err.category, "message": err.message, "scope": err.scope,
                                                 "preserved": err.preserved, "next": err.next_step, **err.data}},
                         indent=2, sort_keys=True, default=str))
        return err.exit_code
    lines = [f"blocked: {err.category}: {err.message}"]
    for cand in err.data.get("candidates") or []:
        lines.append(f"  {cand}")
    if err.scope:
        lines.append(f"scope: {err.scope}")
    if err.preserved:
        lines.append(f"preserved: {err.preserved}")
    if err.next_step:
        lines.append(f"next: {err.next_step}")
    if err.data.get("final"):
        lines.append(err.data["final"])
    print("\n".join(lines))
    return err.exit_code


# ------------------------------------------------------------------ dispatch

def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "raw":
        from office import compat
        return compat.raw(argv[1:])
    if argv and argv[0] == "hook":
        from office import hooks
        return hooks.main(argv[1:])
    from office.state import OfficeError
    parser = _parser()
    args, unknown = parser.parse_known_args(argv)
    if unknown and args.cmd not in ("amend",):
        run_like = [u for u in unknown if re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]+)*", u)]
        if args.cmd and len(run_like) == 1 and len(unknown) == 1:
            # `office close <run>`: the run is a flag on every command, not a positional.
            parser.error(f"unrecognized arguments: {run_like[0]} (name the run with --run: "
                         f"office --run {run_like[0]} {args.cmd} ...)")
        parser.parse_args(argv)  # raises the usage error
    if args.cmd == "submit" and args.reason and not (args.request_scope or args.self_review_exempt):
        # The reason text belongs to --request-scope or --self-review-exempt; a stray argument is still a usage error.
        parser.error(f"unrecognized arguments: {' '.join(args.reason)}")
    if args.version:
        print(version.current())
        return 0
    if args.cmd is None or (args.help and args.cmd is None):
        from office import planfile
        print(PRIMARY.format(ver=version.current()).rstrip())
        return 0 if args.cmd is None and (args.help or not argv) else 2
    try:
        return _run(args, unknown)
    except OfficeError as err:
        if args.cmd == "close":  # every office close path ends with its report line
            from office import closeout
            err.data.setdefault("final", closeout.done(f"stopped early ({err.category}); nothing closed or cleaned up"))
        return emit_error(err, args)
    except KeyboardInterrupt:
        return 130


def _con():
    from office import db
    return db.connect()


def _target(con, args, require_run=True):
    from office import discovery, frontdoor
    run_arg = getattr(args, "run_arg", None) or (args.target if args.cmd == "resume" else None)
    target = discovery.resolve(con, run_arg=run_arg,
                               state_dir=args.state_dir, harness=args.harness, session=args.session)
    if target.run is not None:
        frontdoor.ensure_runtime(target.run)
    if target.dispatch is not None:
        # An executor that lost its env: act as that dispatch (the lease still fences it).
        for key, value in (("OFFICE_RUN_ID", target.run["id"]), ("OFFICE_TASK_ID", target.dispatch["task_id"] or ""),
                           ("OFFICE_DISPATCH_ID", target.dispatch["id"]), ("OFFICE_ROLE", target.dispatch["role"])):
            os.environ.setdefault(key, value)
    return target


def _legacy_result(target) -> Result:
    from office import legacy
    msg, nxt = legacy.guidance(target.legacy)
    res = Result(lines=[msg], next=nxt, exit_code=0)
    return res


def _run(args, unknown) -> int:
    cmd = args.cmd
    cwd = Path.cwd()
    if cmd == "_job":
        from office import jobs
        return jobs.main_job(args.job_id, args.job_attempt)
    if cmd == "_supervise":
        from office import dispatch
        return dispatch.supervise(args.dispatch_id)
    from office.state import OfficeError
    if cmd == "start":
        from office import lifecycle, runtime_default
        if not args.goal and not args.from_run:
            raise OfficeError("usage", "office start needs a goal", next_step='office start "<goal>"', exit_code=2)
        runtime_default.require_new_run_runtime()
        res = lifecycle.start(args.goal, cwd=cwd, gear=args.gear, playbook=args.playbook, blast_radius=args.blast_radius,
                              size_class=args.size_class, irreversible=args.irreversible, volume=args.volume,
                              interview=args.interview, adversarial=args.adversarial, sets=args.set,
                              harness=args.harness, session=args.session, base=args.base, planner=args.planner,
                              issue=args.issue, no_prs=args.no_prs, end_state=args.end_state,
                              benchmark_refresh=args.benchmark_refresh, from_run=args.from_run,
                              plan_review_rounds=args.plan_review_rounds, review_rounds=args.review_rounds,
                              deploy={k: v for k in ("preview", "prod", "verify")
                                      if (v := getattr(args, f"deploy_{k}"))})
        return emit(res, args)
    if cmd == "list":
        from office import lifecycle
        con = _con()
        try:
            return emit(lifecycle.list_runs(con, all_runs=args.all, cwd=cwd), args)
        finally:
            con.close()
    if cmd == "upgrade":
        # Resolved without the front door: the run is on another line by design.
        from office import discovery, upgrade
        con = _con()
        try:
            target = discovery.resolve(con, run_arg=args.target or args.run_arg, state_dir=args.state_dir,
                                       harness=args.harness, session=args.session)
            if target.legacy is not None:
                raise OfficeError("legacy-run", f"run {target.legacy.run_id[:8]} is a 3.0 run pinned to its plugin_commit; "
                                  "it is not upgraded", next_step="finish it on its pinned runtime (office resume)")
            return emit(upgrade.upgrade(con, target.run, to=args.to, apply=args.apply), args)
        finally:
            con.close()
    if cmd == "prune":
        from office import prune
        con = _con()
        try:
            only = prune.select_run(con, args.run_arg) if args.run_arg else None
            return emit(prune.force(con, only) if args.force else prune.dry_run(con, only), args)
        finally:
            con.close()
    if cmd == "config" and args.apply_routing:
        from office import configcmd
        con = _con()
        try:
            from office import state
            run_arg = getattr(args, "run_arg", None)
            run = state.find_run(con, run_arg) if run_arg else None
            if run is None:
                raise OfficeError("usage", "--apply-routing needs --run <id>", exit_code=2,
                                  next_step="office list shows run ids")
            return emit(configcmd.apply_run_routing(con, run, args.quote), args)
        finally:
            con.close()
    if cmd == "config":
        from office import configcmd
        return emit(configcmd.config(key=args.key, value=args.value, tier=args.tier, unset=args.unset, list_=args.list_,
                                     all_=args.all_, edit=args.edit, path=args.path, origin=args.origin,
                                     force=args.force, cwd=cwd), args)
    if cmd == "queue":
        from office import queuecmd
        con = _con()
        try:
            return emit(queuecmd.run_command(con, args), args)
        finally:
            con.close()
    if cmd == "web":
        from office.web import server
        out = server.main(args)
        return out if isinstance(out, int) else emit(out, args)
    if cmd == "setup":
        from office import configcmd
        return emit(configcmd.setup(tier=args.tier or "user", yes=args.yes, cwd=cwd), args)
    if cmd == "doctor":
        from office import doctor
        return emit(doctor.doctor(fix=args.fix, probe_vision=args.probe_vision), args)
    if cmd == "update":
        from office import self_update
        return emit(self_update.check() if args.check else self_update.update(), args)
    if cmd == "install":
        from office import install
        return emit(install.install(only=args.only, dry_run=args.dry_run, migrate_legacy=args.migrate_legacy_hooks,
                                    shell_guard=args.shell_guard), args)
    if cmd == "uninstall":
        from office import install
        return emit(install.uninstall(purge=args.purge), args)
    if cmd == "land" and args.detect:
        from office import land, paths
        ident = paths.repo_identity(cwd)
        return emit(land.detect_deploy(ident[0] if ident else Path(cwd or ".")), args)
    if cmd == "submit" and args.help:
        from office import briefs
        print(SUBMIT_HELP.format(fmt=briefs.PLAN_FORMAT))
        return 0
    if cmd == "raise" and args.help:
        print(RAISE_HELP.rstrip())
        return 0
    con = _con()
    try:
        target = _target(con, args)
        if target.legacy is not None:
            res = _legacy_result(target)
            if target.note:
                res.notices.append(target.note)
            return emit(res, args)
        run = target.run
        res = _dispatch_command(con, run, args, unknown, cwd, target)
        if target.note:
            res.notices.append(target.note)
        if cmd not in ("status", "resume", "preflight"):
            from office import guide, state
            guide.piggyback(con, state.get_run(con, run["id"]), res)
        return emit(res, args)
    finally:
        con.close()


def _dispatch_command(con, run, args, unknown, cwd, target) -> Result:
    cmd = args.cmd
    if cmd == "resume":
        from office import lifecycle
        return lifecycle.resume(con, target, harness=args.harness, session=args.session, cwd=cwd)
    if cmd == "status":
        from office import guide, jobs, lifecycle, db, state
        if not state.is_terminal(run):
            from office import dispatch
            dispatch.reap_orphans(con, run)
            with db.transaction(con):
                lifecycle.reconcile(con, run)
            jobs.kick(con, run["id"])
        return guide.status(con, run, verbose=args.verbose, probe_panes=True)
    if cmd == "wait":
        from office import guide
        return guide.wait(con, run, timeout=args.timeout, poll=args.poll)
    if cmd == "dispatch":
        from office import dispatch
        return dispatch.dispatch(con, run, args.tasks, parallel=args.parallel, route=args.route,
                                 as_model=args.as_model, cli=args.cli, external=args.external,
                                 review_as=args.review_as, review_cli=args.review_cli,
                                 review_external=args.review_external, reroute=args.reroute)
    if cmd == "preflight":
        from office import preflight
        return preflight.preflight(con, run, cwd)
    if cmd == "raise":
        from office import raising
        return raising.raise_issue(con, run, cwd=cwd, kind=args.kind, text=" ".join(args.text or []), files=args.path)
    if cmd == "submit":
        from office import submit
        return submit.submit(con, run, cwd=cwd, plan_path=args.plan, redirect=_redirect(args),
                             request_scope=args.request_scope, exempt=args.self_review_exempt,
                             reason=" ".join(args.reason or []).strip())
    if cmd == "amend" and args.scope == "route":
        from office import routechange
        rest = [*(args.delta or []), *[u for u in unknown if u != "--"]]
        return routechange.change_route(con, run, rest[0] if len(rest) == 1 else None, args.route_as, args.quote,
                                        restart=args.restart)
    if cmd == "amend":
        from office import amend
        delta = " ".join([*(args.delta or []), *[u for u in unknown if u != "--"]]).strip()
        edits = {"add_accept": args.add_accept, "drop_accept": args.drop_accept, "add_checks": args.add_check,
                 "drop_checks": args.drop_check,
                 "set_fields": dict(f.split("=", 1) if "=" in f else (f, "") for f in args.set_fields)}
        if any(edits.values()) and not delta:
            delta = amend.edits_delta(args.scope, edits)
        return amend.amend(con, run, args.scope, delta, contract=args.contract, requirements=args.requirements,
                           quote=args.quote, cwd=cwd, redirect=_redirect(args),
                           drop_criteria=args.drop_criterion, add_criteria=args.add_criterion,
                           no_review=args.no_review, reason=args.reason, edits=edits)
    if cmd == "ack":
        from office import amend
        return amend.ack(con, run, args.amendment)
    if cmd == "land" and args.rebase:
        from office import land
        return land.rebase(con, run)
    if cmd == "land":
        from office import land
        return land.land(con, run, mode=args.land_mode, quote=args.quote, redeploy=args.redeploy,
                         mark_deployed=args.mark_deployed)
    if cmd == "close":
        from office import lifecycle
        if args.abandon:
            return lifecycle.abandon(con, run, args.abandon)
        if args.landed_externally:
            return lifecycle.close_landed_externally(con, run, args.landed_externally, args.quote)
        return lifecycle.close(con, run, handoff=args.handoff)
    if cmd == "benchmarks":
        from office import benchmarks
        if args.action == "brief":
            return benchmarks.brief(con, run)
        if not args.file:
            raise OfficeError("usage", "name the delta file", next_step=benchmarks.SUBMIT_FORM, exit_code=2)
        return benchmarks.submit(con, run, args.file)
    if cmd == "inspect":
        from office import inspect_cmd
        return inspect_cmd.inspect(con, run, args.what, args.ident)
    if cmd == "approve":
        from office import authority
        return authority.approve(con, run, args.target, args.quote, args.extra, root_cause=args.root_cause,
                                 by=args.by, report=args.report, actor=args.actor, reason=args.waive_reason)
    if cmd == "decide":
        from office import convergence
        return convergence.decide(con, run, args.scope, args.choice, quote=args.quote, reason=args.reason)
    if cmd == "waive":
        from office import convergence
        return convergence.cap_waive(con, run, args.scope, reason=args.reason)
    if cmd == "disposition":
        from office import convergence
        note = " ".join([*(args.note or []), *[u for u in unknown if u != "--"]]).strip()
        return convergence.disposition(con, run, args.finding, args.how, note)
    if cmd == "review" and args.target.lower() == "plan":
        from office import plans
        return plans.request_review(con, run, args.quote, args.rounds)
    if cmd == "review":
        from office import convergence
        if not args.report:
            from office.state import Usage
            raise Usage("missing-report", "a fallback review needs --report",
                        next_step="office review <scope>:convergence|visual --report <file>")
        return convergence.fallback_review(con, run, args.target, Path(args.report).expanduser(),
                                           inspected=args.inspected)
    if cmd == "revoke":
        from office import dispatch
        return dispatch.revoke(con, run, args.task, args.reason)
    if cmd == "rerun":
        from office import rerun
        return rerun.rerun(con, run, args.task.upper(), resume=args.resume, fresh=args.fresh, reroute=args.reroute,
                           as_model=args.as_model, cli=args.cli, external=args.external, review_as=args.review_as,
                           review_cli=args.review_cli, review_external=args.review_external)
    if cmd == "dismiss":
        from office import rerun
        return rerun.dismiss(con, run, args.target, all_=args.dismiss_all)
    if cmd == "prompt":
        from office import prompting
        text = " ".join([*(args.message or []), *[u for u in unknown if u != "--"]]).strip()
        return prompting.prompt(con, run, args.target, text)
    if cmd == "answer":
        from office import questions
        text = " ".join([*(args.message or []), *[u for u in unknown if u != "--"]]).strip()
        return questions.answer(con, run, args.target, text)
    from office.state import OfficeError
    raise OfficeError("usage", f"unknown command {cmd}", exit_code=2)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
