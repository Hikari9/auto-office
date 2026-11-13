#!/usr/bin/env python3
"""Independent replay of the 2026-11-14 token-economy routing report (#862).

Inputs are PRIVATE local files; output is sanitized aggregates only (no run,
task, dispatch or session IDs, no paths):

  --tasks-cost   the report's frozen per-task attempt file (tasks-cost.json)
  --scoreboard   rico-mode state/executor-scoreboard.jsonl (any later version)
  --db           a consistent runs.db copy, for the Claude transcript audit
  --claude-projects  ~/.claude/projects, for the Claude transcript audit
  --prices       the report's prices.json (USD per MTok)

Checks:
  1. population: task and attempt counts of the frozen file, and whether every
     frozen task is unchanged in the scoreboard;
  2. report table 3.2 (tasks by first route): land, one-shot, attempts per task,
     chain cost mean/median, and the 90% bootstrap interval both with the
     report's own LCG (seed 42, 4,000 draws) and an independent Python RNG;
  3. Claude executor cost: re-derived from raw session transcripts, including
     subagent transcripts, deduplicated by message id, against the report's
     per-attempt figure.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
import glob
import json
import os
from pathlib import Path
import random
import sqlite3
import statistics
from urllib.parse import quote


def lcg_bootstrap(xs, draws=4000, seed=42):
    """The report's bootstrap, bit-for-bit (final.mjs boot())."""
    if len(xs) < 5:
        return None
    s = seed
    means = []
    for _ in range(draws):
        total = 0.
        for _ in xs:
            s = (s * 1664525 + 1013904223) % 4294967296
            total += xs[int(s / 4294967296 * len(xs))]
        means.append(total / len(xs))
    means.sort()
    return [round(means[int(draws * .05)], 2), round(means[int(draws * .95)], 2)]


def py_bootstrap(xs, draws=4000, seed=862):
    if len(xs) < 5:
        return None
    rng = random.Random(seed)
    n = len(xs)
    means = sorted(sum(xs[rng.randrange(n)] for _ in range(n)) / n for _ in range(draws))
    return [round(means[int(draws * .05)], 2), round(means[int(draws * .95)], 2)]


def first_route_table(tasks, min_tasks=9):
    out = []
    for size in ("XS", "S", "M", "L", "XL"):
        groups = defaultdict(list)
        for t in tasks:
            if t["size"] == size and t["atts"]:
                groups[min(t["atts"], key=lambda a: a["pos"])["route"]].append(t)
        for route, rows in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            if len(rows) < min_tasks:
                continue
            first = [min(t["atts"], key=lambda a: a["pos"]) for t in rows]
            covered = [t for t in rows if all(a["covered"] for a in t["atts"])]
            chain = [sum(a["usd"] + a["revUsd"] for a in t["atts"]) for t in covered]
            out.append({
                "size": size, "first_route": route, "tasks": len(rows),
                "landed_on_rung1": round(sum(a["outcome"] == "accepted" for a in first) / len(rows), 4),
                "one_shot": round(sum(a["kind"] == "oneshot" for a in first) / len(rows), 4),
                "attempts_per_task": round(statistics.mean(len(t["atts"]) for t in rows), 2),
                "cost_n": len(covered),
                "chain_usd_mean": round(statistics.mean(chain), 2) if chain else None,
                "chain_usd_median": round(statistics.median(chain), 2) if chain else None,
                "ci90_report_lcg": lcg_bootstrap(chain),
                "ci90_independent_rng": py_bootstrap(chain),
            })
    return out


def population(tasks, scoreboard_path):
    out = {"frozen_tasks": len(tasks), "frozen_attempts": sum(len(t["atts"]) for t in tasks)}
    if scoreboard_path:
        sb = {(r["run"], r["task"]): r for r in map(json.loads, scoreboard_path.read_text().splitlines())}
        changed = sum(1 for t in tasks if (t["run"], t["task"]) not in sb
                      or len(sb[(t["run"], t["task"])]["attempts"]) != len(t["atts"])
                      or sb[(t["run"], t["task"])]["outcome"] != t["outcome"])
        out.update(scoreboard_tasks=len(sb), scoreboard_attempts=sum(len(r["attempts"]) for r in sb.values()),
                   frozen_tasks_missing_or_changed=changed)
    return out


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def claude_cost_audit(tasks, scoreboard_path, db, projects, prices):
    """Re-derive Claude executor list-price cost from raw transcripts."""
    sb = {(r["run"], r["task"]): r for r in map(json.loads, scoreboard_path.read_text().splitlines())}
    con = sqlite3.connect("file:" + quote(str(db)) + "?mode=ro", uri=True)
    disp = {r[0]: r[1:] for r in con.execute("SELECT id, session_id, started_at, ended_at FROM dispatches")}
    con.close()
    files = defaultdict(list)
    for f in glob.glob(os.path.join(projects, "*", "*.jsonl")):
        files[Path(f).stem].append(f)
    for f in glob.glob(os.path.join(projects, "*", "*", "subagents", "*.jsonl")):
        files[Path(f).parts[-3]].append(f)
    cache = {}

    def messages(sid):
        if sid not in cache:
            seen = {}
            for f in files.get(sid, []):
                with open(f, errors="ignore") as fh:
                    for line in fh:
                        try:
                            j = json.loads(line)
                        except ValueError:
                            continue
                        m = j.get("message") or {}
                        if m.get("usage") and m.get("id") and j.get("timestamp"):
                            seen[m["id"]] = (_ts(j["timestamp"]), m.get("model", ""), m["usage"])
            cache[sid] = list(seen.values())
        return cache[sid]

    def cost(model, u):
        key = next((k for k in prices if k in model), None)
        if key is None:
            return 0.
        p = prices[key]
        i, cw = u.get("input_tokens", 0), u.get("cache_creation_input_tokens", 0)
        cr, o = u.get("cache_read_input_tokens", 0), u.get("output_tokens", 0)
        if "over100k" in p and i + cw + cr > 100_000:
            p = p["over100k"]
        return (i * p["in"] + cw * p["cw"] + cr * p["cr"] + o * p["out"]) / 1e6

    pairs = defaultdict(list)
    for t in tasks:
        full = sb.get((t["run"], t["task"]))
        if not full:
            continue
        for att, seg in zip(sorted(t["atts"], key=lambda a: a["pos"]), full["attempts"]):
            if not att["route"].startswith("claude/") or not att["covered"]:
                continue
            mine = 0.
            for did in seg["dispatches"]:
                sid, start, end = disp.get(did, (None, None, None))
                if not sid or not start:
                    continue
                lo, hi = _ts(start) - 1, (_ts(end) + 1) if end else float("inf")
                mine += sum(cost(m, u) for ts, m, u in messages(sid) if lo <= ts <= hi)
            pairs[(t["size"], att["route"])].append((att["usd"], mine))
    every = [x for v in pairs.values() for x in v]
    return {
        "attempts": len(every),
        "report_total_usd": round(sum(a for a, _ in every), 2),
        "raw_transcript_total_usd": round(sum(b for _, b in every), 2),
        "attempts_within_5pct_or_5c": sum(abs(a - b) <= max(.05, .05 * a) for a, b in every),
        "by_size_route": [{"size": s, "route": r, "n": len(v),
                           "report_mean_usd": round(statistics.mean(a for a, _ in v), 2),
                           "raw_mean_usd": round(statistics.mean(b for _, b in v), 2)}
                          for (s, r), v in sorted(pairs.items()) if len(v) >= 5],
        "note": "includes subagent transcripts; without them haiku@high M reads $2.88 vs $3.40",
    }


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tasks-cost", type=Path, required=True)
    p.add_argument("--scoreboard", type=Path)
    p.add_argument("--db", type=Path)
    p.add_argument("--claude-projects", default=os.path.expanduser("~/.claude/projects"))
    p.add_argument("--prices", type=Path)
    args = p.parse_args()
    tasks = json.loads(args.tasks_cost.read_text())
    out = {"population": population(tasks, args.scoreboard), "table_3_2": first_route_table(tasks)}
    if args.db and args.scoreboard and args.prices:
        out["claude_cost_audit"] = claude_cost_audit(tasks, args.scoreboard, args.db, args.claude_projects,
                                                     json.loads(args.prices.read_text()))
    print(json.dumps(out, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
