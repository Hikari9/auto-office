"""The run on a display-only herdr-projects board card (docs/board-card.md).

The board (herdr-projects, "wherdr") only shows an Office run. Its card is never delegated:
it has no board worktree, plan, review or merge. Office keeps its whole pipeline. This
module links a run to its card and keeps the card up to date:

- the key comes from rico-mode (`OFFICE_BOARD_CARD`, or `office start --card`). Without one,
  and with the board present (inside herdr, its CLI found), Office creates its own card.
  There is never more than one card for a run.
- `card_state` maps the run to the card's state line (working / blocked / done / error, a
  one-line message and a progress percent). Waiting on the human is `blocked` with
  "answer in rico-mode": answers keep flowing through rico-mode, never through the board.
- `render_description` is the card's readable view: the plan, every executor and reviewer
  route with its fallbacks and their reasons, and every review round with its findings.
- `sweep` turns new run events into one deduplicated `card_publish` outbox job. Publishing is
  best-effort: a board failure is recorded (`card.publish_failed`) and never fails the run.

Without a card the run never calls the board: no events, no jobs, no subprocess.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path

from office import db, paths, state
from office.result import Result
from office.state import Usage
from office.util import now_iso, short

ENV_KEY = "OFFICE_BOARD_CARD"
CONSUMER = "card"
JOB_KIND = "card_publish"
MAX_DESCRIPTION = 16000
ANSWER_WHERE = "answer in rico-mode"
_TIMEOUT = 20
_AUDIENCES = ("orchestrator", "runtime")


class CardError(Exception):
    """The board CLI refused or could not run a command."""


# ------------------------------------------------------------------ the board CLI

def find_cli() -> str | None:
    """The herdr-projects command: HERDR_PROJECTS_BIN, the board's data folder, its default
    home, then PATH. None when there is no board CLI here."""
    env = os.environ
    explicit = env.get("HERDR_PROJECTS_BIN")
    if explicit:
        return explicit if _executable(Path(explicit)) else None
    candidates = []
    if env.get("HERDR_PROJECTS_DATA_DIR"):
        candidates.append(Path(env["HERDR_PROJECTS_DATA_DIR"]) / "bin" / "herdr-projects")
    candidates.append(Path.home() / ".herdr-projects" / "data" / "bin" / "herdr-projects")
    for c in candidates:
        if _executable(c):
            return str(c)
    return shutil.which("herdr-projects")


def _executable(p: Path) -> bool:
    return p.is_file() and os.access(p, os.X_OK)


def board_present() -> str | None:
    """The board CLI, when Office runs inside herdr. The `HERDR_ENV` gate keeps every shell
    outside herdr, and every test (the root conftest strips HERDR_*), exactly as before."""
    if os.environ.get("HERDR_ENV") != "1":
        return None
    return find_cli()


def _call(cli: str, args: list[str]) -> str:
    try:
        out = subprocess.run([cli, *args], capture_output=True, text=True, timeout=_TIMEOUT,
                             stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CardError(f"{args[0]}: {exc}") from exc
    if out.returncode != 0:
        raise CardError(f"{args[0]} exited {out.returncode}: {(out.stderr or out.stdout).strip()[:300]}")
    return out.stdout


def _json(cli: str, args: list[str]):
    text = _call(cli, [*args, "--json"])
    try:
        return json.loads(text)
    except ValueError as exc:
        raise CardError(f"{args[0]}: not JSON: {text[:200]}") from exc


def _card_id(created) -> str | None:
    if isinstance(created, dict):
        for d in (created, created.get("task") or {}):
            if isinstance(d, dict) and d.get("id"):
                return str(d["id"])
    return None


def _description(cli: str, card_id: str) -> str:
    d = _json(cli, ["details", card_id])
    return str(((d or {}).get("task") or {}).get("description") or "")


# ------------------------------------------------------------------ linking

def card_of(run: dict) -> dict | None:
    card = (run.get("landing") or {}).get("card")
    return card if isinstance(card, dict) and card.get("id") else None


def title_marker(run: dict) -> str:
    return f"Office {short(run['id'])}:"


def card_title(run: dict) -> str:
    goal = " ".join((run.get("goal") or "").split())
    text = f"{title_marker(run)} {goal}"
    return text if len(text) <= 60 else text[:59].rstrip() + "…"


def _save_card(con, run_id: str, **fields) -> dict:
    """Merge `fields` into landing.card inside one transaction; the rest of landing is kept."""
    with db.transaction(con):
        run = state.get_run(con, run_id)
        landing = dict(run.get("landing") or {})
        card = dict(landing.get("card") or {})
        card.update(fields)
        landing["card"] = card
        state.update_run(con, run_id, landing=landing)
    return card


@contextmanager
def _card_lock(run_id: str):
    d = paths.run_dir(run_id)
    d.mkdir(parents=True, exist_ok=True)
    with (d / "card.lock").open("a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def link(con, run_id: str, card_id: str, *, created_by: str, cli: str | None = None) -> dict:
    """Record the run's card. A run keeps the card it has: a second, different key is refused."""
    card_id = card_id.strip()
    if not card_id:
        raise Usage("missing-card", "name the board card", next_step="office card link <card id>")
    with db.transaction(con):
        run = state.get_run(con, run_id)
        have = card_of(run)
        if have and have["id"] != card_id:
            raise Usage("card-linked", f"run {short(run_id)} already shows on board card {have['id']}",
                        next_step="office card status")
        if have:
            return have
        landing = dict(run.get("landing") or {})
        card = {"id": card_id, "cli": cli or find_cli(), "created_by": created_by, "linked_at": now_iso()}
        landing["card"] = card
        state.update_run(con, run_id, landing=landing)
        state.emit(con, run, "card.linked", f"run shows on board card {card_id} ({created_by})",
                   audience="runtime", payload={"card": card_id, "created_by": created_by})
    return card


def create(con, run_id: str, cli: str) -> tuple[dict, str]:
    """The run's own card: adopted when one titled for this run exists, else added. Serialized
    per run, so two callers can never add two cards. Returns (card, "existing"|"adopted"|"created")."""
    with _card_lock(run_id):
        run = state.get_run(con, run_id)
        have = card_of(run)
        if have:
            return have, "existing"
        marker = title_marker(run)
        found = None
        listed = _json(cli, ["list", "--all", "--query", marker])
        for item in listed if isinstance(listed, list) else []:
            if isinstance(item, dict) and str(item.get("title") or "").startswith(marker):
                found = str(item.get("id") or "")
                break
        if found:
            return link(con, run_id, found, created_by="office", cli=cli), "adopted"
        desc = render_description(con, run)
        created = _json(cli, ["add", card_title(run), "--repo", str(run["repo_root"]), "--desc", desc])
        card_id = _card_id(created)
        if not card_id:
            raise CardError(f"add returned no card id: {str(created)[:200]}")
        linked = link(con, run_id, card_id, created_by="office", cli=cli)
        _last_path(run).parent.mkdir(parents=True, exist_ok=True)
        _last_path(run).write_text(desc, encoding="utf-8")
        return _save_card(con, run_id, desc_hash=_digest(desc)) or linked, "created"


def attach_at_start(con, run_id: str, *, card: str | None = None, no_card: bool = False,
                    setting: str = "auto") -> str | None:
    """Link a new run to its card. The key from rico-mode (or --card) wins; without one, Office
    creates its own card when the board is present. Returns a line for `office start`, or None
    when the run has no card. Never fails the start."""
    env = os.environ.get(ENV_KEY, "").strip()
    if no_card or env.lower() == "off":
        return None
    key = (card or env).strip()
    try:
        if key:
            cli = find_cli()
            link(con, run_id, key, created_by="given", cli=cli)
            if not cli:
                return f"board card {key}: no herdr-projects command found; the card will not update"
            publish(con, run_id)
            return f"board card {key}"
        if str(setting).lower() == "off":
            return None
        cli = board_present()
        if not cli:
            return None
        got, how = create(con, run_id, cli)
        publish(con, run_id)
        return f"board card {got['id']} ({how})"
    except (CardError, OSError) as exc:
        with db.transaction(con):
            run = state.get_run(con, run_id)
            state.emit(con, run, "card.publish_failed", f"board card not set up: {exc}"[:300], audience="runtime")
        return f"board card not set up: {str(exc)[:160]}"


# ------------------------------------------------------------------ what the card says

def _fmt_triple(triple: str | None) -> str:
    """claude@2/claude-opus-5-5@low -> claude/claude-opus-5-5@low (the adapter version dropped)."""
    if not triple:
        return "unrecorded"
    return re.sub(r"^([A-Za-z0-9_.-]+)@\d+/", r"\1/", str(triple))


def _one_line(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _review_gates(con, run_id: str) -> list[dict]:
    rows = con.execute("SELECT * FROM gates WHERE run_id=? AND kind!='checks' ORDER BY created_at, round",
                       (run_id,)).fetchall()
    return [dict(r) for r in rows]


def _gate_label(g: dict) -> str:
    who = g.get("task_id") or ("plan" if g["kind"] == "plan_review" else g.get("subject") or "run")
    return f"{g['kind']} {who} r{g.get('round') or 1}"


def _findings(con, gate_id: str) -> list[dict]:
    rows = con.execute("SELECT * FROM findings WHERE gate_id=? ORDER BY created_at, id", (gate_id,)).fetchall()
    return [dict(r) for r in rows]


_SEVERITY_ORDER = ("critical", "high", "material", "major", "medium", "low", "minor", "trivial")


def _severity_counts(findings: list[dict]) -> str:
    counts: dict[str, int] = {}
    for f in findings:
        sev = (f.get("severity") or f.get("level") or "unrated").lower()
        counts[sev] = counts.get(sev, 0) + 1
    order = {s: i for i, s in enumerate(_SEVERITY_ORDER)}
    return ", ".join(f"{n} {s}" for s, n in sorted(counts.items(), key=lambda kv: order.get(kv[0], 99)))


def _verdict(g: dict) -> str:
    if g.get("verdict"):
        return g["verdict"]
    return "running" if g.get("status") in ("queued", "waiting", "running") else (g.get("status") or "pending")


def _human_reason(run: dict, nxt: str) -> tuple[str, str]:
    """(kind, one-line reason) for a next step only the human can take."""
    low = nxt.lower()
    if "plan questions" in low:
        return "question", "Plan questions need your answers"
    if "intake_gap" in low:
        return "question", "Intake gap: a decision only you can make"
    m = re.search(r"plan defect (\S+)", nxt)
    if m:
        return "question", f"Plan defect {m.group(1)} needs your call"
    if "authorization of r" in low:
        return "permission", f"Plan p{run.get('plan_version') or 1} ready for your approval"
    m = re.search(r"new authority entry (\S+) \(([^)]*)\)", nxt)
    if m:
        return "permission", f"{m.group(1)} ({m.group(2)}) needs your authorization"
    kind = "permission" if re.search(r"approv|authoriz|waive|merge|\bland\b|deploy", low) else "question"
    first = re.split(r";|\. ", nxt, maxsplit=1)[0]
    return kind, _one_line(first, 140)


def card_state(con, run: dict) -> dict:
    """The card's state line, from the run as it is now: state, kind (blocked only), msg, progress."""
    from office import guide, root_wake
    run = state.get_run(con, run["id"])
    phase = run.get("phase")
    landing = run.get("landing") or {}
    if phase == "closed":
        detail = landing.get("handoff") or landing.get("merge_commit") or landing.get("detail") or "closed"
        return {"state": "done", "kind": None, "msg": _one_line(f"Done: {detail}", 200), "progress": None}
    if phase == "abandoned":
        return {"state": "error", "kind": None,
                "msg": _one_line(f"Abandoned: {run.get('terminal_reason') or 'no reason given'}", 200), "progress": None}
    nxt = guide.next_action(con, run)
    if root_wake._human_only(nxt):
        kind, reason = _human_reason(run, nxt)
        return {"state": "blocked", "kind": kind, "msg": f"{reason} · {ANSWER_WHERE}", "progress": None}
    if not run.get("plan_version"):
        return {"state": "working", "kind": None, "msg": _one_line(f"Intake: {run.get('goal')}", 160), "progress": 5}
    reviews = _review_gates(con, run["id"])
    plan_reviews = [g for g in reviews if g["kind"] == "plan_review"]
    if not state.active_authorization(con, run, "plan"):
        if plan_reviews:
            g = plan_reviews[-1]
            msg = f"Plan p{run['plan_version']} review r{g.get('round') or 1}: {_verdict(g)}"
        else:
            msg = f"Plan p{run['plan_version']} submitted"
        return {"state": "working", "kind": None, "msg": msg, "progress": 10}
    tasks = [t for t in state.tasks(con, run["id"]) if t["status"] != "cancelled"]
    n = len(tasks) or 1
    accepted = sum(t["status"] == "accepted" for t in tasks)
    running = sum(t["status"] in ("launching", "running", "submitted") for t in tasks)
    blocked = sum(t["status"] in ("blocked", "paused") for t in tasks)
    if tasks and accepted == len(tasks) and "office land" in nxt:
        return {"state": "working", "kind": None, "msg": "Merge-ready: landing", "progress": 90}
    msg = f"Executing: {accepted}/{len(tasks)} accepted, {running} running, {blocked} blocked"
    task_reviews = [g for g in reviews if g["kind"] != "plan_review"]
    if task_reviews:
        g = task_reviews[-1]
        counts = _severity_counts(_findings(con, g["id"]))
        msg += f" · review {g.get('task_id') or g.get('subject')} r{g.get('round') or 1}: {_verdict(g)}"
        if counts:
            msg += f" ({counts})"
    return {"state": "working", "kind": None, "msg": _one_line(msg, 240),
            "progress": 15 + int(70 * accepted / n)}


def _route_lines(con, run_id: str) -> list[str]:
    rows = con.execute("SELECT role, task_id, holder_id, gate_id, triple, status, outcome FROM dispatches "
                       "WHERE run_id=? ORDER BY COALESCE(launched_at, started_at), id", (run_id,)).fetchall()
    seen: dict[tuple, list] = {}
    for r in rows:
        who = r["task_id"] or r["holder_id"] or r["gate_id"] or "run"
        key = (who, r["role"] or "agent", _fmt_triple(r["triple"]))
        entry = seen.setdefault(key, [0, None])
        entry[0] += 1
        entry[1] = r["status"] or r["outcome"]
    out = []
    for (who, role, route), (count, status) in seen.items():
        extra = f" ×{count}" if count > 1 else ""
        out.append(f"  {who} {role}: {route}{extra}" + (f" ({status})" if status else ""))
    return out


def _fallback_lines(con, run_id: str) -> list[str]:
    rows = con.execute("SELECT kind, task_id, summary, payload_json FROM events WHERE run_id=? "
                       "AND kind='route.fallback' ORDER BY seq", (run_id,)).fetchall()
    chains: dict[str, list] = {}
    for r in rows:
        p = state.json_or(r["payload_json"], {}) or {}
        before, after = p.get("before"), p.get("after")
        if not (before and after):
            m = re.search(r"from (\S+) to (\S+)", r["summary"] or "")
            if not m:
                continue
            before, after = m.group(1), m.group(2)
        reason = p.get("reason") or p.get("source")
        if not reason:
            m = re.search(r"\(([^()]*)\)\s*$", r["summary"] or "")
            reason = m.group(1) if m else ""
        scope = (r["summary"] or "").split(" fell back", 1)[0].split(" rerouted", 1)[0]
        key = p.get("gate") or f"{r['task_id'] or ''} {p.get('stage') or ''}".strip() or scope
        chain = chains.setdefault(key, [scope, _fmt_triple(before), []])
        if chain[2] and chain[2][-1][0] != _fmt_triple(before):
            chain[2].append((_fmt_triple(before), ""))
        chain[2].append((_fmt_triple(after), _one_line(reason, 120)))
    out = []
    for scope, first, steps in chains.values():
        text = first + "".join(f" → {route}" + (f" ({why})" if why else "") for route, why in steps)
        out.append(f"  {scope}: {text}")
    return out


def _review_blocks(con, run_id: str) -> list[tuple[str, list[str]]]:
    blocks = []
    for g in _review_gates(con, run_id):
        fs = _findings(con, g["id"])
        head = f"  {_gate_label(g)}: {_verdict(g)}"
        if g.get("route"):
            head += f" by {_fmt_triple(g['route'])}"
        counts = _severity_counts(fs)
        if counts:
            head += f" · {counts}"
        lines = []
        for f in fs:
            sev = f.get("severity") or f.get("level") or "unrated"
            status = f.get("disposition") or f.get("state") or f.get("status") or ""
            lines.append(f"    - [{sev}] {f.get('code') or ''} {_one_line(f.get('summary'), 160)}"
                         + (f" ({status})" if status else ""))
        blocks.append((head, lines))
    return blocks


def render_description(con, run: dict, kept: list | None = None) -> str:
    """The card's description: header, needs-you, plan, routes, fallbacks, reviews, kept text."""
    run = state.get_run(con, run["id"])
    rid = run["id"]
    plan = state.current_plan(con, rid)
    authorized = state.active_authorization(con, run, "plan")
    view = card_state(con, run)
    try:
        from office import land
        end = land.end_state(con, run)["mode"]
    except Exception:  # an older run may lack frozen requirements
        end = "ask"
    head = [f"Office run {short(rid)} · {run.get('phase')} · req r{run.get('requirements_version')} · "
            f"plan p{run.get('plan_version') or 0} · end state {end}",
            f"Goal: {_one_line(run.get('goal'), 400)}",
            f"{'Needs you' if view['state'] == 'blocked' else 'Now'}: {view['msg']}"]
    plan_part: list[str] = []
    if plan:
        tasks = state.tasks(con, rid)
        if not authorized and not state.is_terminal(run):
            plan_part = [f"Plan p{plan['version']} (awaiting your approval):", plan["body"].rstrip()]
        else:
            who = (f"authorized by {authorized['authorized_by']} {authorized['created_at'][:16]}: "
                   f"\"{_one_line(authorized['quote'], 200)}\"") if authorized else "not authorized"
            plan_part = [f"Plan p{plan['version']} ({who})", "Tasks:"]
            plan_part += [f"  {t['id']} {_one_line(t['title'], 100)}: {t['status']}" for t in tasks]
    routes = _route_lines(con, rid)
    fallbacks = _fallback_lines(con, rid)
    blocks = _review_blocks(con, rid)
    kept_lines = []
    for k in kept or []:
        kept_lines.append(f"  [{str(k.get('at', ''))[:16]}] {k.get('text', '')}")
    foot = [f"Full text: office status --run {short(rid)}; run folder {run.get('state_dir')}"]

    def build(blocks_now, plan_now) -> str:
        parts = ["\n".join(head)]
        if plan_now:
            parts.append("\n".join(plan_now))
        parts.append("Routes:\n" + ("\n".join(routes) if routes else "  none yet"))
        if fallbacks:
            parts.append("Fallbacks:\n" + "\n".join(fallbacks))
        if blocks_now:
            parts.append("Reviews:\n" + "\n".join("\n".join([h, *ls]) for h, ls in blocks_now))
        if kept_lines:
            parts.append("Kept from the card:\n" + "\n".join(kept_lines))
        parts.append("\n".join(foot))
        return "\n\n".join(parts)

    text = build(blocks, plan_part)
    # Over the cap: drop finding lines from the oldest rounds first, then shorten the plan text.
    trimmed = 0
    blocks = [(h, list(ls)) for h, ls in blocks]
    i = 0
    while len(text) > MAX_DESCRIPTION and i < len(blocks):
        trimmed += len(blocks[i][1])
        blocks[i] = (blocks[i][0], [])
        i += 1
        text = build(blocks, plan_part)
    if trimmed:
        foot.append(f"… {trimmed} more finding line(s) in {run.get('state_dir')}")
        text = build(blocks, plan_part)
    if len(text) > MAX_DESCRIPTION and len(plan_part) > 1:
        room = max(500, len(plan_part[1]) - (len(text) - MAX_DESCRIPTION) - 200)
        plan_part = [plan_part[0], plan_part[1][:room] + "\n… the rest of the plan: office status"]
        text = build(blocks, plan_part)
    return text[:MAX_DESCRIPTION]


# ------------------------------------------------------------------ publishing

def _digest(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def _last_path(run: dict) -> Path:
    return paths.run_dir(run["id"]) / "card-last.txt"


def _foreign(current: str, last_written: str | None) -> str:
    """The part of the card's text someone else wrote: its lines Office did not write last."""
    if not last_written:
        return current.strip()
    ours = set(last_written.strip().splitlines())
    theirs = [ln for ln in current.strip().splitlines() if ln not in ours]
    return "\n".join(theirs).strip() or "(lines of the card were removed)"


def _report_args(card_id: str, view: dict) -> list[str]:
    args = ["report", card_id, "--state", view["state"], "--msg", view["msg"]]
    if view["state"] == "blocked":
        args += ["--kind", view["kind"] or "question"]
    if view["state"] == "working" and view.get("progress") is not None:
        args += ["--progress", str(view["progress"])]
    return args


def publish(con, run_id: str) -> dict:
    """Bring the card up to the run's current state. Only what changed is sent. Never raises:
    a failure is recorded as `card.publish_failed` (once per distinct error) and returned."""
    run = state.get_run(con, run_id)
    card = card_of(run) if run else None
    if not card:
        return {"published": False, "reason": "no card"}
    cli = card.get("cli") or find_cli()
    try:
        if not cli:
            raise CardError("no herdr-projects command found")
        kept = list(card.get("kept") or [])
        view = card_state(con, run)
        desc = render_description(con, run, kept)
        last_path = _last_path(run)
        last_written = last_path.read_text(encoding="utf-8") if last_path.is_file() else None
        sent = []
        for _ in range(2):
            current = _description(cli, card["id"])
            # Text Office did not write (a rico-mode brief before the first publish, or an edit made
            # on the card since) is kept on the card, never dropped.
            if current.strip() and _digest(current) != _digest(desc) and (
                    not card.get("desc_hash") or _digest(current) != card["desc_hash"]):
                kept.append({"at": now_iso(), "text": _one_line(_foreign(current, last_written), 2000)})
                desc = render_description(con, run, kept)
            elif _digest(current) == _digest(desc):
                break
            _call(cli, ["describe", card["id"], desc])
            sent.append("describe")
            last_path.parent.mkdir(parents=True, exist_ok=True)
            last_path.write_text(desc, encoding="utf-8")
            last_written = desc
            card["desc_hash"] = _digest(desc)
            if _digest(_description(cli, card["id"])) == card["desc_hash"]:
                break
        if view != card.get("last"):
            _call(cli, _report_args(card["id"], view))
            sent.append("report")
        _save_card(con, run_id, desc_hash=card.get("desc_hash") or _digest(desc), last=view, kept=kept,
                   published_at=now_iso(), error=None, cli=cli)
        return {"published": True, "sent": sent, "state": view["state"]}
    except (CardError, OSError) as exc:
        err = str(exc)[:300]
        with db.transaction(con):
            current = state.get_run(con, run_id)
            if (card_of(current) or {}).get("error") != err:
                state.emit(con, current, "card.publish_failed", f"board card {card['id']} not updated: {err}",
                           audience="runtime", payload={"card": card["id"], "error": err})
        _save_card(con, run_id, error=err)
        return {"published": False, "error": err}


def job_publish(con, run: dict, job: dict) -> dict:
    return publish(con, run["id"])


def sweep(con, run: dict | None) -> None:
    """New run events since the card last looked: queue one publish (or publish now for a run that
    ended, whose jobs no longer run). A run without a card returns at once. Never raises."""
    try:
        if not run:
            return
        run = state.get_run(con, run["id"])
        if not run or not card_of(run):
            return
        rows = state.unread_events(con, run["id"], CONSUMER, _AUDIENCES, limit=None)
        if not rows:
            return
        relevant = any(not r["kind"].startswith("card.") for r in rows)
        terminal = state.is_terminal(run)
        queued = False
        with db.transaction(con):
            state.advance_cursor(con, run["id"], CONSUMER, rows[-1]["seq"])
            if relevant and not terminal:
                waiting = con.execute("SELECT 1 FROM outbox WHERE run_id=? AND kind=? AND status='queued'",
                                      (run["id"], JOB_KIND)).fetchone()
                if not waiting:
                    queued = state.enqueue(con, run, JOB_KIND, {}, dedup_key=f"card:{run['id']}:{rows[-1]['seq']}",
                                           max_attempts=1) is not None
        if terminal and relevant:
            publish(con, run["id"])
        elif queued:
            from office import jobs
            jobs.kick(con, run["id"])
    except Exception:  # the card never fails an Office command
        return


def publish_terminal(con, run_id: str) -> None:
    """Close and abandon: the run's jobs are cancelled, so the last state is sent right here."""
    try:
        run = state.get_run(con, run_id)
        if run and card_of(run):
            sweep(con, run)
    except Exception:
        return


# ------------------------------------------------------------------ office card

def command(con, run: dict, action: str, target: str | None, *, create_card: bool = False) -> Result:
    run = state.get_run(con, run["id"])
    rid = short(run["id"])
    if action == "status":
        card = card_of(run)
        if not card:
            present = board_present()
            return Result(lines=[f"{rid}: no board card" + (
                f" (board present: office card link --create --run {rid} adds one)" if present else
                " (no board here: inside herdr, with herdr-projects installed, Office adds its own)")],
                data={"card": None})
        lines = [f"{rid}: board card {card['id']} ({card.get('created_by')}), cli {card.get('cli') or 'not found'}"]
        if card.get("last"):
            last = card["last"]
            lines.append(f"last sent: {last['state']}" + (f" ({last['kind']})" if last.get("kind") else "")
                         + f": {last['msg']}" + (f" · {last['progress']}%" if last.get("progress") is not None else ""))
        if card.get("published_at"):
            lines.append(f"published {card['published_at'][:19]}Z")
        if card.get("error"):
            lines.append(f"last error: {card['error']}")
        if card.get("kept"):
            lines.append(f"kept {len(card['kept'])} edit(s) made on the card")
        return Result(lines=lines, data={"card": card})
    if action == "link":
        if create_card:
            cli = find_cli()
            if not cli:
                raise Usage("no-board", "no herdr-projects command found",
                            next_step="install herdr-projects, or set HERDR_PROJECTS_BIN")
            try:
                card, how = create(con, run["id"], cli)
            except CardError as exc:
                raise Usage("board-refused", f"the board refused: {exc}", next_step="office card status") from exc
        else:
            if not target:
                raise Usage("missing-card", "name the board card, or --create",
                            next_step="office card link <card id> | office card link --create")
            card, how = link(con, run["id"], target, created_by="given"), "linked"
        out = publish(con, run["id"])
        return Result(lines=[f"{rid}: board card {card['id']} ({how})"
                             + ("" if out.get("published") else f"; not updated yet: {out.get('error') or out.get('reason')}")],
                      data={"card": card, "publish": out})
    if action == "sync":
        if not card_of(run):
            raise Usage("no-card", f"run {rid} has no board card", next_step="office card link <card id> | --create")
        out = publish(con, run["id"])
        if not out.get("published"):
            return Result(lines=[f"{rid}: card not updated: {out.get('error') or out.get('reason')}"], data=out)
        return Result(lines=[f"{rid}: card {card_of(state.get_run(con, run['id']))['id']} "
                             f"{'updated (' + ', '.join(out['sent']) + ')' if out['sent'] else 'already current'}"],
                      data=out)
    raise Usage("bad-action", f"unknown card action {action!r}", next_step="office card status|link|sync")
