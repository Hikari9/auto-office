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
        self.kwargs = {}
        monkeypatch.setattr(candidates.subprocess, "run", self)

    def __call__(self, *args, **kwargs):
        self.calls += 1
        self.kwargs = kwargs
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
    assert "timed_out" not in result


def test_a_retry_that_succeeds_is_used_without_fallback(monkeypatch):
    _good_reading_then_expire_fresh_cache(monkeypatch, percent=40)
    fake = FakeProbe(monkeypatch, [TIMEOUT, _ok(55)])

    result = candidates.probe_quota(ADAPTER)

    assert fake.calls == 2
    assert result == {"status": "ok", "tightest_remaining_percent": 55.0}


def test_timeout_after_a_good_reading_uses_it_within_the_ttl(monkeypatch):
    _good_reading_then_expire_fresh_cache(monkeypatch, percent=60)
    _age_last_good(seconds=120)
    FakeProbe(monkeypatch, [TIMEOUT, TIMEOUT])

    result = candidates.probe_quota(ADAPTER)

    assert result == {"status": "ok", "tightest_remaining_percent": 60.0, "stale": True,
                      "stale_age_s": 120, "cause": "probe timed out; last good reading 120s old"}


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


def test_loaded_host_timeout_twice_without_a_good_reading_is_unavailable_with_the_load(monkeypatch):
    monkeypatch.setattr("office.gates.host_overloaded", lambda: "host load 40 exceeds 16 (8 CPUs)")
    FakeProbe(monkeypatch, [TIMEOUT, TIMEOUT])

    result = candidates.probe_quota(ADAPTER)

    assert result["status"] == "unavailable" and result["tightest_remaining_percent"] is None
    assert result["cause"] == "command timed out twice after 20 seconds; host load 40 exceeds 16 (8 CPUs)"
    assert "stale" not in result and "timed_out" not in result


def test_loaded_host_timeout_uses_the_last_good_reading(monkeypatch):
    _good_reading_then_expire_fresh_cache(monkeypatch, percent=60)
    monkeypatch.setattr("office.gates.host_overloaded", lambda: "host load 40 exceeds 16 (8 CPUs)")
    FakeProbe(monkeypatch, [TIMEOUT, TIMEOUT])

    result = candidates.probe_quota(ADAPTER)

    assert result["status"] == "ok" and result["tightest_remaining_percent"] == 60.0 and result["stale"] is True


@pytest.mark.parametrize(("age", "stale"), [(99, True), (100, False)])
def test_last_good_ttl_boundary_is_exclusive(monkeypatch, age, stale):
    now = 1_000_000.0
    monkeypatch.setattr(candidates.time, "time", lambda: now)
    monkeypatch.setenv("OFFICE_QUOTA_LAST_GOOD_TTL", "100")
    monkeypatch.setenv("OFFICE_QUOTA_CACHE_TTL", "0")
    FakeProbe(monkeypatch, [_ok(60)])
    candidates.probe_quota(ADAPTER)  # stores the good reading at `now`
    monkeypatch.setattr(candidates.time, "time", lambda: now + age)
    candidates._QUOTA_CACHE.clear()
    FakeProbe(monkeypatch, [TIMEOUT, TIMEOUT])

    result = candidates.probe_quota(ADAPTER)

    assert (result["status"] == "ok") is stale and result.get("stale", False) is stale


@pytest.mark.parametrize("corrupt_at", [float("nan"), float("inf")])
def test_a_corrupt_top_level_stamp_is_not_served_as_fresh(monkeypatch, corrupt_at):
    FakeProbe(monkeypatch, [_ok(60)])
    candidates.probe_quota(ADAPTER)
    path = candidates._shared_cache_path()
    data = json.loads(path.read_text())
    data["claude"]["at"] = corrupt_at
    path.write_text(json.dumps(data))
    candidates._QUOTA_CACHE.clear()
    fake = FakeProbe(monkeypatch, [_ok(70)])

    result = candidates.probe_quota(ADAPTER)

    assert fake.calls == 1 and result == {"status": "ok", "tightest_remaining_percent": 70.0}


@pytest.mark.parametrize("junk", ["abc", "inf", "nan"])
def test_unparseable_or_non_finite_last_good_ttl_uses_the_default(monkeypatch, junk):
    _good_reading_then_expire_fresh_cache(monkeypatch, percent=60)
    monkeypatch.setenv("OFFICE_QUOTA_LAST_GOOD_TTL", junk)
    _age_last_good(seconds=5)
    FakeProbe(monkeypatch, [TIMEOUT, TIMEOUT])

    result = candidates.probe_quota(ADAPTER)

    assert result["stale"] is True and result["stale_age_s"] == 5


def test_a_non_timeout_failure_keeps_the_last_good_reading_for_a_later_timeout(monkeypatch):
    _good_reading_then_expire_fresh_cache(monkeypatch, percent=60)
    FakeProbe(monkeypatch, [SimpleNamespace(returncode=7, stdout="")])
    assert candidates.probe_quota(ADAPTER)["status"] == "unknown"
    candidates._QUOTA_CACHE.clear()
    FakeProbe(monkeypatch, [TIMEOUT, TIMEOUT])

    result = candidates.probe_quota(ADAPTER)

    assert result["status"] == "ok" and result["stale"] is True


def test_the_probe_runs_with_stdin_closed_so_a_prompt_exits_instead_of_hanging(monkeypatch):
    fake = FakeProbe(monkeypatch, [_ok(60)])

    candidates.probe_quota(ADAPTER)

    assert fake.kwargs["stdin"] is subprocess.DEVNULL


@pytest.mark.parametrize("corrupt_at", ["x", None, float("nan"), float("inf")])
def test_a_corrupt_last_good_timestamp_is_not_served(monkeypatch, corrupt_at):
    _good_reading_then_expire_fresh_cache(monkeypatch, percent=60)
    path = candidates._shared_cache_path()
    data = json.loads(path.read_text())
    data["claude"]["last_good"]["at"] = corrupt_at
    path.write_text(json.dumps(data))
    FakeProbe(monkeypatch, [TIMEOUT, TIMEOUT])

    result = candidates.probe_quota(ADAPTER)

    assert result["status"] == "unknown" and "stale" not in result


def test_a_last_good_stamp_in_the_future_is_not_served(monkeypatch):
    _good_reading_then_expire_fresh_cache(monkeypatch, percent=60)
    _age_last_good(seconds=-3600)  # the clock moved back: the reading's age is unknowable
    FakeProbe(monkeypatch, [TIMEOUT, TIMEOUT])

    result = candidates.probe_quota(ADAPTER)

    assert result["status"] == "unknown" and "stale" not in result


def test_an_invalid_stored_reading_is_not_served(monkeypatch):
    _good_reading_then_expire_fresh_cache(monkeypatch, percent=60)
    path = candidates._shared_cache_path()
    data = json.loads(path.read_text())
    data["claude"]["last_good"]["result"]["tightest_remaining_percent"] = "abc"
    path.write_text(json.dumps(data))
    FakeProbe(monkeypatch, [TIMEOUT, TIMEOUT])

    result = candidates.probe_quota(ADAPTER)

    assert result["status"] == "unknown" and "stale" not in result


def test_every_stale_agy_family_is_marked_so_a_family_gate_sees_it(monkeypatch):
    agy = {"id": "agy", "quota_probe": {"command": ["probe"]}}
    body = json.dumps({"tightest_remaining_percent": 60,
                       "families": {"claude": {"status": "ok", "tightest_remaining_percent": 40}}})
    FakeProbe(monkeypatch, [SimpleNamespace(returncode=0, stdout=body)])
    candidates.probe_quota(agy)
    monkeypatch.setenv("OFFICE_QUOTA_CACHE_TTL", "0")
    candidates._QUOTA_CACHE.clear()
    FakeProbe(monkeypatch, [TIMEOUT, TIMEOUT])

    result = candidates.probe_quota(agy)
    family = candidates.quota_for_candidate({"adapter_id": "agy", "model_id": "claude-sonnet-5-5"}, result)

    assert family["status"] == "ok" and family["tightest_remaining_percent"] == 40.0
    assert family["stale"] is True and family["cause"] == result["cause"]
    assert candidates.quota_unknown_record({"adapter_id": "agy", "quota": family})["status"] == "stale"


def test_office_status_names_a_route_chosen_on_a_stale_reading(monkeypatch):
    import sqlite3

    from office import guide
    _good_reading_then_expire_fresh_cache(monkeypatch, percent=60)
    FakeProbe(monkeypatch, [TIMEOUT, TIMEOUT])
    quota = candidates.probe_quota(ADAPTER)
    payload = dispatch._route_payload({"candidate": {"adapter_id": "claude", "quota": quota}})
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE dispatches(id TEXT, route_json TEXT)")
    con.execute("INSERT INTO dispatches VALUES('D1', ?)", (json.dumps(payload),))

    note = guide._quota_unknown_note(con, {"id": "T1", "current_dispatch_id": "D1"})

    assert note == f"T1 routed on stale quota: claude: {quota['cause']}"
