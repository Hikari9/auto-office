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
from pathlib import Path

from office import contract, db, paths, review_parse, state
from office.result import Result
from office.state import Refused, Usage
from office.util import atomic_write_json, now_iso, sha256_bytes

TOKEN_FILE = "review.token"
SUBMITTED_FILE = "reply.submitted"
DISPATCH_ENV, TOKEN_ENV = "OFFICE_REVIEW_DISPATCH", "OFFICE_REVIEW_TOKEN"
REVIEW_ROLES = ("plan_reviewer", "code_reviewer", "integration_reviewer", "visual_reviewer")
MAX_CHARS = 200_000


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
