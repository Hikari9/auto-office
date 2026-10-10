#!/usr/bin/env python3
"""route_replay.py -- replay recorded executor/worker routing decisions under discovery off and on (#494).

Reads the `route_audit` rows of a COPY of runs.db and, for each recorded decision, asks HEAD's router the same
question again (`candidates.route_role` for the recorded run, task, role and plan version) under four policies:

  pinned      the run's own pinned policy. A run pinned before #494 has no discovery provenance, so it cannot discover.
  pinned-off  the same pinned policy with `routing.discovery.enabled` forced to false.
  head-off    HEAD's resolved policy with discovery off.
  head-on     HEAD's resolved policy with discovery on (shipped caps: 2 probes and 1 live trial per run, 15 percent
              of the last 20 pooled executor/worker decisions).

What it reports (counts only; no goal, title, brief or task text is read into the report):
  * pinned vs pinned-off: how many decisions are identical (selected route and `decision_hash`). Discovery off must
    change nothing, so every decision must be identical.
  * pinned vs the recorded primary route: how often the route chosen then is the route chosen now. This is drift of
    the catalog, evidence and quota since the decision was recorded; it is information, not a check.
  * head-on vs head-off: which decisions would have drawn a probe, which of those would have become a trial if the
    probe passed (an upper bound: no model is called), which gate or cap stopped the rest, and how often each cap
    bound. Allocation is simulated in recorded order: per-run probe and trial counts, and the rolling window of the
    last 20 dispatch decisions with the trials this replay itself granted.

`route_audit` keeps the scored decision, not the raw request, so each request is rebuilt from the copy with HEAD's
own builder. The copy is never written back, no quota is probed, no model is called and nothing is launched. (Resolving a
route reads each installed harness's `--version`, as every routing decision does.)
The live runs.db is refused: pass a copy, or `--snapshot-live` to take one with SQLite's read-only backup API.

  scripts/route_replay.py --db /tmp/runs-copy.db [--json out.json] [--limit N]
  scripts/route_replay.py --snapshot-live [--json out.json]
  scripts/route_replay.py --self-test
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import json
import math
import os
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter
from pathlib import Path
from urllib.parse import quote

try:
    import office  # noqa: F401  (probe only: is the package importable)
except ImportError:  # run from a checkout without the package installed
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

ROLES = ("executor", "worker")
ALLOCATING_PHASES = ("dispatch", "reroute")  # a plan-phase row is a preview: it launches nothing, so it spends no cap
WINDOW = 20


# ------------------------------------------------------------------ the copy

def live_db() -> Path:
    from office import paths
    return paths.runs_db()


def snapshot(src: Path, dest_dir: Path) -> Path:
    """A consistent copy of `src`, taken through SQLite's backup API on a read-only connection."""
    dest = dest_dir / "runs-copy.db"
    ro = sqlite3.connect(f"file:{quote(str(src))}?mode=ro", uri=True, timeout=30)
    try:
        out = sqlite3.connect(str(dest))
        try:
            ro.backup(out)
        finally:
            out.close()
    finally:
        ro.close()
    return dest


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def refuse_live(db_path: Path) -> str | None:
    live = live_db()
    if db_path.resolve() == live.resolve():
        return (f"{db_path} is the live runs.db. Replay a copy: "
                f"scripts/route_replay.py --snapshot-live  (or: sqlite3 {db_path} '.backup /tmp/runs-copy.db')")
    return None


# ------------------------------------------------------------------ policies

def head_config(enabled: bool) -> dict:
    """HEAD's resolved policy (shipped defaults and the user tier, no repo tier), discovery on or off."""
    from office import config as cfg, route_policy
    config = copy.deepcopy(cfg.resolve(None)[0])
    config["routing"]["discovery"]["enabled"] = enabled
    config[route_policy.DIGEST_KEY] = route_policy.policy_digest(config)
    return config


def configs_for(run: dict) -> dict[str, dict]:
    from office import state
    pinned = copy.deepcopy(state.pinned_config(run))
    off = copy.deepcopy(pinned)
    off.setdefault("routing", {}).setdefault("discovery", {})["enabled"] = False
    return {"pinned": pinned, "pinned-off": off, "head-off": head_config(False), "head-on": head_config(True)}


# ------------------------------------------------------------------ simulated allocation

class Allocation:
    """What the caps would have been at each recorded decision, had discovery been on from the first row."""

    def __init__(self):
        self.probes: Counter = Counter()
        self.trials: Counter = Counter()
        self.history: list[bool] = []  # one flag per dispatch decision so far: did this replay grant it a trial

    def snapshot(self, run_id: str | None, settings: dict) -> dict:
        recent = self.history[-WINDOW:]
        window = min(WINDOW, len(recent) + 1)
        percent = float(settings["max_trial_percent_rolling_20"])
        cap = int(math.floor(window * percent / 100.0 + 1e-9))
        return {"probes": {"used": self.probes[run_id], "max": int(settings["max_probes_per_run"])},
                "trials": {"used": self.trials[run_id], "max": int(settings["max_trials_per_run"])},
                "rolling": {"used": sum(recent), "max": cap, "window": window, "percent": percent,
                            "warmup": cap == 0 and percent > 0}}


@contextlib.contextmanager
def simulated(allocation: Allocation, passing_key: str | None = None):
    """HEAD's router with its allocation read from `allocation` and, optionally, a fresh exact pass for one probe key."""
    from office import route_probe
    real_allocation, real_status = route_probe.allocation, route_probe.status

    def fake_allocation(con, run_id, settings):
        return allocation.snapshot(run_id, settings)

    def fake_status(con, cand, **kw):
        record = real_status(con, cand, **kw)
        if record is None and passing_key is not None:
            adapter = kw.get("adapter")
            if route_probe.key(cand, route_probe._adapter(cand, adapter)) == passing_key:
                return {"result": "pass", "reason_class": None, "probed_at": route_probe._utcnow().isoformat(),
                        "fresh": True, "attempt_id": "replay"}
        return record

    route_probe.allocation, route_probe.status = fake_allocation, fake_status
    try:
        yield
    finally:
        route_probe.allocation, route_probe.status = real_allocation, real_status


# ------------------------------------------------------------------ the replay

def decide(con, config: dict, run: dict, row: dict, *, discovery_input: dict | None = None) -> dict:
    from office import candidates
    return candidates.route_role(con, config, run, row["role"], task_id=row["task_id"], probe=False,
                                 discovery_input=discovery_input)


def load_rows(con, limit: int | None = None) -> list[dict]:
    try:
        rows = con.execute(
            "SELECT id, run_id, task_id, role, phase, plan_version, decision_hash, primary_route, dispatched_route, "
            f"created_at FROM route_audit WHERE role IN ({','.join('?' * len(ROLES))}) ORDER BY created_at, rowid",
            ROLES).fetchall()
    except sqlite3.OperationalError:  # a database that never recorded a route decision
        return []
    out = [dict(r) for r in rows]
    return out[:limit] if limit else out


def key_of(result: dict) -> tuple:
    return result.get("status"), result.get("selected"), result.get("decision_hash")


def replay(db_path: Path, *, limit: int | None = None) -> dict:
    """Replay every recorded executor/worker decision of the copy at `db_path`. Returns the report."""
    from office import db, route_policy, state
    con = db.connect(db_path)
    try:
        rows = load_rows(con, limit)
        report = {
            "source": {"decisions": len(rows), "by_phase": dict(Counter(r["phase"] for r in rows)),
                       "runs": len({r["run_id"] for r in rows})},
            "skipped": Counter(),
            "pinned": {"pinned_before_discovery": 0, "pinned_with_discovery_provenance": 0,
                       "identical_to_discovery_off": 0, "different_from_discovery_off": 0,
                       "provenance_runs_identical_to_discovery_off": 0, "provenance_runs_different_from_discovery_off": 0,
                       "same_primary_as_recorded": 0, "different_primary_from_recorded": 0},
            "head_off": {"decisions": 0, "no_discovery_block": 0},
            "discovery_on": {"decisions": 0, "unchanged_selection": 0, "probe_draws": 0, "would_become_trial": 0,
                             "plan_previews_with_a_probe_candidate": 0, "blocked": Counter(), "caps_bound": Counter(),
                             "plan_previews_blocked": Counter(), "runs_with_a_trial": 0},
        }
        seen_runs: dict[str, dict] = {}
        allocation, trial_runs = Allocation(), set()
        for row in rows:
            run = seen_runs.get(row["run_id"]) or state.get_run(con, row["run_id"])
            if run is None or not row["task_id"]:
                report["skipped"]["no-run" if run is None else "no-task"] += 1
                continue
            seen_runs[row["run_id"]] = run
            run = {**run, "plan_version": row["plan_version"] or run.get("plan_version")}
            try:
                results = replay_one(con, run, row, allocation, trial_runs)
            except Exception as exc:  # noqa: BLE001 - a decision that cannot be rebuilt is counted, not hidden
                report["skipped"][f"error:{type(exc).__name__}"] += 1
                continue
            tally(report, run, row, results)
        report["pinned"]["pinned_before_discovery"] = sum(
            route_policy.PROVENANCE_KEY not in state.pinned_config(r) for r in seen_runs.values())
        report["pinned"]["pinned_with_discovery_provenance"] = len(seen_runs) - report["pinned"]["pinned_before_discovery"]
        report["discovery_on"]["runs_with_a_trial"] = len(trial_runs)
        for name in ("blocked", "caps_bound", "plan_previews_blocked"):
            report["discovery_on"][name] = dict(sorted(report["discovery_on"][name].items()))
        report["skipped"] = dict(report["skipped"])
        return report
    finally:
        con.close()


def replay_one(con, run: dict, row: dict, allocation: Allocation, trial_runs: set) -> dict:
    configs = configs_for(run)
    results = {}
    for name in ("pinned", "pinned-off", "head-off"):
        results[name] = decide(con, configs[name], run, row)
    config = configs["head-on"]
    spends = row["phase"] in ALLOCATING_PHASES
    with simulated(allocation):
        first = decide(con, config, run, row)
    disc = first.get("discovery") or {}
    outcome = {"first": first, "trial": None}
    if disc.get("intent") == "probe" and spends:
        allocation.probes[run["id"]] += 1  # the preflight reserves its probe before it runs it
        handle = {"candidate": disc["candidate"], "probe_key": disc["probe_key"], "reservation_id": "replay",
                  "attempt_id": "replay"}
        with simulated(allocation, passing_key=disc["probe_key"]):
            outcome["trial"] = decide(con, config, run, row, discovery_input=handle)
        if (outcome["trial"].get("discovery") or {}).get("intent") == "trial":
            allocation.trials[run["id"]] += 1
            trial_runs.add(run["id"])
    if spends:
        allocation.history.append(bool(outcome["trial"] and (outcome["trial"].get("discovery") or {}).get("intent") == "trial"))
    results["head-on"] = outcome
    return results


def tally(report: dict, run: dict, row: dict, results: dict) -> None:
    from office import route_policy, state
    pinned, off = results["pinned"], results["pinned-off"]
    # A run pinned before discovery existed cannot discover, so forcing discovery off must change nothing. A run
    # pinned under a discovery-era policy may have it on, and then the two legitimately differ.
    prefix = "" if route_policy.PROVENANCE_KEY not in state.pinned_config(run) else "provenance_runs_"
    report["pinned"][f"{prefix}identical_to_discovery_off" if key_of(pinned) == key_of(off)
                     else f"{prefix}different_from_discovery_off"] += 1
    recorded = row["primary_route"]
    if recorded and pinned.get("slate"):
        same = recorded.split("/", 1)[-1] == (pinned["slate"][0]["route"] or "").split("/", 1)[-1] or \
            recorded == pinned.get("selected")
        report["pinned"]["same_primary_as_recorded" if same else "different_primary_from_recorded"] += 1
    head_off = results["head-off"]
    report["head_off"]["decisions"] += 1
    report["head_off"]["no_discovery_block"] += "discovery" not in head_off
    on = report["discovery_on"]
    first, trial = results["head-on"]["first"], results["head-on"]["trial"]
    on["decisions"] += 1
    disc = first.get("discovery") or {}
    on["unchanged_selection"] += first.get("selected") == head_off.get("selected")
    if disc.get("intent") == "probe":
        if row["phase"] in ALLOCATING_PHASES:
            on["probe_draws"] += 1
            if trial and (trial.get("discovery") or {}).get("intent") == "trial":
                on["would_become_trial"] += 1
            elif trial:
                reason = (trial.get("discovery") or {}).get("blocked") or "no-trial"
                on["blocked"][reason] += 1
                if reason in ("trial-cap", "probe-cap", "rolling-cap"):
                    on["caps_bound"][reason] += 1
        else:
            on["plan_previews_with_a_probe_candidate"] += 1
    elif disc.get("blocked"):
        if row["phase"] in ALLOCATING_PHASES:
            on["blocked"][disc["blocked"]] += 1
            if disc["blocked"] in ("trial-cap", "probe-cap", "rolling-cap"):
                on["caps_bound"][disc["blocked"]] += 1
        else:
            on["plan_previews_blocked"][disc["blocked"]] += 1


def render(report: dict) -> list[str]:
    src, pin, off, on = report["source"], report["pinned"], report["head_off"], report["discovery_on"]
    total = (pin["identical_to_discovery_off"] + pin["different_from_discovery_off"]
             + pin["provenance_runs_identical_to_discovery_off"] + pin["provenance_runs_different_from_discovery_off"])
    lines = [
        f"route_audit decisions replayed: {total} of {src['decisions']} executor/worker rows "
        f"({src['runs']} runs; by phase {src['by_phase']}); skipped {report['skipped'] or 'none'}",
        f"runs pinned before discovery existed: {pin['pinned_before_discovery']}; "
        f"with discovery provenance: {pin['pinned_with_discovery_provenance']}",
        f"runs pinned before discovery, policy vs the same policy with discovery forced off: identical "
        f"{pin['identical_to_discovery_off']}, different {pin['different_from_discovery_off']}",
        f"runs pinned with discovery provenance, same comparison: identical {pin['provenance_runs_identical_to_discovery_off']}, "
        f"different {pin['provenance_runs_different_from_discovery_off']} (they may have discovery on)",
        f"pinned policy now vs the primary route recorded then: same {pin['same_primary_as_recorded']}, "
        f"different {pin['different_primary_from_recorded']} (catalog, evidence and quota drift; information only)",
        f"HEAD policy with discovery off: {off['decisions']} decisions, {off['no_discovery_block']} without a discovery block",
        f"HEAD policy with discovery on: {on['decisions']} decisions, selection unchanged for {on['unchanged_selection']}",
        f"  dispatch decisions that would have drawn a probe: {on['probe_draws']}",
        f"  of those, would have become a trial if the probe passed (upper bound; no model called): {on['would_become_trial']}",
        f"  plan previews showing a probe candidate (spend nothing): {on['plan_previews_with_a_probe_candidate']}",
        f"  runs that would have taken a trial: {on['runs_with_a_trial']}",
        f"  dispatch decisions stopped by: {on['blocked'] or 'nothing'}",
        f"  caps that bound at dispatch: {on['caps_bound'] or 'none'}",
        f"  plan previews stopped by: {on['plan_previews_blocked'] or 'nothing'} (previews spend no cap)",
    ]
    return lines


# ------------------------------------------------------------------ self-test

FAKE_CODEX = """#!{python}
import sys
if sys.argv[1:] == ["--version"]:
    print("codex-cli 0.162.0")
    sys.exit(0)
sys.exit(1)
"""
SENTINEL = "ZZ-PRIVATE-TASK-TEXT-ZZ"


def build_fixture(home: Path) -> Path:
    """A runs.db with one run pinned before discovery, one run pinned under HEAD's policy, and recorded decisions."""
    from office import candidates, db, paths, plan_view, route_learning, route_policy, state, version
    from office.util import dumps, now_iso
    con = db.connect(paths.runs_db())
    route_learning.ensure_schema(con)
    head = head_config(True)
    old = copy.deepcopy(head)
    for key in (route_policy.PROVENANCE_KEY, route_policy.DIGEST_KEY):
        old.pop(key, None)
    old["routing"]["adaptive"]["budget_ceiling_usd"] = 25.0
    now = now_iso()
    risk = {"size_class": "S", "blast_radius": "repo", "irreversible": False}
    for rid, config, tasks in (("old-run", old, ("T1", "T2")), ("new-run", head, ("T1", "T2", "T3"))):
        sdir = paths.run_dir(rid)
        sdir.mkdir(parents=True, exist_ok=True)
        with db.transaction(con):
            con.execute(
                "INSERT INTO runs(id, family_id, created_at, status, office_version, repo_root, git_common_dir, goal, phase, "
                "gear, playbook, base_sha, state_dir, requirements_version, plan_version, routing_version, policy_json, "
                "risk_json, gates_json, envelope_json, plan_review_json, planner_mode, updated_at, escalations_used) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (rid, rid, now, "executing", version.current(), str(home), str(home / ".git"), SENTINEL, "executing", "",
                 "Change", "0" * 40, str(sdir), 1, 1, 1, dumps(config), dumps(risk), "{}", "[]", "{}", "inline", now))
            for tid in tasks:
                con.execute(
                    "INSERT INTO tasks(run_id, id, title, role, scope_json, depends_json, interfaces_json, accept_json, "
                    "checks_json, visual_json, status, introduced_plan_version, contract_version, acceptance_version, "
                    "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rid, tid, SENTINEL, "executor", dumps([f"{tid.lower()}.py"]), "[]", "[]", "[]", "[]", None, "planned",
                     1, 1, 1, now, now))
    for rid, tasks in (("old-run", ("T1", "T2")), ("new-run", ("T1", "T2", "T3"))):
        run = state.get_run(con, rid)
        for phase in ("plan", "dispatch"):
            for tid in tasks:
                decision = candidates.route_role(con, state.pinned_config(run), run, "executor", task_id=tid, probe=False)
                audit = {**decision["routing"], "phase": phase, "task_id": tid, "role": "executor",
                         "decision_hash": decision["decision_hash"]}
                with db.transaction(con):
                    plan_view.record_audit(con, run, audit, plan_version=1,
                                           dispatched=decision["selected"] if phase == "dispatch" else None)
    con.close()
    return paths.runs_db()


def self_test() -> int:
    failures: list[str] = []

    def check(ok: bool, what: str) -> None:
        print(f"{'ok  ' if ok else 'FAIL'} {what}")
        if not ok:
            failures.append(what)

    with tempfile.TemporaryDirectory(prefix="route-replay-selftest-") as tmp:
        tmp = Path(tmp).resolve()
        for name in ("bin", "data", "state", "repo"):
            (tmp / name).mkdir()
        (tmp / "bin" / "codex").write_text(FAKE_CODEX.format(python=sys.executable))
        (tmp / "bin" / "codex").chmod(0o755)
        os.environ.update({"OFFICE_DATA_HOME": str(tmp / "data"), "OFFICE_STATE_HOME": str(tmp / "state"),
                           "OFFICE_USER_CONFIG": str(tmp / "user-config.yaml"), "OFFICE_QUOTA_PROBE": "off",
                           "PATH": os.pathsep.join([str(tmp / "bin"), "/usr/bin", "/bin"]),
                           "CODEX_HOME": str(tmp / "codex-home"), "CLAUDE_CONFIG_DIR": str(tmp / "claude-home")})
        os.environ.pop("AUTO_OFFICE_RUNS_DB", None)
        # A 100 percent rolling cap makes every dispatch decision draw, and no cost or exploration limit gets in the
        # way, so the per-run caps are what bind.
        (tmp / "user-config.yaml").write_text(
            "routing:\n  discovery:\n    max_trial_percent_rolling_20: 100\n  adaptive:\n    exploration:\n"
            "      {rate: 0.0, margin: 1.0, max_cost_vs_primary_percent: 100000}\n")
        os.chdir(tmp / "repo")
        live = build_fixture(tmp / "repo")
        before = (sha(live), live.stat().st_mtime_ns)
        check(refuse_live(live) is not None, "the live runs.db is refused")
        copy_path = snapshot(live, tmp)
        check(copy_path != live and sha(copy_path) != "", "a snapshot is a separate file")
        check(refuse_live(copy_path) is None, "a copy is accepted")
        odd = tmp / "odd name?#%.db"
        sqlite3.connect(str(odd)).close()
        (tmp / "odd").mkdir()
        check(snapshot(odd, tmp / "odd").is_file(), "a path with URI characters can still be snapshotted")
        from office import db as office_db
        empty = tmp / "empty.db"
        office_db.connect(empty).close()
        check(replay(empty)["source"]["decisions"] == 0, "a database with no recorded decisions replays to zero")
        report = replay(copy_path)
        check((sha(live), live.stat().st_mtime_ns) == before, "the source database is untouched")
        pin = report["pinned"]
        check(report["source"]["decisions"] == 10 and not report["skipped"], f"all 10 recorded decisions replayed {report['skipped']}")
        check(pin["different_from_discovery_off"] == 0 and pin["identical_to_discovery_off"] == 4,
              "discovery off and pinned pre-change decisions are identical")
        check(pin["pinned_before_discovery"] == 1 and pin["pinned_with_discovery_provenance"] == 1,
              "the run pinned before discovery is told apart from the run pinned under HEAD")
        on = report["discovery_on"]
        check((on["probe_draws"], on["would_become_trial"], on["runs_with_a_trial"]) == (2, 2, 2),
              f"each run's first dispatch would have probed and taken the run's one trial {on['probe_draws'], on['would_become_trial']}")
        check(on["caps_bound"] == {"trial-cap": 3} and on["blocked"] == {"trial-cap": 3},
              f"the per-run trial cap bound for the other three dispatches {on['caps_bound']}")
        check(on["plan_previews_with_a_probe_candidate"] >= 1, "plan previews are counted apart and spend no cap")
        check(report["head_off"]["no_discovery_block"] == report["head_off"]["decisions"], "HEAD with discovery off carries no discovery block")
        text = "\n".join(render(report)) + json.dumps(report)
        check(SENTINEL not in text, "the report carries no goal, title or task text")
        # the cap simulation binds: a run's second would-be trial is stopped by the per-run cap
        sim = Allocation()
        sim.trials["x"], sim.history = 1, [True]
        settings = {**head_config(True)["routing"]["discovery"], "max_trial_percent_rolling_20": 15}
        check(sim.snapshot("x", settings)["trials"] == {"used": 1, "max": 1}, "the simulated per-run trial count is read back")
        cold, six = Allocation(), Allocation()
        six.history = [False] * 6
        check((cold.snapshot("x", settings)["rolling"]["max"], six.snapshot("x", settings)["rolling"]["max"]) == (0, 1),
              "a cold history opens no rolling window: 6 decisions are needed for one trial at 15 percent")
        shutil.rmtree(tmp / "state", ignore_errors=True)
    print("FAILED" if failures else "route_replay self-test passed")
    return 1 if failures else 0


# ------------------------------------------------------------------ cli

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, help="a COPY of runs.db (the live file is refused)")
    parser.add_argument("--snapshot-live", action="store_true", help="copy the live runs.db read-only, then replay the copy")
    parser.add_argument("--json", type=Path, help="write the full report here")
    parser.add_argument("--limit", type=int, help="replay only the first N recorded decisions")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    if bool(args.db) == bool(args.snapshot_live):
        parser.error("give exactly one of --db COPY or --snapshot-live")
    with tempfile.TemporaryDirectory(prefix="route-replay-") as tmp:
        tmp = Path(tmp).resolve()
        if args.snapshot_live:
            source = live_db()
            copy_path = snapshot(source, tmp)
        else:
            problem = refuse_live(args.db)
            if problem:
                print(problem, file=sys.stderr)
                return 2
            copy_path = tmp / "runs-copy.db"
            shutil.copyfile(args.db, copy_path)
            for suffix in ("-wal", "-shm"):
                if Path(f"{args.db}{suffix}").exists():
                    shutil.copyfile(f"{args.db}{suffix}", f"{copy_path}{suffix}")
        # Everything below runs against the private copy and a private, empty Office home.
        os.environ.update({"AUTO_OFFICE_RUNS_DB": str(copy_path), "OFFICE_STATE_HOME": str(tmp / "state"),
                           "OFFICE_DATA_HOME": str(tmp / "data"), "OFFICE_QUOTA_PROBE": "off"})
        report = replay(copy_path, limit=args.limit)
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print("\n".join(render(report)))
    return 1 if report["pinned"]["different_from_discovery_off"] else 0


if __name__ == "__main__":
    sys.exit(main())
