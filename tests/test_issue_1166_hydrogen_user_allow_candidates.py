"""Candidate admission must honor an exact, size-scoped Hydrogen user allow."""
import pytest

from office import adapters, candidates, db, route_policy, route_probe, routing
from office.util import now_iso

MODEL = "gemini-4-hydrogen"
SPEC = f"agy@1/{MODEL}@medium"


@pytest.fixture
def setup_route(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OFFICE_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("OFFICE_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(candidates, "required_user_policies", lambda *_: [])
    adapter = {"id": "agy", "capabilities": ["code"]}
    monkeypatch.setattr(adapters, "load_sources", lambda: {"agy": (adapter, None)})
    monkeypatch.setattr(adapters, "load_all", lambda: {"agy": adapter})
    monkeypatch.setattr(adapters, "installed", lambda _: True)
    monkeypatch.setattr(adapters, "route_version", lambda *args: "1.2.3")
    monkeypatch.setattr(adapters, "profile", lambda *args: {"command": "fake"})
    row = {"invocation_harness": "agy", "model_id": MODEL, "effort": "medium", "dispatchable": False}
    monkeypatch.setattr(candidates, "catalog_rows", lambda: [row])
    return row, adapter


def config(route=SPEC, role="executor", sizes=None, **extra):
    rule = {"role": role, "route": route, "state": "allow", **extra}
    if sizes is not None:
        rule["sizes"] = sizes
    return {"routing": {"user_policy": {"role_rules": [rule]}}}


def test_route_role_selects_exact_sized_user_allow(setup_route, tmp_path, monkeypatch):
    _, adapter = setup_route
    cfg = config(sizes=["S"])
    # Routing reads the user's live policy, not the run's pinned copy (#1120).
    monkeypatch.setattr(candidates, "required_user_policies", lambda *_: [route_policy.user_policy(cfg)])
    with db.connect(tmp_path / "runs.db") as con:
        built, _ = candidates.build_candidates(con, "executor", probe=False,
                                                allow_policies=[route_policy.user_policy(cfg)], task_size="S")
        assert len(built) == 1
        cand = built[0]
        fp = route_probe.fingerprint(cand, adapter)
        con.execute("INSERT INTO route_probes(key, harness, harness_version, adapter_hash, profile, "
                    "invocation_model_id, effort, result, probed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (route_probe.key(cand, adapter), fp["harness"], fp["harness_version"], fp["adapter_hash"],
                     fp["profile"], fp["invocation_model_id"], fp["effort"], "pass", now_iso()))
        con.commit()
        decision = candidates.route_role(con, cfg, {"gear": "S", "risk": {"size_class": "S"}}, "executor",
                                         probe=False, observe=True)
    assert decision["selected"] == routing.candidate_id(cand), decision
    assert not [r for r in decision["rejected"] if MODEL in str(r) and
                (r.get("category") == "hydrogen-safety" or "floor" in str(r).lower())]


@pytest.mark.parametrize("route,role,size,effort,extra", [
    (f"agy/{MODEL}", "executor", "S", "medium", {"wildcard": True}),
    (f"agy@1/{MODEL}@high", "executor", "S", "high", {}),
    (SPEC, "executor", "M", "medium", {}),
    (SPEC, "code_reviewer", "S", "medium", {}),
])
def test_other_allows_do_not_admit_hydrogen(setup_route, route, role, size, effort, extra):
    row, _ = setup_route
    row["effort"] = effort
    cfg = config(route, role=role, sizes=["S"], **extra)
    built, _ = candidates.build_candidates(None, role, probe=False,
                                           allow_policies=[route_policy.user_policy(cfg)], task_size=size)
    assert not built
