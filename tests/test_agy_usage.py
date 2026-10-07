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
