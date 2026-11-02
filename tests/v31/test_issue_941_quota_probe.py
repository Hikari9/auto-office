"""#941: a quota probe timeout retries once after a backoff, then falls back to the last good
reading within a TTL. The reading is marked stale, and the route receipt records it. Fake probe only."""
from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from office import candidates, dispatch

ADAPTER = {"id": "claude", "quota_probe": {"command": ["probe"]}}
TIMEOUT = subprocess.TimeoutExpired(["probe"], 20)


def _ok(percent):
    return SimpleNamespace(returncode=0, stdout=json.dumps({"tightest_remaining_percent": percent}))


class FakeProbe:
    """Stands in for subprocess.run: each call pops the next scripted outcome."""

    def __init__(self, monkeypatch, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        monkeypatch.setattr(candidates.subprocess, "run", self)

    def __call__(self, *args, **kwargs):
        self.calls += 1
        out = self.outcomes.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out


@pytest.fixture(autouse=True)
def isolated_quota(tmp_path, monkeypatch):
    candidates._QUOTA_CACHE.clear()
    monkeypatch.setattr(candidates.paths, "state_home", lambda: tmp_path)
    monkeypatch.setattr("office.gates.host_overloaded", lambda: None)
    monkeypatch.setattr(candidates.time, "sleep", lambda seconds: None)
    for key in ("OFFICE_QUOTA_FIXTURE", "OFFICE_QUOTA_CACHE_TTL", "OFFICE_QUOTA_LAST_GOOD_TTL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OFFICE_QUOTA_PROBE", "on")


def _good_reading_then_expire_fresh_cache(monkeypatch, percent=60):
    """Store a good reading, then make the fresh shared entry expire so the next call probes again."""
    FakeProbe(monkeypatch, [_ok(percent)])
    assert candidates.probe_quota(ADAPTER)["status"] == "ok"
    monkeypatch.setenv("OFFICE_QUOTA_CACHE_TTL", "0")
    candidates._QUOTA_CACHE.clear()


def _age_last_good(seconds):
    path = candidates._shared_cache_path()
    data = json.loads(path.read_text())
    data["claude"]["last_good"]["at"] -= seconds
    path.write_text(json.dumps(data))


def test_timeout_on_a_quiet_host_retries_once_after_backoff(monkeypatch):
    fake = FakeProbe(monkeypatch, [TIMEOUT, TIMEOUT])
    sleeps = []
    monkeypatch.setattr(candidates.time, "sleep", sleeps.append)

    result = candidates.probe_quota(ADAPTER)

    assert fake.calls == 2
    assert sleeps == [candidates.PROBE_RETRY_BACKOFF_S]
    assert result["status"] == "unknown" and result["tightest_remaining_percent"] is None
    assert result["cause"] == "command timed out after 20 seconds"


def test_a_retry_that_succeeds_is_used_without_fallback(monkeypatch):
    fake = FakeProbe(monkeypatch, [TIMEOUT, _ok(55)])

    result = candidates.probe_quota(ADAPTER)

    assert fake.calls == 2
    assert result == {"status": "ok", "tightest_remaining_percent": 55.0}


def test_timeout_after_a_good_reading_uses_it_within_the_ttl(monkeypatch):
    _good_reading_then_expire_fresh_cache(monkeypatch, percent=60)
    FakeProbe(monkeypatch, [TIMEOUT, TIMEOUT])

    result = candidates.probe_quota(ADAPTER)

    assert result["status"] == "ok" and result["tightest_remaining_percent"] == 60.0
    assert result["stale"] is True and result["stale_age_s"] >= 0
    assert result["cause"].startswith("probe timed out; last good reading")


def test_a_later_process_inside_the_failure_ttl_still_falls_back_without_probing(monkeypatch):
    _good_reading_then_expire_fresh_cache(monkeypatch, percent=60)
    FakeProbe(monkeypatch, [TIMEOUT, TIMEOUT])
    assert candidates.probe_quota(ADAPTER)["stale"] is True  # records the timed-out failure

    monkeypatch.delenv("OFFICE_QUOTA_CACHE_TTL")  # a new office process with the default TTLs
    candidates._QUOTA_CACHE.clear()
    fake = FakeProbe(monkeypatch, [TIMEOUT, TIMEOUT])

    result = candidates.probe_quota(ADAPTER)

    assert fake.calls == 0  # the failed entry is still fresh: no second 20s probe
    assert result["status"] == "ok" and result["stale"] is True


def test_last_good_reading_older_than_the_ttl_is_not_used(monkeypatch):
    _good_reading_then_expire_fresh_cache(monkeypatch, percent=60)
    _age_last_good(seconds=candidates._last_good_ttl() + 1)
    FakeProbe(monkeypatch, [TIMEOUT, TIMEOUT])

    result = candidates.probe_quota(ADAPTER)

    assert result["status"] == "unknown" and "stale" not in result
    assert result["cause"] == "command timed out after 20 seconds"


def test_zero_last_good_ttl_disables_the_fallback(monkeypatch):
    _good_reading_then_expire_fresh_cache(monkeypatch, percent=60)
    monkeypatch.setenv("OFFICE_QUOTA_LAST_GOOD_TTL", "0")
    FakeProbe(monkeypatch, [TIMEOUT, TIMEOUT])

    result = candidates.probe_quota(ADAPTER)

    assert result["status"] == "unknown" and "stale" not in result


def test_a_non_timeout_failure_never_uses_the_last_good_reading(monkeypatch):
    _good_reading_then_expire_fresh_cache(monkeypatch, percent=60)
    FakeProbe(monkeypatch, [SimpleNamespace(returncode=7, stdout="")])

    result = candidates.probe_quota(ADAPTER)

    assert result["status"] == "unknown" and "stale" not in result
    assert result["cause"] == "command exited with code 7"


def test_a_disabled_probe_never_uses_the_last_good_reading(monkeypatch):
    FakeProbe(monkeypatch, [_ok(60)])
    candidates.probe_quota(ADAPTER)
    monkeypatch.setenv("OFFICE_QUOTA_PROBE", "off")
    candidates._QUOTA_CACHE.clear()

    result = candidates.probe_quota(ADAPTER)

    assert result == {"status": "unknown", "tightest_remaining_percent": None, "cause": "probe disabled"}


def test_a_route_chosen_on_a_stale_reading_is_recorded_as_stale(monkeypatch):
    _good_reading_then_expire_fresh_cache(monkeypatch, percent=60)
    FakeProbe(monkeypatch, [TIMEOUT, TIMEOUT])
    quota = candidates.probe_quota(ADAPTER)

    record = candidates.quota_unknown_record({"adapter_id": "claude", "quota": quota})

    assert record == {"harness": "claude", "status": "stale", "cause": quota["cause"]}
    payload = dispatch._route_payload({"candidate": {"adapter_id": "claude", "quota": quota}})
    assert payload["quota_unknown"] == record


def test_a_fresh_reading_is_not_recorded_as_unknown(monkeypatch):
    FakeProbe(monkeypatch, [_ok(60)])
    quota = candidates.probe_quota(ADAPTER)

    assert candidates.quota_unknown_record({"adapter_id": "claude", "quota": quota}) is None
