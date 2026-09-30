#!/usr/bin/env python3
"""Score evaluation jobs and compare v3.1 against matched v3 (spec §24).

    eval/v31/analyze.py [--results ~/.office-eval/results.jsonl] [--markdown out.md]

Per job it derives, from the timestamped orchestrator transcript(s), the worker
logs and the job's isolated state:

- orchestrator tool calls, total and administrative (Office machinery, state or
  packet files, polling/waiting, dispatch, todo bookkeeping);
- hand-authored routine JSON (a JSON file written or edited by the orchestrator);
- telemetry-only turns (every tool call in the turn only records telemetry);
- reads of Office source code (runtime .py/.sh/.mjs under the plugin);
- orchestrator tokens and Claude's list-price cost estimate; worker tokens where
  the harness prints them (codex `tokens used`); otherwise unmeasured;
- v3.1 invariants from runs.db.

Money is never inferred: codex runs on a subscription with no per-call price,
so total USD is reported as unmeasured and tokens are the like-for-like cost.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import statistics
from collections import defaultdict
from pathlib import Path

EVAL_HOME = Path("~/.office-eval").expanduser()

ADMIN_BASH = re.compile(
    r"(^|[\s;&|(/])(office|office_runtime\.py|office_spawn\.sh|office_liveness\.sh|office_readback\.sh|office_worktree\.sh|"
    r"review_loop\.sh|review_finding\.sh|verify\.sh|office_family\.py|office_packets\.py|office_monitor\.py|"
    r"close_finished_panes\.mjs|herdr|sqlite3|sleep|ps|pgrep|kill|jq)\b"
    r"|state\.json|\.office/|\.auto-office/|auto-office/runs|office-eval/v3-plugin|office-eval/v31-plugin|/scripts/")
TELEMETRY = re.compile(r"office_runtime\.py\S*\s+(record-[a-z-]+|mark-spoke|spoke-digest|ack-event|save-checkpoint|"
                       r"state-save|family-update|record-event)|office\s+raw\s")
SOURCE = re.compile(r"(office_runtime|office_routing|office_scoring|office_family|office_packets|office_monitor)\.py|"
                    r"/src/office/[a-z_]+\.py|scripts/[a-z_]+\.(sh|mjs)|scripts/hooks/")
JSON_WRITE = re.compile(r"(>|tee)\s*['\"]?\S+\.json\b|json\.dump\(|cat\s*<<.*\.json|--intent\s|--packet\s")
ADMIN_TOOLS = {"TodoWrite", "Task", "Agent", "Monitor", "TaskOutput", "BashOutput", "KillShell", "KillBash",
               "ScheduleWakeup", "Skill", "TaskStop", "SendMessage"}
STATE_PATH = re.compile(r"\.office/|\.auto-office/|xdg-state|/state/|/data/|office-eval/v3-plugin|office-eval/v31-plugin|"
                        r"\.local/state/auto-office|runs\.db|state\.json")


READERS = re.compile(r"^\s*(cat|sed|head|tail|less|more|grep|rg|awk|nl|bat)\b")


def _reads_source(command: str) -> bool:
    """A pipeline segment that starts with a reader and names Office source as
    its own argument (running `office_runtime.py --help | head` is not a read)."""
    return any(READERS.match(seg) and SOURCE.search(seg) for seg in re.split(r"[|;&\n]+", command))


def classify(block: dict) -> dict:
    name, inp = block["name"], block.get("input") or {}
    text = inp.get("command") or inp.get("file_path") or inp.get("path") or inp.get("pattern") or ""
    admin = name in ADMIN_TOOLS
    if name == "Bash":
        admin = bool(ADMIN_BASH.search(text))
    elif name in ("Read", "Grep", "Glob", "Write", "Edit", "MultiEdit", "NotebookEdit"):
        admin = bool(STATE_PATH.search(text + " " + str(inp.get("path") or "")))
    hand_json = ((name in ("Write", "Edit", "MultiEdit") and str(inp.get("file_path", "")).endswith(".json"))
                 or (name == "Bash" and bool(JSON_WRITE.search(text))))
    source = (name == "Read" and bool(SOURCE.search(text))) or (name == "Bash" and _reads_source(text))
    telemetry = name == "Bash" and bool(TELEMETRY.search(text)) and not re.search(r"\b(start|dispatch|submit|close)\b", text)
    return {"name": name, "admin": admin, "hand_json": hand_json, "source": source, "telemetry": telemetry,
            "text": text[:160]}


def _codex_blocks(item: dict) -> list[dict]:
    """Map one codex item to tool-call blocks in the shape classify() reads."""
    kind = item.get("type")
    if kind == "command_execution":
        return [{"name": "Bash", "input": {"command": item.get("command", "")}}]
    if kind == "file_change":
        return [{"name": "Edit", "input": {"file_path": c.get("path", "")}} for c in item.get("changes") or [{}]]
    if kind in ("mcp_tool_call", "web_search", "todo_list"):
        return [{"name": {"todo_list": "TodoWrite"}.get(kind, kind), "input": {}}]
    return []


def score_transcripts(root: Path) -> dict:
    """Scores Claude stream-json and codex --json transcripts alike."""
    calls, turns_telemetry, usage, cost, sessions = [], 0, defaultdict(int), 0.0, 0
    for path in sorted(root.glob("transcript-s*.jsonl")):
        seen: set[str] = set()
        turn: list[dict] = []

        def close_turn():
            nonlocal turns_telemetry, turn
            if turn and all(c["telemetry"] for c in turn):
                turns_telemetry += 1
            turn = []

        for line in path.read_text().splitlines():
            rec = json.loads(line)
            e = rec.get("e") or {}
            kind = e.get("type")
            if kind == "assistant":  # Claude: one message = one turn
                blocks = [b for b in e["message"].get("content", []) if b.get("type") == "tool_use"]
                cls = [dict(classify(b), t=rec["t"]) for b in blocks]
                calls.extend(cls)
                if cls and all(c["telemetry"] for c in cls):
                    turns_telemetry += 1
            elif kind == "result":
                sessions += 1
                cost += float(e.get("total_cost_usd") or 0)
                for k, v in (e.get("usage") or {}).items():
                    if isinstance(v, (int, float)):
                        usage[k] += v
            elif kind in ("item.started", "item.completed"):  # codex
                item = e.get("item") or {}
                if item.get("type") == "agent_message":
                    close_turn()
                    continue
                if item.get("id") in seen:
                    continue
                blocks = _codex_blocks(item)
                if not blocks:
                    continue
                seen.add(item.get("id"))
                cls = [dict(classify(b), t=rec["t"]) for b in blocks]
                calls.extend(cls)
                turn.extend(cls)
            elif kind == "thread.started":
                sessions += 1
            elif kind == "turn.completed":
                close_turn()
                for k, v in (e.get("usage") or {}).items():
                    if isinstance(v, (int, float)):
                        usage[k] += v
        close_turn()
    first_dispatch = next((c["t"] for c in calls if c["name"] == "Bash" and re.search(
        r"office\s+dispatch\b|office_spawn\.sh", c["text"])), None)
    return {
        "tool_calls": len(calls),
        "admin_calls": sum(c["admin"] for c in calls),
        "hand_json": sum(c["hand_json"] for c in calls),
        "hand_json_examples": [c["text"] for c in calls if c["hand_json"]][:3],
        "telemetry_only_turns": turns_telemetry,
        "source_reads": sum(c["source"] for c in calls),
        "source_read_examples": [c["text"] for c in calls if c["source"]][:3],
        "orchestrator_sessions": sessions,
        "first_dispatch_command_at": round(first_dispatch, 1) if first_dispatch is not None else None,
        "orchestrator_cost_usd_estimate": round(cost, 4) if cost else None,
        # codex reports cached input inside input_tokens; Claude reports it separately.
        "orchestrator_tokens": int(usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
                                   + usage.get("cache_read_input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)),
        "orchestrator_output_tokens": int(usage.get("output_tokens", 0)),
    }


def worker_tokens(root: Path) -> dict:
    total, logs = 0, 0
    pattern = re.compile(r"tokens used\s*[:\n]?\s*([\d,]+)", re.I)
    for path in root.rglob("*"):
        if not path.is_file() or "repo" in path.relative_to(root).parts[:1] or path.suffix in (".png", ".db", ".jsonl"):
            continue
        if path.stat().st_size > 20_000_000:
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        found = pattern.findall(text)
        if found:
            logs += 1
            total += int(found[-1].replace(",", ""))
    calls = [json.loads(l) for l in (root / "harness-calls.jsonl").read_text().splitlines()] if (root / "harness-calls.jsonl").exists() else []
    workers = [c for c in calls if c.get("argv", "").split()[:1] not in (["--version"], ["-V"], ["login"], ["models"], [])]
    return {"worker_invocations": len(workers), "worker_logs_with_tokens": logs, "worker_tokens": total,
            "worker_tokens_complete": logs >= len([c for c in workers if c["harness"] == "codex"]) and
            not [c for c in workers if c["harness"] != "codex"]}


def v31_invariants(root: Path, fixture: str) -> dict:
    db = root / "data" / "runs.db"
    if not db.exists():
        return {"db": False}
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    q = lambda sql, *a: con.execute(sql, a).fetchall()  # noqa: E731
    out = {"db": True}
    runs = q("SELECT id, phase, office_version, goal FROM runs WHERE office_version IS NOT NULL AND goal NOT LIKE 'office doctor%'")
    out["runs"] = [dict(r) for r in runs]
    out["visual_pass_not_comparable"] = len(q("SELECT id FROM gates WHERE kind='visual' AND verdict='PASS' "
                                               "AND COALESCE(evidence_status,'') NOT IN ('COMPARABLE','NOT_APPLICABLE')"))
    out["visual_verdicts"] = [dict(r) for r in q("SELECT verdict, evidence_status FROM gates WHERE kind='visual'")]
    # Stale results stay audit-only: record how many PASS gates went stale (informational).
    out["stale_pass_gates"] = len(q("SELECT id FROM gates WHERE verdict='PASS' AND stale_reason IS NOT NULL"))
    out["version_mismatch_dispatches"] = len(q(
        "SELECT d.id FROM dispatches d JOIN runs r ON r.id=d.run_id WHERE d.office_version IS NOT NULL "
        "AND d.office_version != r.office_version")) if "office_version" in {r[1] for r in q("PRAGMA table_info(dispatches)")} else None
    out["reviewer_dispatches"] = [dict(r) for r in q(
        "SELECT role, triple, status, terminal_classification FROM dispatches WHERE kind='reviewer'")] if "kind" in {
        r[1] for r in q("PRAGMA table_info(dispatches)")} else []
    out["code_review_verdicts"] = [r[0] for r in q("SELECT verdict FROM gates WHERE kind='code_review'")]
    out["changes_required_rounds"] = len(q("SELECT id FROM gates WHERE verdict='CHANGES_REQUIRED'"))
    if fixture.startswith("F08"):
        others = [r for r in runs if "Unrelated placeholder" in (r["goal"] or "")]
        out["second_run_dispatches"] = sum(len(q("SELECT id FROM dispatches WHERE run_id=?", r["id"])) for r in others)
    return out


def median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def spread(xs):
    xs = [x for x in xs if x is not None]
    if len(xs) < 2 or not statistics.median(xs):
        return 0.0
    return (max(xs) - min(xs)) / statistics.median(xs)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=str(EVAL_HOME / "results.jsonl"))
    ap.add_argument("--markdown")
    ap.add_argument("--jobs-out", default=str(EVAL_HOME / "scored.jsonl"))
    args = ap.parse_args()
    jobs = []
    for line in Path(args.results).read_text().splitlines():
        rec = json.loads(line)
        root = Path(rec["root"])
        rec.update(score_transcripts(root))
        rec.update(worker_tokens(root))
        if rec["version"] == "v31":
            rec["invariants"] = v31_invariants(root, rec["fixture"])
        rec["total_tokens"] = rec["orchestrator_tokens"] + rec["worker_tokens"] if rec["worker_tokens_complete"] else None
        jobs.append(rec)
    Path(args.jobs_out).write_text("".join(json.dumps(j) + "\n" for j in jobs))

    by = defaultdict(list)
    for j in jobs:
        by[(j["fixture"], j["version"])].append(j)
    fixtures = sorted({f for f, _ in by})
    lines = ["| fixture | ver | n | admin calls | total calls | TTFD s | wall s | orch tokens | total tokens | "
             "landed+hidden | spread wall/tokens |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    ratios = defaultdict(list)
    for f in fixtures:
        med = {}
        for v in ("v3", "v31"):
            js = by.get((f, v), [])
            if not js:
                continue
            m = {k: median([j.get(k) for j in js]) for k in
                 ("admin_calls", "tool_calls", "time_to_first_executor", "wall_seconds", "orchestrator_tokens", "total_tokens")}
            med[v] = m
            ok = sum(1 for j in js if j["landed"] and j["hidden_pass"])
            sp = f"{spread([j['wall_seconds'] for j in js]):.0%}/{spread([j.get('total_tokens') or j['orchestrator_tokens'] for j in js]):.0%}"
            lines.append(f"| {f} | {v} | {len(js)} | {m['admin_calls']} | {m['tool_calls']} | {m['time_to_first_executor']} | "
                         f"{m['wall_seconds']} | {m['orchestrator_tokens']} | {m['total_tokens'] if m['total_tokens'] is not None else 'unmeasured'} | "
                         f"{ok}/{len(js)} | {sp} |")
        if "v3" in med and "v31" in med:
            for k in med["v3"]:
                a, b = med["v31"][k], med["v3"][k]
                if a is not None and b:
                    ratios[k].append(a / b)
    lines.append("")
    lines.append("Median of per-fixture v3.1/v3 median ratios:")
    targets = {"admin_calls": 0.5, "time_to_first_executor": 1.0, "wall_seconds": 1.1, "total_tokens": 1.1}
    for k, rs in ratios.items():
        med_r = statistics.median(rs)
        tgt = targets.get(k)
        verdict = "" if tgt is None else (" PASS" if med_r <= tgt else " FAIL") + f" (target <= {tgt:.0%})"
        lines.append(f"- {k}: {med_r:.2f} over {len(rs)} fixtures{verdict}")
    hard = defaultdict(int)
    for j in jobs:
        if j["version"] != "v31":
            continue
        inv = j.get("invariants") or {}
        hard["hand_json"] += j["hand_json"]
        hard["telemetry_only_turns"] += j["telemetry_only_turns"]
        hard["source_reads"] += j["source_reads"]
        hard["visual_pass_not_comparable"] += inv.get("visual_pass_not_comparable") or 0
        hard["version_mismatch_dispatches"] += inv.get("version_mismatch_dispatches") or 0
        hard["second_run_dispatches"] += inv.get("second_run_dispatches") or 0
        if j["fixture"].startswith("F04"):
            hard["F04_visual_pass"] += sum(1 for g in inv.get("visual_verdicts", []) if g["verdict"] == "PASS")
    lines.append("")
    lines.append("v3.1 hard requirements (counts across all v3.1 jobs; each must be 0):")
    for k, v in hard.items():
        lines.append(f"- {k}: {v}")
    need5 = sorted({f"{f}/{v}" for (f, v), js in by.items() if len(js) < 5 and
                    (spread([j["wall_seconds"] for j in js]) > 0.2 or
                     spread([j.get("total_tokens") or j["orchestrator_tokens"] for j in js]) > 0.2)})
    lines.append("")
    lines.append("Needs 5 repetitions (spread > 20%): " + (", ".join(need5) or "none"))
    text = "\n".join(lines)
    print(text)
    if args.markdown:
        Path(args.markdown).write_text(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
