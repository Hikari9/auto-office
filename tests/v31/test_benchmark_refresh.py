"""Opt-in, in-session benchmark refresh (#229)."""
from __future__ import annotations

import json

import yaml

from conftest import PLAN_ONE, approved_run


def _run(env):
    con = env.con()
    from office import state
    return state.get_run(con, con.execute("SELECT id FROM runs").fetchone()[0])


def _start(env, *extra):
    env.trust()
    code, out = env.office("start", "fixture", "--gear", "direct+review", "--planner", "inline", *extra)
    assert code == 0, out
    return _run(env)


def _open_row(monkeypatch):
    """Make one dispatchable catalog row unscored, as a newly listed model would be."""
    from office import benchmarks, candidates
    index = benchmarks.index_version()
    rows = candidates.catalog_rows()
    target = next(r for r in rows if r.get("dispatchable") is not False and not r.get("alias_family")
                  and (r.get("benchmark_indexes") or {}).get(index) is not None)
    blank = dict(target, benchmark_indexes={})
    monkeypatch.setattr(candidates, "catalog_rows", lambda: [blank if r is target else r for r in rows])
    return blank, index


def _delta(row, index, **over):
    body = {"index_version": index, "source": "https://artificialanalysis.ai/", "fetched_at": "2026-10-02T10:00:00Z",
            "rows": [{"harness": row["invocation_harness"], "model_id": row["model_id"], "effort": row["effort"],
                      "score": 44.5, "url": f"https://artificialanalysis.ai/models/{row['model_id']}"}]}
    body.update(over)
    return body


def test_choice_is_off_by_default_and_brief_is_the_explicit_opt_in(env, monkeypatch):
    _open_row(monkeypatch)
    run = _start(env)
    assert run["benchmark_refresh"]["enabled"] is False
    code, out = env.office("benchmarks", "submit", "/nonexistent.yaml")
    assert code == 4 and "benchmark-refresh-off" in out, out
    code, out = env.office("benchmarks", "brief")
    assert code == 0 and "brief:" in out, out
    assert _run(env)["benchmark_refresh"]["enabled"] is True


def test_refresh_is_bounded_validated_and_bound_to_later_routes(env, monkeypatch):
    row, index = _open_row(monkeypatch)
    run = _start(env, "--benchmark-refresh")
    assert run["benchmark_refresh"] == {"enabled": True, "max": 1, "used": 0}
    code, out = env.office("benchmarks", "brief")
    assert code == 0 and "brief:" in out, out
    brief = out.split("brief: ", 1)[1].split()[0]
    text = open(brief).read()
    assert row["model_id"] in text and "Never infer" in text
    code, out = env.office("benchmarks", "brief")
    assert code == 4 and "benchmark-refresh-spent" in out, out  # one refresh per run

    from office import benchmarks, candidates
    bad = env.tmp / "bad.yaml"
    bad.write_text(yaml.safe_dump(_delta(row, "Artificial Analysis Intelligence Index v9")))
    code, out = env.office("benchmarks", "submit", str(bad))
    assert code == 4 and "not comparable" in out, out
    assert _run(env)["benchmark_refresh"].get("snapshot") is None

    before = candidates.route_role(env.con(), _run(env)["policy"], _run(env), "executor", probe=False)
    good = env.tmp / "good.yaml"
    good.write_text(yaml.safe_dump(_delta(row, index)))
    code, out = env.office("benchmarks", "submit", str(good))
    assert code == 0 and "accepted" in out, out
    run = _run(env)
    snap = run["benchmark_refresh"]["snapshot"]
    after = candidates.route_role(env.con(), run["policy"], run, "executor", probe=False)
    assert before["benchmark_snapshot"] == run["catalog_hash"] and after["benchmark_snapshot"] == snap
    scored = [c for c in after["request"]["candidates"] if c["model_id"] == row["model_id"] and c["effort"] == row["effort"]]
    assert all(c["benchmark_indexes"][index] == 44.5 for c in scored)


def test_delta_never_replaces_a_trusted_score_or_names_unknown_rows(env, monkeypatch):
    row, index = _open_row(monkeypatch)
    from office import benchmarks, candidates
    trusted = next(r for r in candidates.catalog_rows() if (r.get("benchmark_indexes") or {}).get(index) is not None
                   and not r.get("alias_family"))
    delta = _delta(row, index)
    delta["rows"].append({"harness": trusted["invocation_harness"], "model_id": trusted["model_id"],
                          "effort": trusted["effort"], "score": 99, "url": "https://artificialanalysis.ai/x"})
    delta["rows"].append({"harness": "codex", "model_id": "no-such-model", "effort": "high", "score": 50,
                          "url": "https://artificialanalysis.ai/y"})
    delta["rows"].append({"harness": row["invocation_harness"], "model_id": row["model_id"], "effort": row["effort"],
                          "score": "n/a", "url": "https://artificialanalysis.ai/z"})
    _, problems = benchmarks.validate(delta)
    text = " ".join(problems)
    assert "already has a trusted score" in text and "unknown model or effort mapping" in text
    assert "is not a number" in text and "repeats" in text
    _, problems = benchmarks.validate(_delta(row, index, rows=[]))
    assert problems == ["no rows: nothing was fetched"]


def test_route_payload_keeps_the_snapshot_hash():
    from office import dispatch
    out = dispatch._route_payload({"candidate": {"harness": "codex"}, "benchmark_snapshot": "abc"})
    assert out["benchmark_snapshot"] == "abc"
