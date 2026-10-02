"""Opt-in, in-session benchmark refresh for one run (#229).

The user opts in at intake (`office start --benchmark-refresh`); the choice is
recorded on the run either way. Office never fetches anything itself, so route
selection stays offline. When the catalog has rows without a score under its
benchmark index, `office benchmarks brief` hands the orchestrator a bounded
brief for one low-cost background subagent, which fetches Artificial Analysis
data and returns a delta through `office benchmarks submit <file>`.

A delta is accepted whole or not at all. It must use the catalog's index
version, name existing catalog rows (harness, model, effort) that have no score
yet, and carry a numeric score with its Artificial Analysis source. It never
replaces a trusted score and never infers a missing one. An accepted delta is
a run-scoped snapshot named by its hash; routes decided after it record that
hash, and earlier routes keep theirs.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from office import candidates, db, paths, state
from office.result import Result
from office.state import Refused, Usage
from office.util import atomic_write_json, now_iso, parse_iso, sha256_obj, short

MAX_REFRESHES = 1  # one background refresh per run: no refresh loop
SOURCE_PREFIX = "https://artificialanalysis.ai/"
SUBMIT_FORM = "office benchmarks submit <delta.yaml>"


def index_version() -> str:
    data = yaml.safe_load((paths.resources_root() / "catalog" / "seed.yaml").read_text(encoding="utf-8")) or {}
    return str(data.get("benchmark_index_version") or "")


def choice(run: dict) -> dict:
    return run.get("benchmark_refresh") or {"enabled": False}


def _key(row: dict) -> tuple[str, str, str]:
    return (str(row.get("harness") or row.get("invocation_harness") or ""), str(row.get("model_id") or ""),
            str(row.get("effort") or ""))


def missing() -> list[dict]:
    """Dispatchable catalog rows with no score under the catalog's index."""
    index = index_version()
    out = []
    for row in candidates.catalog_rows():
        if row.get("dispatchable") is False or row.get("alias_family"):
            continue
        if (row.get("benchmark_indexes") or {}).get(index) is None:
            h, m, e = _key(row)
            out.append({"harness": h, "model_id": m, "effort": e})
    return out


def _require_enabled(run: dict) -> dict:
    c = choice(run)
    if not c.get("enabled"):
        raise Refused("benchmark-refresh-off", "this run did not opt in to a benchmark refresh at intake",
                      next_step="no action; routes use the shipped catalog")
    return c


def brief(con, run: dict) -> Result:
    """Start the run's one bounded refresh: write the subagent brief and count it."""
    c = _require_enabled(run)
    rows = missing()
    if not rows:
        return Result(lines=["every dispatchable catalog row has a score; no refresh needed"], next="no action")
    if int(c.get("used") or 0) >= int(c.get("max") or MAX_REFRESHES):
        raise Refused("benchmark-refresh-spent", f"this run's {c.get('max') or MAX_REFRESHES} benchmark refresh is used",
                      next_step="no action; routes keep the current snapshot")
    n = int(c.get("used") or 0) + 1
    target = paths.run_dir(run["id"]) / "benchmarks" / f"delta-{n}.yaml"
    path = target.with_name(f"brief-{n}.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_brief_text(rows, target), encoding="utf-8")
    with db.transaction(con):
        state.update_run(con, run["id"], benchmark_refresh={**c, "used": n, "started_at": now_iso(), "brief": str(path)})
        state.emit(con, run, "benchmarks.refresh_started", f"benchmark refresh {n} started for {len(rows)} rows",
                   audience="runtime")
    return Result(lines=[f"benchmark refresh {n}/{c.get('max') or MAX_REFRESHES}: {len(rows)} catalog rows lack "
                         f"{index_version()} scores", f"brief: {path}"],
                  next=("start one low-cost background subagent (in-session, smallest capable model, low effort) on "
                        "that brief; keep working; it ends with office benchmarks submit"))


def _brief_text(rows: list[dict], target: Path) -> str:
    listing = "\n".join(f"- harness {r['harness']}, model {r['model_id']}, effort {r['effort']}" for r in rows)
    return f"""ROLE benchmark refresher. One pass, then stop. Do not edit anything except {target}.

Fetch Artificial Analysis ({SOURCE_PREFIX}) pages for these catalog rows and record their
{index_version()} score:
{listing}

Rules:
- Use only {index_version()}. A page showing another index version is not a score for it.
- Map each row to the page for the same model and the same reasoning effort. If the effort
  mapping is unclear, leave the row out.
- Copy scores exactly. Never infer, average, or interpolate a missing score; leave the row out.
- Make at most one fetch per row and no retries loop.

Write {target}:
index_version: {index_version()}
source: {SOURCE_PREFIX}
fetched_at: <UTC ISO time of the fetch>
rows:
  - harness: <harness>
    model_id: <model_id>
    effort: <effort>
    score: <number>
    url: <the page the score came from>

Then run: {SUBMIT_FORM.replace('<delta.yaml>', str(target))}
If nothing could be fetched, write rows: [] and still submit, so the failure is recorded.
"""


def validate(delta: dict) -> tuple[list[dict], list[str]]:
    """(rows, problems). Any problem rejects the whole delta."""
    problems = []
    index = index_version()
    if not isinstance(delta, dict):
        return [], ["the delta is not a mapping"]
    if delta.get("index_version") != index:
        problems.append(f"index version {delta.get('index_version')!r} is not the catalog's {index!r}; "
                        "scores from different index versions are not comparable")
    if not str(delta.get("source") or "").startswith(SOURCE_PREFIX):
        problems.append(f"source must be {SOURCE_PREFIX}")
    try:
        parse_iso(str(delta.get("fetched_at") or ""))
    except (TypeError, ValueError):
        problems.append("fetched_at is not an ISO time")
    rows = delta.get("rows")
    if not isinstance(rows, list) or not rows:
        problems.append("no rows: nothing was fetched")
        return [], problems
    open_rows = {_key(r) for r in missing()}
    known = {_key(r) for r in candidates.catalog_rows()}
    seen, out = set(), []
    for i, r in enumerate(rows, 1):
        if not isinstance(r, dict):
            problems.append(f"row {i} is not a mapping")
            continue
        key = _key(r)
        label = f"row {i} ({'/'.join(key)})"
        if key in seen:
            problems.append(f"{label} repeats")
        seen.add(key)
        if key not in known:
            problems.append(f"{label} is not a catalog row (unknown model or effort mapping)")
        elif key not in open_rows:
            problems.append(f"{label} already has a trusted score; a refresh never replaces one")
        score = r.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 < score <= 100:
            problems.append(f"{label} score {score!r} is not a number in (0, 100]")
        if not str(r.get("url") or "").startswith(SOURCE_PREFIX):
            problems.append(f"{label} url must be an Artificial Analysis page")
        out.append({"harness": key[0], "model_id": key[1], "effort": key[2], "score": score, "url": r.get("url")})
    return out, problems


def submit(con, run: dict, path: str) -> Result:
    c = _require_enabled(run)
    if not c.get("used"):
        raise Refused("benchmark-refresh-not-started", "no refresh was started for this run",
                      next_step="office benchmarks brief")
    file = Path(path).expanduser()
    try:
        delta = yaml.safe_load(file.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise Usage("bad-delta", f"cannot read {file}: {exc}", next_step=SUBMIT_FORM)
    rows, problems = validate(delta)
    if problems:
        with db.transaction(con):
            state.update_run(con, run["id"], benchmark_refresh={**c, "failed": problems[:8], "failed_at": now_iso()})
            state.emit(con, run, "benchmarks.refresh_failed", f"benchmark refresh rejected: {problems[0][:160]}")
        raise Refused("benchmark-delta-invalid", "; ".join(problems[:6]), scope="benchmarks",
                      preserved="the current snapshot; no score was changed",
                      next_step="no action; routes keep the current snapshot")
    body = {"index_version": delta["index_version"], "source": delta["source"], "fetched_at": str(delta["fetched_at"]),
            "base_catalog": snapshot_base(run), "rows": rows}
    digest = sha256_obj(body)
    snap = paths.run_dir(run["id"]) / "benchmarks" / f"snapshot-{digest[:12]}.json"
    atomic_write_json(snap, {**body, "hash": digest})
    with db.transaction(con):
        state.update_run(con, run["id"], benchmark_refresh={**c, "snapshot": digest, "snapshot_path": str(snap),
                                                             "accepted_at": now_iso(), "failed": None})
        state.emit(con, run, "benchmarks.refreshed", f"benchmark snapshot {digest[:12]} accepted: {len(rows)} new scores "
                   f"({delta['index_version']}); later routes use it")
    return Result(lines=[f"benchmark snapshot {digest[:12]} accepted for run {short(run['id'])}: "
                         + ", ".join(f"{r['model_id']}@{r['effort']}={r['score']}" for r in rows)],
                  next="no action; routes decided from now on use and record this snapshot")


def snapshot_base(run: dict) -> str | None:
    return run.get("catalog_hash")


def apply(run: dict, cands: list[dict]) -> str | None:
    """Fill candidates' missing scores from the run's accepted snapshot. Returns
    the snapshot hash a route decided now should record (the catalog's when the
    run has no snapshot)."""
    snap = choice(run).get("snapshot")
    path = choice(run).get("snapshot_path")
    if not snap or not path:
        return snapshot_base(run)
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return snapshot_base(run)  # an unreadable snapshot is never trusted
    if data.get("hash") != snap:
        return snapshot_base(run)
    scores = {(r["harness"], r["model_id"], r["effort"]): r["score"] for r in data.get("rows") or []}
    index = data["index_version"]
    for c in cands:
        score = scores.get((c["harness"], c["model_id"], str(c.get("effort") or "")))
        if score is not None and (c.get("benchmark_indexes") or {}).get(index) is None:
            c["benchmark_indexes"] = {**(c.get("benchmark_indexes") or {}), index: score}
    return snap
