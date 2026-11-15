"""Token-location discovery, keychain token and AI credits in scripts/agy-usage.py.

Every test points HOME at a temp dir, so the real user token is never read.
"""
import base64
import importlib.util
import json
import subprocess
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
    # Default keychain isolation: `security` reports a missing item. Tests that
    # exercise the keychain override this through _keychain().
    mod.keychain_calls = []

    def missing_item(cmd, *a, **kw):
        mod.keychain_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 44, b"", b"")

    monkeypatch.setattr(mod.subprocess, "run", missing_item)
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
    # Any keychain lookup went through the fixture's fake, never a real `security`.
    assert all(c[0] == "security" for c in agy.keychain_calls)
    assert agy.subprocess.run.__name__ == "missing_item"


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
    monkeypatch.setattr(agy, "fetch_agy_quota", lambda: (raw, summary, None, None))
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
    monkeypatch.setattr(agy, "fetch_agy_quota", lambda: (raw, None, None, None))
    assert agy.main(["--json", *flags]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["families"]["gemini"]["tightest_remaining_percent"] == 50
    assert out["tightest_remaining_percent"] == 50


def test_legacy_number_is_unknown_when_gemini_family_has_no_source(agy, monkeypatch, capsys):
    raw = {"buckets": [{"modelId": "claude-sonnet-5-5-high", "remainingFraction": 0.8, "resetTime": "c"}]}
    monkeypatch.setattr(agy, "fetch_agy_quota", lambda: (raw, None, None, None))
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

    def post(url, token, body=None):
        calls.append(url)
        return ({"buckets": []}, None) if url == agy.QUOTA_URL else (None, "boom")

    monkeypatch.setattr(agy, "_post_quota", post)
    data, summary, credits, err = agy.fetch_agy_quota()
    assert (data, summary, err) == ({"buckets": []}, None, None)
    assert credits["status"] == "unknown"
    assert calls == [agy.QUOTA_URL, agy.SUMMARY_URL, agy.LOAD_CODE_ASSIST_URL]


# --- macOS keychain token ---------------------------------------------------

FAKE_RT = "fake-keychain-rt"


def _keychain(agy, monkeypatch, stdout="", returncode=0, platform="darwin", raises=None):
    monkeypatch.setattr(agy.sys, "platform", platform)
    calls = []

    def run(cmd, **kw):
        calls.append((cmd, kw))
        if raises:
            raise raises
        assert not kw.get("text") and not kw.get("universal_newlines")  # real stdout is bytes
        out = stdout if isinstance(stdout, bytes) else stdout.encode("utf-8")
        return subprocess.CompletedProcess(cmd, returncode, out, b"")

    monkeypatch.setattr(agy.subprocess, "run", run)
    return calls


def test_keychain_base64_value_is_used_after_files(agy, tmp_path, monkeypatch):
    blob = "go-keyring-base64:" + base64.b64encode(json.dumps(token(FAKE_RT)).encode()).decode()
    calls = _keychain(agy, monkeypatch, stdout=blob + "\n")
    agy.get_refreshed_access_token()
    assert f"refresh_token={FAKE_RT}" in agy.seen["body"]
    cmd, kw = calls[0]
    assert cmd == ["security", "find-generic-password", "-s", "gemini", "-a", "antigravity", "-w"]
    assert kw.get("timeout") and not kw.get("shell")


def test_keychain_plain_json_value(agy, monkeypatch):
    _keychain(agy, monkeypatch, stdout=json.dumps(token(FAKE_RT)))
    agy.get_refreshed_access_token()
    assert f"refresh_token={FAKE_RT}" in agy.seen["body"]


def test_token_file_wins_over_keychain(agy, tmp_path, monkeypatch):
    put(tmp_path, NEW, token("file-rt"))
    calls = _keychain(agy, monkeypatch, stdout=json.dumps(token(FAKE_RT)))
    agy.get_refreshed_access_token()
    assert "refresh_token=file-rt" in agy.seen["body"] and not calls


@pytest.mark.parametrize("kw", [{"returncode": 44}, {"raises": FileNotFoundError()}])
def test_keychain_item_missing_names_every_location(agy, tmp_path, monkeypatch, kw):
    _keychain(agy, monkeypatch, **kw)
    access, err = agy.get_refreshed_access_token()
    assert access is None
    assert str(tmp_path / LEGACY) in err and str(tmp_path / NEW) in err
    assert "keychain gemini/antigravity" in err and "log in" in err
    assert "body" not in agy.seen


def test_off_macos_never_runs_security(agy, tmp_path, monkeypatch):
    calls = _keychain(agy, monkeypatch, platform="linux")
    _, err = agy.get_refreshed_access_token()
    assert not calls and str(tmp_path / NEW) in err


SECRET_VALUE = "SECRET-DO-NOT-LEAK"


@pytest.mark.parametrize("kw, needle", [
    ({"returncode": 1, "stdout": SECRET_VALUE}, "security exited 1"),
    ({"raises": subprocess.TimeoutExpired("security", 5)}, "TimeoutExpired"),
    ({"stdout": "go-keyring-base64:" + SECRET_VALUE + "!!"}, "Malformed"),
    ({"stdout": SECRET_VALUE}, "Malformed"),
    ({"stdout": b"\xff\xfe" + SECRET_VALUE.encode()}, "Malformed"),
    ({"stdout": json.dumps({"token": {"access_token": SECRET_VALUE}})}, "no token.refresh_token"),
])
def test_keychain_failures_are_distinct_and_leak_nothing(agy, monkeypatch, kw, needle):
    _keychain(agy, monkeypatch, **kw)
    access, err = agy.get_refreshed_access_token()
    assert access is None and needle in err
    assert "keychain gemini/antigravity" in err and SECRET_VALUE not in err
    assert "body" not in agy.seen


# --- AI credits -------------------------------------------------------------

def _credits_main(agy, monkeypatch, capsys, credits, flags=("--json",)):
    raw = {"buckets": [
        {"modelId": "gemini-3.8-flash-high", "remainingFraction": 0.5, "resetTime": "g"},
        {"modelId": "claude-sonnet-5-5-high", "remainingFraction": 0.8, "resetTime": "c"},
    ]}
    monkeypatch.setattr(agy, "fetch_agy_quota", lambda: (raw, None, credits, None))
    code = agy.main(list(flags))
    return code, capsys.readouterr().out


def test_credits_from_paid_tier_ok_with_string_amount(agy):
    out = agy.credits_from({"paidTier": {"availableCredits": [
        {"creditType": "GOOGLE_ONE_AI", "creditAmount": "2500"}]}})
    assert out == {"status": "ok", "balance": 2500, "minimum": 1,
                   "credit_type": "GOOGLE_ONE_AI", "source": "paidTier"}


def test_credits_current_tier_and_minimum_from_response(agy):
    out = agy.credits_from({"currentTier": {"availableCredits": [
        {"creditType": "T", "creditAmount": 50}]}, "minimumCreditAmountForUsage": "50"})
    assert out["status"] == "ok" and out["minimum"] == 50 and out["source"] == "currentTier"


def test_credits_below_minimum_is_exhausted(agy):
    out = agy.credits_from({"paidTier": {"availableCredits": [
        {"creditType": "T", "creditAmount": "0", "minimumCreditAmountForUsage": "10"}]}})
    assert out["status"] == "exhausted" and out["balance"] == 0 and out["minimum"] == 10


@pytest.mark.parametrize("resp", [None, {}, {"paidTier": {}}, {"paidTier": {"availableCredits": []}},
                                  {"paidTier": {"availableCredits": [{"creditAmount": "x"}]}}])
def test_credits_unknown_with_cause(agy, resp):
    out = agy.credits_from(resp)
    assert out["status"] == "unknown" and out["cause"] and out["balance"] is None


def test_exhausted_credits_force_zero_on_both_families(agy, monkeypatch, capsys):
    credits = {"status": "exhausted", "balance": 0, "minimum": 1, "credit_type": "T", "source": "paidTier"}
    code, out = _credits_main(agy, monkeypatch, capsys, credits)
    out = json.loads(out)
    assert code == 0 and out["credits"] == credits
    assert out["tightest_remaining_percent"] == 0
    for name, reset in (("gemini", "g"), ("claude", "c")):
        fam = out["families"][name]
        assert fam["status"] == "ok" and fam["tightest_remaining_percent"] == 0
        assert fam["cause"] == "AI credits balance too low" and fam["exhausted_by"] == "ai_credits"
        assert fam["reset_time"] == reset


def test_exhausted_credits_percent_prints_zero(agy, monkeypatch, capsys):
    credits = {"status": "exhausted", "balance": 0, "minimum": 1, "credit_type": "T", "source": "paidTier"}
    code, out = _credits_main(agy, monkeypatch, capsys, credits, flags=("--percent",))
    assert code == 0 and out.strip() == "0"


@pytest.mark.parametrize("status", ["ok", "unknown"])
def test_non_exhausted_credits_leave_quota_alone(agy, monkeypatch, capsys, status):
    credits = {"status": status, "balance": 5, "minimum": 1, "credit_type": "T", "source": "paidTier"}
    _, out = _credits_main(agy, monkeypatch, capsys, credits)
    out = json.loads(out)
    assert out["credits"]["status"] == status
    assert out["tightest_remaining_percent"] == 50
    assert out["families"]["claude"]["tightest_remaining_percent"] == 80
    assert "exhausted_by" not in out["families"]["gemini"]


def test_credits_endpoint_failure_leaves_quota_as_is(agy, monkeypatch):
    monkeypatch.setattr(agy, "get_refreshed_access_token", lambda: ("tok", None))
    seen = []

    def post(url, token, body=None):
        seen.append((url, body))
        if url == agy.LOAD_CODE_ASSIST_URL:
            return None, "API returned status 500: boom"
        return ({"buckets": [{"modelId": "gemini-x", "remainingFraction": 0.4}]}, None)

    monkeypatch.setattr(agy, "_post_quota", post)
    data, _, credits, err = agy.fetch_agy_quota()
    assert err is None and data["buckets"]
    assert credits["status"] == "unknown" and "500" in credits["cause"]
    assert (agy.LOAD_CODE_ASSIST_URL, {"metadata": {"ideType": "ANTIGRAVITY", "pluginType": "GEMINI"}}) in seen


def test_fetch_parses_load_code_assist_response(agy, monkeypatch):
    monkeypatch.setattr(agy, "get_refreshed_access_token", lambda: ("tok", None))

    def post(url, token, body=None):
        if url == agy.LOAD_CODE_ASSIST_URL:
            return {"paidTier": {"availableCredits": [{"creditType": "T", "creditAmount": "0"}]}}, None
        return {"buckets": []}, None

    monkeypatch.setattr(agy, "_post_quota", post)
    assert agy.fetch_agy_quota()[2]["status"] == "exhausted"


def test_missing_auth_exits_2_with_both_families_unknown(agy, tmp_path, monkeypatch, capsys):
    _keychain(agy, monkeypatch, returncode=44)
    assert agy.main(["--json"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["families"]["gemini"]["status"] == "unknown"
    assert out["families"]["claude"]["status"] == "unknown"
    assert "keychain gemini/antigravity" in out["error"]


def test_docstring_documents_keychain_and_credits(agy):
    for needle in ("security find-generic-password -s gemini -a antigravity -w", "go-keyring-base64:",
                   "loadCodeAssist", "availableCredits", "minimumCreditAmountForUsage",
                   "AI credits balance too low", "ai_credits"):
        assert needle in agy.__doc__
