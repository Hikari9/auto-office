"""#1168: a bounded S canary prefers the user-allowed Hydrogen route, then reverts to ordinary scoring."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from office import config as cfg, db, dispatch, hydrogen_trials as h, route_probe, routing, state  # noqa: E402
from office.util import now_iso  # noqa: E402

SPEC = "agy/gemini-4-hydrogen@medium"
RISK = {"size_class": "S", "blast_radius": "repo", "irreversible": False}
DESC = {"task_size": "S", "bounds": "bounded", "domain": "docs"}
PINNED_ON = {"routing": {"hydrogen_trials": {"enabled": True}}}


def _run(rid="R1"):
    return {"id": rid, "gates": {"code_review": True}, "risk": RISK, "repo_root": None}


def _task(tid="T1", size="S"):
    return {"id": tid, "descriptor": {**DESC, "task_size": size}}


def _live(monkeypatch, budget):
    trials = {"enabled": True, **({"canary_tasks": budget} if budget is not None else {})}
    monkeypatch.setattr(cfg, "resolve", lambda root, *a, **k: ({"routing": {"hydrogen_trials": trials}}, []))


def _book(con, run_id, n, *, canary=1):
    with db.transaction(con):
        for i in range(n):
            con.execute("INSERT INTO hydrogen_trial_opportunities(id, run_id, task_id, selected_route, experimental, "
                        "created_at, canary) VALUES (?,?,?,?,?,?,?)",
                        (f"HO{run_id}{i}", run_id if n == 1 else f"{run_id}-{i}", "T", SPEC, 1, now_iso(), canary))


@pytest.mark.parametrize("value,ok", [(0, True), (10, True), (5, True), (11, False), (-1, False),
                                      (True, False), ("10", False), (2.0, False)])
def test_validate_bounds_canary_tasks(value, ok):
    problems = h.validate({"routing": {"hydrogen_trials": {"enabled": True, "canary_tasks": value}}})
    assert (problems == []) is ok, problems
    assert h.settings({"routing": {"hydrogen_trials": {"canary_tasks": value}}})["canary_tasks"] == (value if ok else 0)


def test_validate_still_rejects_other_keys():
    assert h.validate({"routing": {"hydrogen_trials": {"enabled": True, "cap": 5}}})


def test_live_budget_reaches_a_run_pinned_without_it(tmp_path, monkeypatch):
    """A run pinned before canary_tasks existed sees the live global budget."""
    _live(monkeypatch, 10)
    assert h.live_budget(_run()) == 10
    with db.connect(tmp_path / "runs.db") as con:
        assert h.canary_active(con, PINNED_ON, _run(), _task(), h.live_budget(_run()))


def test_pinned_disabled_blocks_the_canary_whatever_the_live_config(tmp_path, monkeypatch):
    _live(monkeypatch, 10)
    with db.connect(tmp_path / "runs.db") as con:
        for pinned in ({}, {"routing": {"hydrogen_trials": {"enabled": False, "canary_tasks": 10}}}):
            assert not h.canary_active(con, pinned, _run(), _task(), h.live_budget(_run()))


def test_invalid_or_unreadable_live_config_means_no_budget(monkeypatch):
    _live(monkeypatch, 99)
    assert h.live_budget(_run()) == 0

    def boom(*a, **k):
        raise ValueError("unreadable")
    monkeypatch.setattr(cfg, "resolve", boom)
    assert h.live_budget(_run()) == 0


@pytest.mark.parametrize("size", ["XS", "M", "L"])
def test_non_s_tasks_are_unaffected(tmp_path, size):
    with db.connect(tmp_path / "runs.db") as con:
        assert not h.canary_active(con, PINNED_ON, {**_run(), "risk": {**RISK, "size_class": size}},
                                   _task(size=size), 10)


def test_unsafe_run_gets_no_canary(tmp_path):
    with db.connect(tmp_path / "runs.db") as con:
        assert not h.canary_active(con, PINNED_ON, {**_run(), "gates": {"code_review": False}}, _task(), 10)
        assert not h.canary_active(con, PINNED_ON, {**_run(), "risk": {**RISK, "irreversible": True}}, _task(), 10)


def test_auto_reverts_at_budget(tmp_path):
    with db.connect(tmp_path / "runs.db") as con:
        _book(con, "old", 3)
        assert h.canary_spent(con) == 3
        assert h.canary_active(con, PINNED_ON, _run(), _task(), 4)
        assert not h.canary_active(con, PINNED_ON, _run(), _task(), 3)
        assert h.canary_progress(con, 3) == {"budget": 3, "spent": 3, "remaining": 0, "complete": True}


def test_one_canary_per_run(tmp_path):
    with db.connect(tmp_path / "runs.db") as con:
        _book(con, "R1", 1)
        assert not h.canary_active(con, PINNED_ON, _run("R1"), _task(), 10)
        assert h.canary_active(con, PINNED_ON, _run("R2"), _task(), 10)


def test_ordinary_rows_do_not_spend_the_budget(tmp_path):
    with db.connect(tmp_path / "runs.db") as con:
        _book(con, "x", 4, canary=None)
        assert h.canary_spent(con) == 0


# ------------------------------------------------------------------ dispatch booking

def _decision(canary=True):
    cand = {**route_probe.candidate_from_spec(SPEC), "hydrogen_user_allow": True}
    return {"status": "selected", "selected": SPEC, "candidate": cand,
            **({"hydrogen_canary": {"budget": 10, "spent": 0}} if canary else {})}


def _settle(con, monkeypatch, run, decision, budget):
    monkeypatch.setattr(state, "pinned_config", lambda r: PINNED_ON)
    monkeypatch.setattr(h, "live_budget", lambda r: budget)
    with db.transaction(con):
        return dispatch.settle_discovery(con, run, _task(), decision, launching=True)


def test_settle_books_a_canary_row(tmp_path, monkeypatch):
    with db.connect(tmp_path / "runs.db") as con:
        settled = _settle(con, monkeypatch, _run(), _decision(), 10)
        assert settled["selected"] == SPEC
        assert settled["hydrogen_counter"]["canary"] is True
        row = con.execute("SELECT experimental, canary FROM hydrogen_trial_opportunities").fetchone()
        assert tuple(row) == (1, 1)


def test_lost_race_still_launches_but_is_not_a_canary(tmp_path, monkeypatch):
    with db.connect(tmp_path / "runs.db") as con:
        _book(con, "other", 2)
        settled = _settle(con, monkeypatch, _run(), _decision(), 2)
        assert settled["selected"] == SPEC
        assert settled["hydrogen_counter"] == {**settled["hydrogen_counter"], "canary": False,
                                               "blocked": "canary-complete"}
        assert h.canary_spent(con) == 2
        assert con.execute("SELECT canary FROM hydrogen_trial_opportunities WHERE run_id='R1'").fetchone()[0] == 0


def test_second_canary_in_a_run_is_not_counted(tmp_path, monkeypatch):
    with db.connect(tmp_path / "runs.db") as con:
        _book(con, "R1", 1)
        settled = _settle(con, monkeypatch, _run("R1"), _decision(), 10)
        assert settled["hydrogen_counter"]["blocked"] == "one-canary-per-run"
        assert h.canary_spent(con) == 1


def test_booking_requires_a_transaction(tmp_path):
    with db.connect(tmp_path / "runs.db") as con:
        with pytest.raises(RuntimeError):
            h.book_canary(con, "R1", "T1", SPEC, 10)


# ------------------------------------------------------------------ routing preference

def _cand(spec):
    return {**route_probe.candidate_from_spec(spec), "harness_version": "1.2.3", "capabilities": ["code"],
            "dispatchable": True, "route_status": "available", "quota": {"status": "ok"}}


def _seed_probe(con, cand):
    adapter = route_probe._adapter(cand)
    fp = route_probe.fingerprint(cand, adapter)
    con.execute(
        "INSERT OR REPLACE INTO route_probes(key, harness, harness_version, adapter_hash, profile, "
        "invocation_model_id, effort, result, reason_class, detail, probed_at, run_id, dispatch_id, attempt_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (route_probe.key(cand, adapter), fp["harness"], fp["harness_version"], fp["adapter_hash"], fp["profile"],
         fp["invocation_model_id"], fp["effort"], "pass", None, "seeded", now_iso(), "r", None, "a"))
    con.commit()


OTHER = "claude/claude-sonnet-5-5@medium"


def _route(tmp_path, canary):
    hydro, other = _cand(SPEC), _cand(OTHER)
    db_path = tmp_path / "runs.db"
    with db.connect(db_path) as con:
        _seed_probe(con, hydro)
    policy = {"denied": [], "role_rules": [{"role": "executor", "route": r, "state": "allow"} for r in (SPEC, OTHER)],
              "strict_roles": [], "ceilings": {}, "ceiling_sources": {}, "overkill": [],
              "sources": {"role_rules": "user"}}
    req = {"role": "executor", "run_gear": "S", "size_class": "S", "runs_db": str(db_path),
           "candidates": [other, hydro], "user_policies": [policy], "allow_free_models": True,
           # The size slate anchors Sonnet ahead, so without the canary Hydrogen is not the pick.
           "preferred_seed": [{"model_id": "claude-sonnet-5-5", "effort": "medium", "harness": "claude"}],
           "slate_anchor": True}
    if canary:
        req["hydrogen_canary"] = canary
    return routing.route(req), routing.candidate_id(hydro), routing.candidate_id(other)


def test_canary_puts_the_user_allowed_hydrogen_route_first(tmp_path):
    decision, hydro, other = _route(tmp_path, {"budget": 10, "spent": 3, "remaining": 7, "complete": False})
    assert decision["status"] == "selected", decision
    assert decision["selected"] == hydro
    assert decision["slate"][0]["reason"] == "hydrogen canary 4/10"
    assert decision["hydrogen_canary"]["route"] == hydro
    assert "hydrogen canary 4/10" in decision["selection_disclosure"]["reason"]
    assert other in [e["route"] for e in decision["slate"]]


def test_without_canary_ranking_is_unchanged(tmp_path):
    decision, hydro, _ = _route(tmp_path, None)
    assert decision["status"] == "selected", decision
    assert "hydrogen_canary" not in decision
    assert decision["selected"] != hydro
    assert not any("hydrogen canary" in e["reason"] for e in decision["slate"])


def test_agy_maps_xhigh():
    import yaml
    seed = yaml.safe_load((ROOT / "adapters" / "seed" / "agy.yaml").read_text())
    assert seed["effort_mapping"]["xhigh"] == "xhigh"
    assert seed["effort_mapping"]["max"] is None


# ------------------------------------------------------------------ trial path ignores canary rows

def _trial_rows(con, n, *, experimental=0):
    with db.transaction(con):
        for i in range(n):
            con.execute("INSERT INTO hydrogen_trial_opportunities(id, run_id, task_id, selected_route, experimental, "
                        "created_at) VALUES (?,?,?,?,?,?)", (f"HT{i}", f"trial-{i}", "T", "x", experimental, now_iso()))


def _canary_rows(con, run_id, n):
    with db.transaction(con):
        for i in range(n):
            h.book_canary(con, f"{run_id}-{i}" if n > 1 else run_id, "T", SPEC, 99 if i % 2 == 0 else 0)


@pytest.mark.parametrize("trials,experimental", [(0, 0), (5, 0), (19, 0), (19, 1), (25, 1)])
def test_trial_snapshot_is_unchanged_by_canary_rows(tmp_path, trials, experimental):
    with db.connect(tmp_path / "a.db") as plain, db.connect(tmp_path / "b.db") as mixed:
        _trial_rows(plain, trials, experimental=experimental)
        _trial_rows(mixed, trials, experimental=experimental)
        _canary_rows(mixed, "C", 12)  # counted (canary=1) and over-budget (canary=0) rows
        assert h.snapshot(mixed) == h.snapshot(plain)


def test_canary_rows_do_not_end_the_trial_cold_start(tmp_path):
    with db.connect(tmp_path / "runs.db") as con:
        _canary_rows(con, "C", 30)
        assert h.snapshot(con)["cold_start"] is True and h.snapshot(con)["previous"] == 0


def test_a_canary_in_a_run_does_not_use_its_trial_slot(tmp_path):
    with db.connect(tmp_path / "runs.db") as con:
        _trial_rows(con, 19)
        _canary_rows(con, "R1", 1)
        with db.transaction(con):
            booked = h.book(con, "R1", "T2", SPEC, experiment=True, fallback_route="fallback")
        assert booked["experimental"] is True and booked["blocked"] is None


def test_shipped_default_defines_canary_tasks_so_config_sets_it_without_force(tmp_path, monkeypatch):
    """#1171: the shipped default must define the key, or `office config` refuses it without --force."""
    import subprocess
    import yaml
    from office import configcmd
    shipped = yaml.safe_load(cfg.default_config_path().read_text())
    assert shipped["routing"]["hydrogen_trials"] == {"enabled": False, "canary_tasks": 0}
    assert h.settings(shipped)["canary_tasks"] == 0 and h.validate(shipped) == []
    user = tmp_path / "user.yaml"
    monkeypatch.setenv("OFFICE_USER_CONFIG", str(user))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.chdir(repo)
    configcmd.config(key="routing.hydrogen_trials.canary_tasks", value="10", tier="user")
    assert yaml.safe_load(user.read_text())["routing"]["hydrogen_trials"]["canary_tasks"] == 10
