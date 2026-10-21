"""Token-location discovery in scripts/agy-usage.py.

Every test points HOME at a temp dir, so the real user token is never read.
"""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LEGACY = ".gemini/antigravity-cli/antigravity-oauth-token"
NEW = ".gemini/jetski-standalone-oauth-token"


@pytest.fixture
def agy(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location("agy_usage", ROOT / "scripts/agy-usage.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # Stop before the network: record the refresh token the probe settled on.
    seen = {}

    def fake_resolve():
        return [("cid", "csecret")], None, None

    def fake_urlopen(req, timeout=None):
        seen["body"] = req.data.decode()
        raise mod.urllib.error.URLError("stop")

    monkeypatch.setattr(mod, "resolve_oauth_client", fake_resolve)
    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    mod.seen = seen
    return mod


def put(home, rel, payload):
    path = home / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return path


def token(value):
    return {"token": {"refresh_token": value}}


def test_legacy_path_only(agy, tmp_path):
    put(tmp_path, LEGACY, token("legacy-rt"))
    agy.get_refreshed_access_token()
    assert "refresh_token=legacy-rt" in agy.seen["body"]


def test_new_path_only(agy, tmp_path):
    put(tmp_path, NEW, token("new-rt"))
    agy.get_refreshed_access_token()
    assert "refresh_token=new-rt" in agy.seen["body"]


def test_legacy_wins_when_both_exist(agy, tmp_path):
    put(tmp_path, LEGACY, token("legacy-rt"))
    put(tmp_path, NEW, token("new-rt"))
    agy.get_refreshed_access_token()
    assert "refresh_token=legacy-rt" in agy.seen["body"]


def test_neither_present_names_every_path(agy, tmp_path):
    access, err = agy.get_refreshed_access_token()
    assert access is None
    assert str(tmp_path / LEGACY) in err
    assert str(tmp_path / NEW) in err
    assert "body" not in agy.seen


@pytest.mark.parametrize("payload", [
    {"unexpected": "layout"},
    {"token": {}},
    {"token": "string"},
    {"token": {"refresh_token": ""}},
    ["not", "a", "dict"],
])
def test_unrecognised_shape_names_file(agy, tmp_path, payload):
    path = put(tmp_path, NEW, payload)
    access, err = agy.get_refreshed_access_token()
    assert access is None
    assert str(path) in err
    assert "unrecognised format" in err
    assert "body" not in agy.seen


def test_unrecognised_shape_exits_2_with_json_error(agy, tmp_path, capsys):
    put(tmp_path, NEW, {"unexpected": "layout"})
    assert agy.main(["--json"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["remaining_percent"] is None
    assert "unrecognised format" in out["error"]


def test_unreadable_json_names_file(agy, tmp_path):
    path = put(tmp_path, NEW, "{not json")
    _, err = agy.get_refreshed_access_token()
    assert str(path) in err


def test_docstring_names_both_paths(agy):
    assert "~/.gemini/antigravity-cli/antigravity-oauth-token" in agy.__doc__
    assert "~/.gemini/jetski-standalone-oauth-token" in agy.__doc__


def test_process_quota_reports_independent_model_families(agy):
    result = agy.process_quota({
        "buckets": [
            {
                "modelId": "gemini-3.8-flash-high",
                "remainingFraction": 0.73,
                "resetTime": "2026-10-21T10:00:00Z",
            },
            {
                "modelId": "claude-sonnet-5-5-medium",
                "remainingFraction": 0.0,
                "resetTime": "2026-10-21T16:00:00Z",
            },
        ]
    })

    assert result["families"] == {
        "gemini": {
            "tightest_remaining_percent": 73,
            "reset_time": "2026-10-21T10:00:00Z",
            "status": "ok",
        },
        "claude": {
            "tightest_remaining_percent": 0,
            "reset_time": "2026-10-21T16:00:00Z",
            "status": "ok",
        },
    }
    assert result["tightest_remaining_percent"] == 73


SUMMARY = {"groups": [
    {"displayName": "Gemini Models", "buckets": [
        {"bucketId": "gemini-weekly", "window": "weekly", "remainingFraction": 0.9668508,
         "resetTime": "2026-10-22T03:07:25Z"},
        {"bucketId": "gemini-5h", "window": "5h", "remainingFraction": 1,
         "resetTime": "2026-10-21T09:52:36Z"},
    ]},
    {"displayName": "Claude and GPT models", "buckets": [
        {"bucketId": "3p-weekly", "window": "weekly", "remainingFraction": 0.9922192,
         "resetTime": "2026-10-28T04:44:18Z"},
        {"bucketId": "3p-5h", "window": "5h", "remainingFraction": 0.0,
         "resetTime": "2026-10-21T09:44:18Z"},
    ]},
]}


def test_summary_families_take_the_tightest_window_per_group(agy):
    assert agy.summary_families(SUMMARY) == {
        "gemini": {"tightest_remaining_percent": 96, "reset_time": "2026-10-22T03:07:25Z", "status": "ok"},
        "claude": {"tightest_remaining_percent": 0, "reset_time": "2026-10-21T09:44:18Z", "status": "ok"},
    }


@pytest.mark.parametrize("summary", [None, {}, {"groups": "x"}, {"groups": [{"displayName": "Other", "buckets": []}]},
                                     {"groups": [{"displayName": "Gemini Models", "buckets": [{"bucketId": "g"}]}]}])
def test_summary_families_unusable_input_yields_nothing(agy, summary):
    assert agy.summary_families(summary) == {}


def _main_json(agy, monkeypatch, capsys, summary):
    raw = {"buckets": [
        {"modelId": "gemini-3.8-flash-high", "remainingFraction": 0.5, "resetTime": "g"},
        {"modelId": "claude-sonnet-5-5-high", "remainingFraction": 0.8, "resetTime": "c"},
    ]}
    monkeypatch.setattr(agy, "fetch_agy_quota", lambda: (raw, summary, None))
    assert agy.main(["--json"]) == 0
    return json.loads(capsys.readouterr().out)


def test_main_prefers_account_summary_for_families(agy, monkeypatch, capsys):
    out = _main_json(agy, monkeypatch, capsys, SUMMARY)
    assert out["family_source"] == "retrieveUserQuotaSummary"
    assert out["families"]["gemini"]["tightest_remaining_percent"] == 96
    assert out["families"]["claude"]["tightest_remaining_percent"] == 0
    assert out["tightest_remaining_percent"] == 96  # legacy number is the Gemini family result


@pytest.mark.parametrize("flags", [[], ["--claude"], ["--all"], ["--all", "--claude"]])
def test_legacy_number_is_gemini_family_for_every_flag_combination(agy, monkeypatch, capsys, flags):
    raw = {"buckets": [
        {"modelId": "gemini-3.8-flash-high", "remainingFraction": 0.5, "resetTime": "g"},
        {"modelId": "claude-sonnet-5-5-high", "remainingFraction": 0.1, "resetTime": "c"},
        {"modelId": "other-model", "remainingFraction": 0.0, "resetTime": "o"},
    ]}
    monkeypatch.setattr(agy, "fetch_agy_quota", lambda: (raw, None, None))
    assert agy.main(["--json", *flags]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["families"]["gemini"]["tightest_remaining_percent"] == 50
    assert out["tightest_remaining_percent"] == 50


def test_legacy_number_is_unknown_when_gemini_family_has_no_source(agy, monkeypatch, capsys):
    raw = {"buckets": [{"modelId": "claude-sonnet-5-5-high", "remainingFraction": 0.8, "resetTime": "c"}]}
    monkeypatch.setattr(agy, "fetch_agy_quota", lambda: (raw, None, None))
    assert agy.main(["--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["tightest_remaining_percent"] is None and out["cause"]
    assert out["families"]["gemini"]["status"] == "unknown" and out["families"]["gemini"]["cause"]
    assert out["families"]["claude"]["tightest_remaining_percent"] == 80
    assert agy.main(["--percent"]) == 2


def test_bucket_without_fraction_is_unknown_not_full(agy):
    result = agy.process_quota({"buckets": [{"modelId": "gemini-3.8-flash-high", "resetTime": "g"}]})
    assert result["tightest_remaining_percent"] is None
    assert result["families"]["gemini"]["status"] == "unknown"


def test_main_falls_back_to_per_model_buckets_without_summary(agy, monkeypatch, capsys):
    out = _main_json(agy, monkeypatch, capsys, None)
    assert out["family_source"] == "retrieveUserQuota"
    assert out["families"]["gemini"]["tightest_remaining_percent"] == 50
    assert out["families"]["claude"]["tightest_remaining_percent"] == 80


def test_summary_failure_does_not_fail_the_probe(agy, monkeypatch):
    monkeypatch.setattr(agy, "get_refreshed_access_token", lambda: ("tok", None))
    calls = []

    def post(url, token):
        calls.append(url)
        return ({"buckets": []}, None) if url == agy.QUOTA_URL else (None, "boom")

    monkeypatch.setattr(agy, "_post_quota", post)
    assert agy.fetch_agy_quota() == ({"buckets": []}, None, None)
    assert calls == [agy.QUOTA_URL, agy.SUMMARY_URL]
