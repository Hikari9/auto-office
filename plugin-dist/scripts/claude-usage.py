#!/usr/bin/env python3
"""Read remaining Claude Code subscription quota, non-interactively.

Reads the stored Claude Code OAuth token from the macOS login Keychain (service
"Claude Code-credentials"), falling back to ~/.claude/.credentials.json, and asks
api.anthropic.com for the account's rate-limit windows. No browser, no
automation, no model tokens consumed.

Usage:
    ./claude-usage.py           # human-readable
    ./claude-usage.py --json    # machine-readable, for routing
    ./claude-usage.py --percent # just the tightest remaining percent, e.g. "86"

Exit codes:
    0  headroom read successfully
    2  could not read (no credentials, no token, HTTP failure) — the caller must
       treat this as "claude headroom unknown" and route as if claude were
       exhausted unless the user says otherwise.
"""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

CREDS_PATH = os.path.expanduser("~/.claude/.credentials.json")
KEYCHAIN_SERVICE = "Claude Code-credentials"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"


def _token_from_blob(blob, origin):
    try:
        data = json.loads(blob)
    except ValueError as e:
        raise RuntimeError(f"{origin} is not valid JSON: {e}") from e
    token = data.get("claudeAiOauth", {}).get("accessToken")
    if not token:
        raise RuntimeError(f"no claudeAiOauth.accessToken in {origin}")
    return token


def read_token():
    errors = []
    try:
        blob = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        ).stdout.strip()
        return _token_from_blob(blob, f"keychain item {KEYCHAIN_SERVICE!r}")
    except (subprocess.SubprocessError, OSError, RuntimeError) as e:
        errors.append(f"keychain: {e}")

    if os.path.exists(CREDS_PATH):
        try:
            with open(CREDS_PATH) as f:
                return _token_from_blob(f.read(), CREDS_PATH)
        except (OSError, RuntimeError) as e:
            errors.append(f"{CREDS_PATH}: {e}")
    else:
        errors.append(f"{CREDS_PATH}: not found")

    raise RuntimeError("; ".join(errors) + " (run `claude` and log in)")


def fetch(url, token):
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", "User-Agent": UA}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            return json.load(res)
    except urllib.error.HTTPError as e:
        body = e.read()[:200].decode("utf-8", "replace")
        raise RuntimeError(f"{url} returned {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"{url} unreachable: {e.reason}") from e


def fetch_profile(token):
    """Account/plan labels. Best-effort — never fatal."""
    try:
        data = fetch(PROFILE_URL, token)
    except (RuntimeError, ValueError):
        return {}
    return {
        "account": data.get("account", {}).get("email"),
        "organization": data.get("organization", {}).get("name"),
        "tier": data.get("organization", {}).get("rate_limit_tier"),
    }


def _window(data, key):
    win = data.get(key) or {}
    used = win.get("utilization")
    if used is None:
        return None
    return {
        "used_percent": used,
        "remaining_percent": 100 - used,
        "resets_at": win.get("resets_at"),
    }


def summarize(data, profile):
    session = _window(data, "five_hour")
    weekly = _window(data, "seven_day")
    if session is None and weekly is None:
        raise RuntimeError(
            f"no five_hour/seven_day utilization in {USAGE_URL} response"
        )
    used = max(w["used_percent"] for w in (session, weekly) if w)
    info = {
        "account": profile.get("account"),
        "organization": profile.get("organization"),
        "tier": profile.get("tier"),
        "session_used_percent": session and session["used_percent"],
        "session_remaining_percent": session and session["remaining_percent"],
        "session_resets_at": session and session["resets_at"],
        "weekly_used_percent": weekly and weekly["used_percent"],
        "weekly_remaining_percent": weekly and weekly["remaining_percent"],
        "weekly_resets_at": weekly and weekly["resets_at"],
        "tightest_used_percent": used,
        "tightest_remaining_percent": 100 - used,
    }
    spend = data.get("spend") or {}
    info["extra_usage_enabled"] = bool(spend.get("enabled"))
    info["extra_usage_percent"] = spend.get("percent")
    return info


def main(argv):
    try:
        token = read_token()
        info = summarize(fetch(USAGE_URL, token), fetch_profile(token))
    except (RuntimeError, ValueError, OSError) as e:
        print(f"claude headroom UNKNOWN: {e}", file=sys.stderr)
        return 2

    if "--percent" in argv:
        print(info["tightest_remaining_percent"])
    elif "--json" in argv:
        print(json.dumps(info, indent=2))
    else:
        print("=== Claude Code subscription quota ===")
        print(f"Account: {info['account']}")
        print(f"Org:     {info['organization']}")
        print(f"Tier:    {info['tier']}")
        print(
            f"Session: {info['session_used_percent']}% used, "
            f"{info['session_remaining_percent']}% left "
            f"(resets {info['session_resets_at']})"
        )
        print(
            f"Weekly:  {info['weekly_used_percent']}% used, "
            f"{info['weekly_remaining_percent']}% left "
            f"(resets {info['weekly_resets_at']})"
        )
        print(f"Left:    {info['tightest_remaining_percent']}% (tightest window)")
        if info["extra_usage_enabled"]:
            print(f"Credits: extra usage ON, {info['extra_usage_percent']}% spent")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
