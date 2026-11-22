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
NAN = float("nan")
INF = float("inf")


def _ok(percent):
    return SimpleNamespace(returncode=0, stdout=json.dumps({"tightest_remaining_percent": percent}))


def _stdout(text):
    return SimpleNamespace(returncode=0, stdout=text)


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


def _edit_cache(edit):
    """Apply `edit(data)` to the harness entry of the shared cache file."""
    path = candidates._shared_cache_path()
    data = json.loads(path.read_text())
    edit(data["claude"])
    path.write_text(json.dumps(data))


def _age_last_good(seconds):
    _edit_cache(lambda entry: entry["last_good"].__setitem__("at", entry["last_good"]["at"] - seconds))


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


def test_a_non_timeout_failure_keeps_the_last_good_reading_for_a_later_timeout(monkeypatch):
    _good_reading_then_expire_fresh_cache(monkeypatch, percent=60)
    FakeProbe(monkeypatch, [SimpleNamespace(returncode=7, stdout="")])
    assert candidates.probe_quota(ADAPTER)["status"] == "unknown"
    candidates._QUOTA_CACHE.clear()
    FakeProbe(monkeypatch, [TIMEOUT, TIMEOUT])

    result = candidates.probe_quota(ADAPTER)

    assert result["status"] == "ok" and result["stale"] is True


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


@pytest.mark.parametrize(("junk", "age", "stale"), [("abc", 5, True), ("inf", 601, False), ("nan", 5, True)])
def test_unparseable_or_non_finite_last_good_ttl_uses_the_default(monkeypatch, junk, age, stale):
    # The default is 600s: a 5s-old reading is inside it, a 601s-old one is outside it. An infinite
    # or NaN TTL must fall back to 600s, so a 601s-old reading must not be served.
    _good_reading_then_expire_fresh_cache(monkeypatch, percent=60)
    monkeypatch.setenv("OFFICE_QUOTA_LAST_GOOD_TTL", junk)
    _age_last_good(seconds=age)
    FakeProbe(monkeypatch, [TIMEOUT, TIMEOUT])

    result = candidates.probe_quota(ADAPTER)

    assert result.get("stale", False) is stale


@pytest.mark.parametrize("corrupt_at", ["x", None, NAN, INF])
def test_a_corrupt_top_level_stamp_is_not_served_as_fresh(monkeypatch, corrupt_at):
    FakeProbe(monkeypatch, [_ok(60)])
    candidates.probe_quota(ADAPTER)
    _edit_cache(lambda entry: entry.__setitem__("at", corrupt_at))
    candidates._QUOTA_CACHE.clear()
    fake = FakeProbe(monkeypatch, [_ok(70)])

    result = candidates.probe_quota(ADAPTER)

    assert fake.calls == 1 and result == {"status": "ok", "tightest_remaining_percent": 70.0}


def test_a_future_fresh_stamp_is_not_served(monkeypatch):
    FakeProbe(monkeypatch, [_ok(90)])
    candidates.probe_quota(ADAPTER)
    _edit_cache(lambda entry: entry.__setitem__("at", entry["at"] + 3600))
    candidates._QUOTA_CACHE.clear()
    fake = FakeProbe(monkeypatch, [_ok(70)])

    result = candidates.probe_quota(ADAPTER)

    assert fake.calls == 1 and result["tightest_remaining_percent"] == 70.0


def test_a_nan_fresh_cache_entry_is_not_served(monkeypatch):
    candidates._shared_cache_put("claude", {"status": "ok", "tightest_remaining_percent": NAN})
    fake = FakeProbe(monkeypatch, [_ok(70)])

    result = candidates.probe_quota(ADAPTER)

    assert fake.calls == 1 and result == {"status": "ok", "tightest_remaining_percent": 70.0}


@pytest.mark.parametrize("stdout", ['{"tightest_remaining_percent": NaN}', '{"tightest_remaining_percent": Infinity}'])
def test_a_non_finite_probe_percent_is_unknown_not_ok(monkeypatch, stdout):
    FakeProbe(monkeypatch, [_stdout(stdout)])

    result = candidates.probe_quota(ADAPTER)

    assert result["status"] == "unknown" and result["tightest_remaining_percent"] is None
    assert result["cause"] == "command returned a non-finite tightest_remaining_percent"


@pytest.mark.parametrize("literal", ["NaN", "Infinity"])
def test_a_non_finite_family_percent_is_unknown_for_that_family(monkeypatch, literal):
    body = ('{"tightest_remaining_percent": 60, "families": {"claude": {"status": "ok", '
            '"tightest_remaining_percent": ' + literal + "}}}")
    FakeProbe(monkeypatch, [_stdout(body)])

    result = candidates.probe_quota({"id": "agy", "quota_probe": {"command": ["probe"]}})

    assert result["status"] == "ok" and result["families"]["claude"]["status"] == "unknown"


def test_a_huge_integer_family_percent_is_unknown_for_that_family(monkeypatch):
    body = ('{"tightest_remaining_percent": 60, "families": {"claude": {"status": "ok", '
            '"tightest_remaining_percent": ' + "9" * 400 + "}}}")
    FakeProbe(monkeypatch, [_stdout(body)])

    result = candidates.probe_quota({"id": "agy", "quota_probe": {"command": ["probe"]}})

    assert result["status"] == "ok" and result["families"]["claude"]["status"] == "unknown"


def test_a_boolean_family_percent_is_unknown_for_that_family(monkeypatch):
    body = ('{"tightest_remaining_percent": 60, "families": {"claude": {"status": "ok", '
            '"tightest_remaining_percent": true}}}')
    FakeProbe(monkeypatch, [_stdout(body)])

    result = candidates.probe_quota({"id": "agy", "quota_probe": {"command": ["probe"]}})

    assert result["status"] == "ok" and result["families"]["claude"]["status"] == "unknown"


def test_a_boolean_probe_percent_is_unknown_not_a_reading(monkeypatch):
    FakeProbe(monkeypatch, [_stdout('{"tightest_remaining_percent": true}')])

    result = candidates.probe_quota(ADAPTER)

    assert result["status"] == "unknown"
    assert result["cause"] == "command returned a boolean tightest_remaining_percent"


def test_a_fixture_non_finite_percent_is_unknown_not_a_reading(monkeypatch):
    monkeypatch.setenv("OFFICE_QUOTA_FIXTURE", '{"claude": NaN}')

    result = candidates.probe_quota(ADAPTER)

    assert result == {"status": "unknown", "tightest_remaining_percent": None,
                      "cause": "command returned a non-finite tightest_remaining_percent"}


def test_deeply_nested_probe_output_is_unknown_not_a_crash(monkeypatch):
    FakeProbe(monkeypatch, [_stdout("[" * 100000 + "]" * 100000)])

    result = candidates.probe_quota(ADAPTER)

    assert result == {"status": "unknown", "tightest_remaining_percent": None,
                      "cause": "command returned invalid JSON"}


def test_a_huge_integer_from_the_probe_is_unknown_not_a_crash(monkeypatch):
    FakeProbe(monkeypatch, [_stdout('{"tightest_remaining_percent": ' + "9" * 400 + "}")])

    result = candidates.probe_quota(ADAPTER)

    assert result == {"status": "unknown", "tightest_remaining_percent": None,
                      "cause": "command could not be run or parsed"}


@pytest.mark.parametrize("field", ["at", "percent"])
def test_a_huge_integer_in_the_cache_is_not_a_crash(monkeypatch, field):
    _good_reading_then_expire_fresh_cache(monkeypatch, percent=60)
    if field == "at":
        _edit_cache(lambda entry: entry["last_good"].__setitem__("at", 10 ** 400))
    else:
        _edit_cache(lambda entry: entry["last_good"]["result"].__setitem__("tightest_remaining_percent", 10 ** 400))
    FakeProbe(monkeypatch, [TIMEOUT, TIMEOUT])

    result = candidates.probe_quota(ADAPTER)

    assert result["status"] == "unknown" and "stale" not in result


@pytest.mark.parametrize(("junk", "age", "probes"), [("inf", 400, True), ("nan", 100, False)])
def test_a_non_finite_shared_ttl_uses_the_default(monkeypatch, junk, age, probes):
    # The default fresh TTL is 300s: a 400s-old entry is stale, a 100s-old one is fresh.
    FakeProbe(monkeypatch, [_ok(60)])
    candidates.probe_quota(ADAPTER)
    _edit_cache(lambda entry: entry.__setitem__("at", entry["at"] - age))
    monkeypatch.setenv("OFFICE_QUOTA_CACHE_TTL", junk)
    candidates._QUOTA_CACHE.clear()
    fake = FakeProbe(monkeypatch, [_ok(70)])

    result = candidates.probe_quota(ADAPTER)

    assert fake.calls == (1 if probes else 0)
    assert result["tightest_remaining_percent"] == (70.0 if probes else 60.0)


@pytest.mark.parametrize("patch", [
    {"tightest_remaining_percent": "abc"},
    {"tightest_remaining_percent": NAN},
    {"tightest_remaining_percent": INF},
    {"families": {"claude": {"status": "ok", "tightest_remaining_percent": "abc"}}},
    {"families": {"claude": {"status": "ok", "tightest_remaining_percent": NAN}}},
    {"tightest_remaining_percent": True},
    {"tightest_remaining_percent": "50"},
])
def test_an_invalid_stored_reading_is_not_served(monkeypatch, patch):
    _good_reading_then_expire_fresh_cache(monkeypatch, percent=60)
    _edit_cache(lambda entry: entry["last_good"]["result"].update(patch))
    FakeProbe(monkeypatch, [TIMEOUT, TIMEOUT])

    result = candidates.probe_quota(ADAPTER)

    assert result["status"] == "unknown" and "stale" not in result


@pytest.mark.parametrize("corrupt_at", ["x", None])
def test_a_corrupt_last_good_timestamp_is_not_served(monkeypatch, corrupt_at):
    _good_reading_then_expire_fresh_cache(monkeypatch, percent=60)
    _edit_cache(lambda entry: entry["last_good"].__setitem__("at", corrupt_at))
    FakeProbe(monkeypatch, [TIMEOUT, TIMEOUT])

    result = candidates.probe_quota(ADAPTER)

    assert result["status"] == "unknown" and "stale" not in result


def test_a_last_good_stamp_in_the_future_is_not_served(monkeypatch):
    _good_reading_then_expire_fresh_cache(monkeypatch, percent=60)
    _age_last_good(seconds=-3600)  # the clock moved back: the reading's age is unknowable
    FakeProbe(monkeypatch, [TIMEOUT, TIMEOUT])

    result = candidates.probe_quota(ADAPTER)

    assert result["status"] == "unknown" and "stale" not in result


def test_every_stale_ok_agy_family_is_marked_so_a_family_gate_sees_it(monkeypatch):
    agy = {"id": "agy", "quota_probe": {"command": ["probe"]}}
    body = json.dumps({"tightest_remaining_percent": 60, "families": {
        "claude": {"status": "ok", "tightest_remaining_percent": 40},
        "gemini": {"status": "ok", "tightest_remaining_percent": 20},
        "codex": {"status": "unknown"}}})
    FakeProbe(monkeypatch, [_stdout(body)])
    candidates.probe_quota(agy)
    monkeypatch.setenv("OFFICE_QUOTA_CACHE_TTL", "0")
    candidates._QUOTA_CACHE.clear()
    FakeProbe(monkeypatch, [TIMEOUT, TIMEOUT])

    result = candidates.probe_quota(agy)
    family = candidates.quota_for_candidate({"adapter_id": "agy", "model_id": "claude-sonnet-5-5"}, result)

    assert family["status"] == "ok" and family["tightest_remaining_percent"] == 40.0
    assert family["stale"] is True and family["cause"] == result["cause"]
    assert result["families"]["gemini"]["stale"] is True
    assert "stale" not in result["families"]["codex"]
    assert candidates.quota_unknown_record({"adapter_id": "agy", "quota": family})["status"] == "stale"


def test_the_probe_runs_with_stdin_closed(monkeypatch):
    fake = FakeProbe(monkeypatch, [_ok(60)])

    candidates.probe_quota(ADAPTER)

    assert fake.kwargs["stdin"] is subprocess.DEVNULL


def test_office_status_names_a_route_chosen_on_a_stale_reading(monkeypatch):
    import sqlite3

    from office import guide
    _good_reading_then_expire_fresh_cache(monkeypatch, percent=60)
    _age_last_good(seconds=120)
    FakeProbe(monkeypatch, [TIMEOUT, TIMEOUT])
    quota = candidates.probe_quota(ADAPTER)
    payload = dispatch._route_payload({"candidate": {"adapter_id": "claude", "quota": quota}})
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE dispatches(id TEXT, route_json TEXT)")
    con.execute("INSERT INTO dispatches VALUES('D1', ?)", (json.dumps(payload),))

    note = guide._quota_unknown_note(con, {"id": "T1", "current_dispatch_id": "D1"})

    assert note == "T1 routed on stale quota: claude: probe timed out; last good reading 120s old"
