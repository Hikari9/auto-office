"""Regenerate the planted-bug diffs from a clean checkout of the pinned base.

Each case edits real Office code in the working tree, captures `git diff`, then restores
the file. Run from the repository root at the base commit in planted/BASE.
"""
from __future__ import annotations

import pathlib
import subprocess

GIT = "/usr/bin/git"
OUT = pathlib.Path("research/hydrogen-shadow/planted")


def edit(path: str, old: str, new: str) -> None:
    p = pathlib.Path(path)
    s = p.read_text()
    if s.count(old) != 1:
        raise SystemExit(f"{path}: anchor not unique: {old[:60]!r}")
    p.write_text(s.replace(old, new))


def capture(case: str, files: list[str]) -> None:
    diff = subprocess.run([GIT, "diff", "--", *files], capture_output=True, text=True, check=True).stdout
    (OUT / case).mkdir(parents=True, exist_ok=True)
    (OUT / case / "change.diff").write_text(diff)
    subprocess.run([GIT, "checkout", "--", *files], check=True)


def main() -> None:
    # p1 logic: claim_liveness reuses pid_alive, which folds an unprobeable pid into DEAD.
    edit("src/office/util.py", '''    state = pid_state(pid)
    if state == DEAD:
        return DEAD, f"pid {pid} no longer exists"
    if state == UNKNOWN:
        return UNKNOWN, f"pid {pid} could not be probed"
    start = _claim_start(claimed_by)''', '''    if not pid_alive(pid):
        return DEAD, f"pid {pid} no longer exists"
    start = _claim_start(claimed_by)''')
    capture("p1-logic", ["src/office/util.py"])

    # p2 auth: user_allowed no longer requires an exact route spec, so a wildcard allow admits Hydrogen.
    edit("src/office/hydrogen_trials.py", '''    `rule` is route_policy.effective_role_state for this candidate, role and size, so size scope
    is already applied. A wildcard route, a shipped/seed/override source or a non-allow never counts.
    """
    from office import route_policy
    rule = rule or {}
    return (rule.get("state") == "allow" and rule.get("source") == "user"
            and route_policy._is_exact_spec(rule.get("rule")))''', '''    `rule` is route_policy.effective_role_state for this candidate, role and size, so size scope
    is already applied, and the rule it returns already matched this candidate's route.
    A shipped/seed/override source or a non-allow never counts.
    """
    rule = rule or {}
    return rule.get("state") == "allow" and rule.get("source") == "user"''')
    capture("p2-auth", ["src/office/hydrogen_trials.py"])

    # p3 off-by-one: the rolling window no longer counts the pending decision.
    edit("src/office/route_probe.py", '''    used = con.execute(query, args).fetchone()[0]
    window = min(ROLLING_WINDOW, len(rows) + 1)''', '''    used = con.execute(query, args).fetchone()[0]
    # The window is the decisions actually on record, capped at the rolling size.
    window = min(ROLLING_WINDOW, len(rows))''')
    capture("p3-off-by-one", ["src/office/route_probe.py"])

    # p4 missing test: a correct env override for the kick throttle, with no test.
    edit("src/office/jobs.py", "import json\n", "import json\nimport math\n")
    edit("src/office/jobs.py", "KICK_THROTTLE_SECONDS = 10\n",
         'KICK_THROTTLE_SECONDS = 10\nKICK_THROTTLE_ENV = "OFFICE_KICK_THROTTLE_SECONDS"\n')
    edit("src/office/jobs.py", '''def mode() -> str:
    return os.environ.get("OFFICE_JOBS", "spawn")
''', '''def mode() -> str:
    return os.environ.get("OFFICE_JOBS", "spawn")


def kick_throttle_seconds() -> float:
    """Seconds before a queued job may be kicked again. `OFFICE_KICK_THROTTLE_SECONDS`
    overrides the default; a value that is not a finite, non-negative number is ignored."""
    raw = os.environ.get(KICK_THROTTLE_ENV)
    if raw is None:
        return float(KICK_THROTTLE_SECONDS)
    try:
        value = float(raw)
    except ValueError:
        return float(KICK_THROTTLE_SECONDS)
    return value if math.isfinite(value) and value >= 0 else float(KICK_THROTTLE_SECONDS)
''')
    edit("src/office/jobs.py", "    cutoff = (now - timedelta(seconds=KICK_THROTTLE_SECONDS)).isoformat()",
         "    cutoff = (now - timedelta(seconds=kick_throttle_seconds())).isoformat()")
    capture("p4-missing-test", ["src/office/jobs.py"])

    # p5 race: book() reads the caps outside any writer transaction when the caller holds none.
    edit("src/office/hydrogen_trials.py", '''    """Caller MUST already hold the same SQLite writer tx that will create the dispatch.

    Records all eligible opportunities, including non-trials, so a failed trial
    cannot vanish from the 20-entry denominator. No updates/deletes of prior rows.
    """
    if not con.in_transaction:
        raise RuntimeError("Hydrogen reservation requires a BEGIN IMMEDIATE dispatch transaction")
    before = snapshot(con)''', '''    """Book one eligible opportunity. The dispatch path calls this inside its writer tx;
    the reroute path has none, so the insert then opens its own BEGIN IMMEDIATE.

    Records all eligible opportunities, including non-trials, so a failed trial
    cannot vanish from the 20-entry denominator. No updates/deletes of prior rows.
    """
    before = snapshot(con)''')
    edit("src/office/hydrogen_trials.py", '''    event_id = "HO" + uuid.uuid4().hex
    con.execute("INSERT INTO hydrogen_trial_opportunities(id, run_id, task_id, selected_route, experimental, "
                "created_at) VALUES (?,?,?,?,?,?)", (event_id, run_id, task_id, selected_route,
                                                      int(experiment), now_iso()))''', '''    event_id = "HO" + uuid.uuid4().hex
    row = (event_id, run_id, task_id, selected_route, int(experiment), now_iso())
    sql = ("INSERT INTO hydrogen_trial_opportunities(id, run_id, task_id, selected_route, experimental, "
           "created_at) VALUES (?,?,?,?,?,?)")
    if con.in_transaction:
        con.execute(sql, row)
    else:
        from office import db
        with db.transaction(con):
            con.execute(sql, row)''')
    capture("p5-race", ["src/office/hydrogen_trials.py"])


if __name__ == "__main__":
    main()
