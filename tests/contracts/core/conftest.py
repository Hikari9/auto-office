"""Small synthetic inputs for runtime contracts; no mutable catalog values."""
from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from office import executor_slates, routing


@pytest.fixture
def policy_root(tmp_path):
    source = Path(executor_slates.POLICY["_provenance"]["source"]).parent
    target = tmp_path / "catalog"
    target.mkdir()
    for path in source.iterdir():
        if path.is_file():
            shutil.copy2(path, target / path.name)
    return tmp_path


@pytest.fixture
def synthetic_candidate():
    def make(model="fixture-a", effort="low", harness="fake", *, capabilities=None, quota=80,
             status="available", score=55):
        return {
            "harness": harness, "harness_version": "1.0", "model_id": model,
            "invocation_model_id": model, "effort": effort,
            "capabilities": list(capabilities if capabilities is not None else ["builder", "review"]),
            "quota": {"status": "ok", "tightest_remaining_percent": quota},
            "cost": {"money_estimate": 1, "quota_burn": 1, "wall_clock_seconds": 1},
            "route_status": status,
        }
    return make


@pytest.fixture
def route_contract():
    def run(candidates, *, role="code_reviewer", policies=(), seed=None, required=None,
            context=None, extra=None):
        request = {
            "role": role, "candidates": candidates, "preferred_seed": seed,
            "user_policies": list(policies), "runs_db": None,
            "policy": {"floor": None, "required_capabilities": list(["review"] if required is None else required)},
            "context": {"gear": "M", "size_class": "M"} if context is None else context,
        }
        request.update(extra or {})
        return routing.route(request)
    return run


@pytest.fixture
def user_policy():
    def make(*rules):
        return {"role_rules": list(rules), "denied": [], "overkill": [], "ceilings": {},
                "sources": {"role_rules": "user"}}
    return make


def rule(role, route, state, **fields):
    return {"role": role, "route": route, "state": state, **fields}
