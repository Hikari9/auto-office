"""#1002: an adapter's `quota_probe.unknown_is_unlimited: true` turns an unknown quota into full headroom,
so routing no longer sets the route aside as quota-unknown. Shipped seeds keep false. Fake probe only."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from office import candidates, routing


@pytest.fixture(autouse=True)
def isolated_quota(tmp_path, monkeypatch):
    candidates._QUOTA_CACHE.clear()
    monkeypatch.setattr(candidates.paths, "state_home", lambda: tmp_path)
    monkeypatch.setattr("office.gates.host_overloaded", lambda: None)
    for key in ("OFFICE_QUOTA_FIXTURE", "OFFICE_QUOTA_CACHE_TTL", "OFFICE_QUOTA_LAST_GOOD_TTL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OFFICE_QUOTA_PROBE", "on")


def _adapter(flag, command=None):
    probe = {"unknown_is_unlimited": flag}
    if command:
        probe["command"] = command
    return {"id": "opencode", "quota_probe": probe}


def test_no_probe_command_with_the_flag_reads_as_unlimited():
    quota = candidates.probe_quota(_adapter(True))
    assert quota["status"] == "ok" and quota["tightest_remaining_percent"] == 100.0 and quota["unlimited"]
    assert "unknown_is_unlimited" in quota["cause"] and "no command configured" in quota["cause"]
    # Routing keeps it among the safe routes, and the dispatch flags no unknown quota.
    assert routing._quota_gate({"quota": quota}, 5) is None
    assert candidates.quota_unknown_record({"adapter_id": "opencode", "quota": quota}) is None


def test_without_the_flag_quota_stays_unknown():
    for flag in (False, None, "true"):
        quota = candidates.probe_quota(_adapter(flag))
        assert quota["status"] == "unknown" and quota["tightest_remaining_percent"] is None
        candidates._QUOTA_CACHE.clear()


def test_a_probe_that_cannot_tell_reads_as_unlimited_but_a_real_reading_is_kept(monkeypatch):
    replies = [SimpleNamespace(returncode=0, stdout=json.dumps({"tightest_remaining_percent": None})),
               SimpleNamespace(returncode=0, stdout=json.dumps({"tightest_remaining_percent": 12}))]
    monkeypatch.setattr(candidates.subprocess, "run", lambda *a, **k: replies.pop(0))
    monkeypatch.setenv("OFFICE_QUOTA_CACHE_TTL", "0")
    assert candidates.probe_quota(_adapter(True, ["probe"]))["unlimited"] is True
    candidates._QUOTA_CACHE.clear()
    real = candidates.probe_quota(_adapter(True, ["probe"]))
    assert real["status"] == "ok" and real["tightest_remaining_percent"] == 12 and "unlimited" not in real


def test_an_unavailable_probe_is_not_turned_into_headroom(monkeypatch):
    monkeypatch.setattr(candidates, "_probe_quota", lambda adapter: {
        "status": "unavailable", "tightest_remaining_percent": None, "cause": "host overloaded"})
    assert candidates.probe_quota(_adapter(True))["status"] == "unavailable"


def test_shipped_seeds_keep_the_flag_false():
    import yaml
    seeds = sorted((Path(__file__).resolve().parents[2] / "adapters" / "seed").glob("*.yaml"))
    assert seeds
    for seed in seeds:
        flag = ((yaml.safe_load(seed.read_text()) or {}).get("quota_probe") or {}).get("unknown_is_unlimited")
        assert flag in (False, None), seed.name
