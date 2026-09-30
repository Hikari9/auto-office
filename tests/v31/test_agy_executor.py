"""agy is the preferred executor seed; its adapter must carry a worker profile and the builder capability."""
from pathlib import Path

import yaml

from office import adapters, candidates

ROOT = Path(__file__).resolve().parents[2]


def _executor_required():
    for path in sorted((ROOT / "config").glob("*.yaml")):
        roles = (yaml.safe_load(path.read_text()) or {}).get("roles") or {}
        if "executor" in roles:
            return set(roles["executor"].get("required_capabilities") or [])
    raise AssertionError("no executor role policy in config/")


def test_agy_adapter_has_worker_profile_and_builder():
    agy = adapters.load_all()["agy"]
    assert adapters.profile(agy, "worker"), "agy needs a worker launch profile"
    assert _executor_required() <= set(agy["capabilities"])


def test_agy_gemini_flash_is_an_executor_candidate(monkeypatch):
    monkeypatch.setattr(adapters, "installed", lambda a: True)
    monkeypatch.setattr(adapters, "harness_version", lambda a: "1.2.12")
    cands, skipped = candidates.build_candidates(None, "executor", probe=False)
    agy = [c for c in cands if c["harness"] == "agy" and c["model_id"] == "gemini-3.8-flash"]
    assert {c["effort"] for c in agy} >= {"medium"}, skipped
    required = _executor_required()
    assert all(required <= set(c["capabilities"]) for c in agy)
    assert not [s for s in skipped if s["candidate"].startswith("agy/") and "launch profile" in s["reason"]]
