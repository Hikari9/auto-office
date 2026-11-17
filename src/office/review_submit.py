"""`office review submit`: a reviewer hands its verdict back through an explicit command (#1030).

Rico, 2026-11-02: reviewers hand back like executors (`office submit`). The command validates the reply in
the format its gate requires and records it at once: it writes the stored copy (`reply.txt`, the file every
reader already uses) and a submitted marker that ends the reviewer's wait without the settle and idle
heuristics. Writing the reply file directly, and a headless reviewer's stdout, stay as fallbacks.

Reviewers have no Office authority (`dispatch.write_agent_env` strips the dispatch identity). This command
honours only a per-dispatch secret, `OFFICE_REVIEW_TOKEN`, issued to that reviewer's session alone: it can
hand back its own review and nothing else, and every other command still refuses a reviewer.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import shlex
from pathlib import Path

from office import contract, db, paths, review_parse, state
from office.result import Result
from office.state import Refused, Usage
from office.util import atomic_write_json, now_iso, sha256_bytes

TOKEN_FILE = "review.token"
SUBMITTED_FILE = "reply.submitted"
DISPATCH_ENV, TOKEN_ENV = "OFFICE_REVIEW_DISPATCH", "OFFICE_REVIEW_TOKEN"
REVIEW_ROLES = ("plan_reviewer", "code_reviewer", "integration_reviewer", "visual_reviewer")
# The one file a granted reviewer submits from, beside its reply: the grant names this exact command, no wildcard.
SUBMIT_FILE = "review-submit.txt"
# Claude Code runs its built-in read-only Bash commands in every permission mode, and the set is not
# configurable (docs: permissions > read-only commands). Dropping the blanket Bash deny for the submit allow
# would let them through, so each documented member is denied explicitly, bare and with arguments.
CLAUDE_READONLY_BASH = ("ls", "cat", "echo", "pwd", "head", "tail", "grep", "find", "wc", "which", "diff",
                        "stat", "du", "cd", "git")
# #1186 (Rico, 2026-11-15: "some bash commands should be allowed too by reviewers, as long as safe ones"). Where
# Claude Code's OS sandbox runs, a Claude reviewer keeps the built-in read-only commands above (git status/log/diff/
# show/blame, ls, cat, grep, find, ...) and may also run these targeted test runners, bare or with arguments. The
# sandbox, not the command name, is the fence: the test code they execute cannot reach the network or write the
# checkout, Office's state or the guidance roots (read_scope.claude_sandbox_settings).
REVIEWER_TEST_BASH = ("pytest", "python -m pytest", "python3 -m pytest", ".venv/bin/python -m pytest",
                      ".venv/bin/pytest", "pnpm test", "npm test", "node --test")
# Defence in depth under that sandbox: dontAsk already denies anything no rule allows, and these name the
# mutating commands a reviewer must never run even if a later Claude Code widened its read-only set.
REVIEWER_BASH_DENY = ("rm", "rmdir", "mv", "cp", "chmod", "chown", "ln", "dd", "truncate", "sudo", "curl", "wget",
                      "ssh", "scp", "rsync", "git push", "git commit", "git checkout", "git switch", "git restore",
                      "git reset", "git clean", "git stash", "git rebase", "git merge", "git cherry-pick", "git am",
                      "git apply", "git tag", "git branch", "git worktree", "git config", "git remote", "git fetch",
                      "git pull", "git clone", "git gc", "git update-ref", "gh", "pip install", "pip3 install",
                      "uv pip", "uv add", "uv sync", "npm install", "npm i", "npm ci", "pnpm install", "pnpm add",
                      "pnpm i", "yarn", "brew", "find * -delete", "find * -exec", "find * -execdir", "find * -ok")


def submit_argv(output: str | Path) -> list[str]:
    """The exact argv a granted reviewer may run (#1030 narrow grant)."""
    return ["office", "review", "submit", "--file", str(Path(output).parent / SUBMIT_FILE)]


def submit_command(output: str | Path) -> str:
    """The same command as one shell line, its path quoted (a run dir may contain spaces)."""
    return shlex.join(submit_argv(output))


def codex_rules(output: str | Path) -> str:
    """A codex execpolicy rules file allowing exactly the submit command to run outside the sandbox (it writes
    runs.db). Codex splits a command line at control operators and allows a segment only when it matches a
    prefix rule; redirections, substitutions, env assignments and globs are never matched."""
    tokens = submit_argv(output)
    return ("# Written by Auto Office for this reviewer dispatch only (#1030): one exact command, nothing else.\n"
            f"prefix_rule(pattern={tokens!r}, decision=\"allow\",\n"
            "            justification=\"hand this review back to Auto Office; it writes only Office state\")\n")


# #1231: pi cannot limit bash to one command, so a pi reviewer never gets bash. Its pane form instead loads a
# per-dispatch extension (explicit `-e` paths load even under --no-extensions) that registers one tool. The
# tool writes the review to the submit file and runs the exact submit argv with no shell: the same narrow grant
# codex's prefix rule and claude's Bash allow give.
PI_SUBMIT_TOOL = "office_review_submit"


def pi_extension_path(output: str | Path) -> Path:
    return Path(output).parent / ".pi" / "office-review-submit.ts"


def pi_extension(output: str | Path) -> str:
    """The pi extension source for one reviewer dispatch: one tool, one exact command."""
    argv = submit_argv(output)
    return f"""// Written by Auto Office for this reviewer dispatch only (#1231): one tool, one exact command.
// @ts-nocheck
import {{ execFile }} from "node:child_process";
import {{ writeFileSync }} from "node:fs";
import {{ Type }} from "@earendil-works/pi-ai";

const ARGV = {json.dumps(argv)};
const SUBMIT_FILE = {json.dumps(argv[-1])};

export default function (pi) {{
  pi.registerTool({{
    name: {json.dumps(PI_SUBMIT_TOOL)},
    label: "Office review submit",
    description: "Hand your complete review back to Auto Office (this runs `office review submit`). Pass the "
      + "complete review, only the lines your brief specifies. Office checks them and records your review at once. "
      + "On an error, fix the lines and call this tool again.",
    parameters: Type.Object({{
      review: Type.String({{ description: "The complete review, only the lines your brief specifies" }}),
    }}),
    async execute(_toolCallId, params, signal) {{
      writeFileSync(SUBMIT_FILE, params.review, "utf8");
      const result = await new Promise((resolve) => {{
        execFile(ARGV[0], ARGV.slice(1), {{ signal, timeout: 120000, maxBuffer: 1 << 20 }}, (err, stdout, stderr) => {{
          resolve({{ ok: !err, text: `${{stdout || ""}}${{stderr || ""}}`.trim() || (err ? String(err.message) : "") }});
        }});
      }});
      if (!result.ok) throw new Error(result.text || "office review submit failed");
      return {{ content: [{{ type: "text", text: result.text || "review recorded" }}], details: undefined }};
    }},
  }});
}}
"""


MAX_CHARS = 200_000
FALLBACK_EVENT = "review.reply_fallback"
# Reviewer harnesses that cannot run the submit command, so they keep the reply file as their completion step
# (their adapter reviewer profile says `submit: file`). Every other reviewer profile says `submit: shell`
# (pi's pane form reaches it through its extension tool, #1231).
NO_SHELL_HARNESSES = ("agy", "opencode")


def pointer(output: str | Path, *, prompt_file: str | Path, images: str = "") -> str:
    """The one-line prompt that sends a reviewer to its brief: `office review submit` is the completion step, the
    reply file and the final reply are what a harness without a shell falls back to."""
    return (f"Read and carry out the review brief at {prompt_file} exactly.{images} "
            f"Finish with `office review submit`: write the complete review to "
            f"{Path(output).parent / SUBMIT_FILE} and run exactly: {submit_command(output)} . "
            f"If you have an `{PI_SUBMIT_TOOL}` tool, call it with the complete review instead. "
            f"If your tools cannot run that command, write the complete review to {output} instead "
            "(Office reads only that file, never your terminal), or if they cannot write files, end your reply with the "
            "complete review. Do not edit anything else.")


def record_fallback(d: dict, source: str) -> None:
    """Note once per dispatch that its review came back by a fallback (reply file, last message, stdout log) and not
    by `office review submit`, so the fallback rate can be counted from events."""
    if not d.get("id") or not d.get("run_id"):
        return
    try:
        con = db.connect()
        try:
            if con.execute("SELECT 1 FROM events WHERE dispatch_id=? AND kind=?", (d["id"], FALLBACK_EVENT)).fetchone():
                return
            run = state.get_run(con, d["run_id"])
            with db.transaction(con):
                state.emit(con, run, FALLBACK_EVENT, f"{d.get('role')} {d['id']} returned its review by {source}, "
                           "not office review submit", task_id=d.get("task_id"), dispatch_id=d["id"],
                           audience="runtime", payload={"source": source, "harness": d.get("harness")})
        finally:
            con.close()
    except Exception:  # a measurement must never cost a review its reply
        return


def _ddir(run_id: str, dispatch_id: str) -> Path:
    return paths.run_dir(run_id) / "dispatches" / dispatch_id


def token_env(run: dict, dispatch: dict) -> dict[str, str]:
    """The reviewer session's submit identity, created once per dispatch (0600 beside its launch spec)."""
    ddir = _ddir(run["id"], dispatch["id"])
    ddir.mkdir(parents=True, exist_ok=True)
    path = ddir / TOKEN_FILE
    if not path.is_file():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(secrets.token_hex(24))
    return {DISPATCH_ENV: dispatch["id"], TOKEN_ENV: path.read_text().strip()}


def submitted(output: str | Path | None) -> bool:
    """The reviewer handed its review back with `office review submit` (the marker sits beside its reply)."""
    if not output:
        return False
    out = Path(output)
    return (out.parent / SUBMITTED_FILE).is_file() and out.is_file() and out.stat().st_size > 0


def _reviewed_commit_problem(run: dict, gate: dict, text: str) -> str | None:
    from office import convergence
    key = gate.get("input_key") or ""
    if gate["kind"] == "convergence_review":
        commit = key.partition(":")[2]
    elif gate["kind"] == "self_review_subagent":
        parts = key.split(":")
        commit = parts[1] if len(parts) > 1 else ""
    else:
        return None
    if not commit:
        return None
    return convergence.commit_problem(commit, convergence.reviewed_commit(text))


def submit(con, text: str) -> Result:
    did, token = os.environ.get(DISPATCH_ENV), os.environ.get(TOKEN_ENV)
    if not did or not token:
        raise Usage("not-a-reviewer", "office review submit runs inside a reviewer session: its "
                    f"{DISPATCH_ENV}/{TOKEN_ENV} identity is missing",
                    next_step="write your review to the reply file your prompt names instead")
    d = state.get_dispatch(con, did)
    if d is None or d.get("role") not in REVIEW_ROLES or not d.get("gate_id"):
        raise Refused("not-a-review", f"{did} is not a reviewer dispatch with an open review gate")
    ddir = _ddir(d["run_id"], did)
    expected = (ddir / TOKEN_FILE).read_text().strip() if (ddir / TOKEN_FILE).is_file() else ""
    if not expected or not hmac.compare_digest(expected, token):
        raise Refused("review-identity", f"the submit identity does not match reviewer {did}")
    if d.get("ended_at") or d.get("status") in ("exited", "failed", "cancelled"):
        raise Refused("review-closed", f"reviewer {did} has already ended; its review can no longer be submitted")
    if d.get("lease_id") and con.execute("SELECT 1 FROM leases WHERE id=? AND revoked_at IS NOT NULL",
                                         (d["lease_id"],)).fetchone():
        raise Refused("lease-lost", f"{d['lease_id']} is no longer reviewer {did}'s live lease; its review is fenced out",
                      preserved="nothing was recorded")
    gate = db.row_dict(con.execute("SELECT * FROM gates WHERE id=?", (d["gate_id"],)).fetchone()) or {}
    if gate.get("status") not in ("queued", "running"):
        raise Refused("review-closed", f"review gate {d['gate_id']} is {gate.get('status') or 'gone'}")
    text = (text or "").strip()
    if not text:
        raise Usage("empty-review", "no review on stdin or in --file", next_step="office review submit --file <path>")
    if len(text) > MAX_CHARS:
        raise Usage("review-too-long", f"the review is over {MAX_CHARS} characters; keep one line per finding")
    run = state.get_run(con, d["run_id"])
    from office import gates
    parsed = review_parse.parse(gates._last_block(text), plan_review=gate["kind"] == "plan_review",
                                visual=gate["kind"] == "visual", contract=contract.of(run))
    if not parsed.valid:
        raise Refused("review-invalid", "your review is not in the required format: " + "; ".join(parsed.errors[:6]),
                      preserved="nothing was recorded", next_step="fix those lines and run office review submit again")
    if (problem := _reviewed_commit_problem(run, gate, text)):
        raise Refused("review-wrong-commit", problem, preserved="nothing was recorded",
                      next_step="review the commit your brief names, then office review submit again")
    spec = gates._launch_spec(ddir)
    output = Path(spec.get("output") or ddir / "reply.txt")
    verdict = parsed.verdict or parsed.evidence_status
    with db.transaction(con):
        # Fenced with the gate check: a gate that took its result meanwhile keeps the reply it took.
        live = con.execute("SELECT status FROM gates WHERE id=?", (gate["id"],)).fetchone()
        if not live or live[0] not in ("queued", "running"):
            raise Refused("review-closed", f"review gate {gate['id']} closed while the review was being submitted")
        tmp = output.with_name(output.name + ".submitting")
        tmp.write_text(text + "\n", encoding="utf-8")
        os.replace(tmp, output)
        atomic_write_json(ddir / SUBMITTED_FILE, {"at": now_iso(), "sha256": sha256_bytes((text + "\n").encode())},
                          mode=0o600)
        state.emit(con, run, "review.submitted", f"{d.get('task_id') or gate.get('scope') or 'plan'} {d['role']} "
                   f"{did} submitted its review ({verdict})", task_id=d.get("task_id"), dispatch_id=did,
                   audience="runtime", payload={"gate": gate["id"], "verdict": verdict})
    return Result(lines=[f"review submitted: {verdict} for {gate['kind']} gate {gate['id']}; Office records it now"],
                  data={"dispatch": did, "gate": gate["id"], "verdict": verdict})
