#!/usr/bin/env python3
"""Read remaining ChatGPT/Codex quota (5-hour and weekly windows), non-interactively.

Reads the stored Codex session token from ~/.codex/auth.json and asks the ChatGPT
backend for the account's rate-limit windows. No browser, no automation, no model
tokens consumed.

Usage:
    ./codex-usage.py           # human-readable
    ./codex-usage.py --json    # machine-readable, for routing (session + weekly)
    ./codex-usage.py --percent # bare integer percentage (tightest across windows)

Exit codes:
    0  headroom read successfully
    2  could not read (no auth file, no token, HTTP failure) — the caller must
       treat this as "codex headroom unknown" and route as if codex were exhausted
       unless the user says otherwise.
"""

import json
import os
import sys
import urllib.error
import urllib.request

AUTH_PATH = os.path.expanduser("~/.codex/auth.json")
USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"


def read_token(path=AUTH_PATH):
    if not os.path.exists(path):
        raise RuntimeError(f"auth file not found at {path} (run `codex login`)")
    with open(path) as f:
        auth = json.load(f)
    token = auth.get("tokens", {}).get("access_token")
    if not token:
        raise RuntimeError(f"no tokens.access_token in {path}")
    return token


def fetch_usage(token):
    req = urllib.request.Request(
        USAGE_URL, headers={"Authorization": f"Bearer {token}", "User-Agent": UA}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            return json.load(res)
    except urllib.error.HTTPError as e:
        body = e.read()[:200].decode("utf-8", "replace")
        raise RuntimeError(f"usage API returned {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"usage API unreachable: {e.reason}") from e


def _format_duration(seconds):
    if seconds is None:
        return "N/A"
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts) if parts else "0m"


def _parse_window(win):
    if not win or not isinstance(win, dict):
        return None
    used = win.get("used_percent", 0)
    reset = win.get("reset_after_seconds", 0) or 0
    reset_at = win.get("reset_at")
    window_secs = win.get("limit_window_seconds", 0) or 0
    return {
        "used_percent": used,
        "remaining_percent": 100 - used,
        "resets_in_seconds": reset,
        "resets_in": _format_duration(reset),
        "reset_at": reset_at,
        "limit_window_seconds": window_secs,
    }


def summarize(data):
    rl = data.get("rate_limit", {}) or {}
    primary = rl.get("primary_window", {}) or {}
    secondary = rl.get("secondary_window", {}) or {}

    p_parsed = _parse_window(primary)
    s_parsed = _parse_window(secondary)

    session = None
    weekly = None

    for w in (p_parsed, s_parsed):
        if not w:
            continue
        limit_secs = w["limit_window_seconds"]
        if limit_secs <= 86400:  # 5-hour window (18000s)
            session = w
        else:  # weekly window (604800s)
            weekly = w

    if session is None and weekly is None:
        if p_parsed:
            weekly = p_parsed
    elif weekly is None and p_parsed and p_parsed != session:
        weekly = p_parsed

    windows = [w for w in (session, weekly) if w]
    tightest_used = max(w["used_percent"] for w in windows) if windows else 0
    tightest_remaining = 100 - tightest_used

    info = {
        "account": data.get("email"),
        "tier": str(data.get("plan_type") or "").upper(),
        "session_used_percent": session["used_percent"] if session else None,
        "session_remaining_percent": session["remaining_percent"] if session else None,
        "session_resets_in_seconds": session["resets_in_seconds"] if session else None,
        "session_resets_in": session["resets_in"] if session else None,
        "session_reset_at": session["reset_at"] if session else None,
        "weekly_used_percent": weekly["used_percent"] if weekly else None,
        "weekly_remaining_percent": weekly["remaining_percent"] if weekly else None,
        "weekly_resets_in_seconds": weekly["resets_in_seconds"] if weekly else None,
        "weekly_resets_in": weekly["resets_in"] if weekly else None,
        "weekly_reset_at": weekly["reset_at"] if weekly else None,
        "tightest_used_percent": tightest_used,
        "tightest_remaining_percent": tightest_remaining,
        "resets_in_seconds": weekly["resets_in_seconds"] if weekly else (session["resets_in_seconds"] if session else 0),
        "resets_in": weekly["resets_in"] if weekly else (session["resets_in"] if session else "N/A"),
    }
    return info


def main(argv):
    try:
        info = summarize(fetch_usage(read_token()))
    except (RuntimeError, ValueError, OSError) as e:
        print(f"codex headroom UNKNOWN: {e}", file=sys.stderr)
        return 2

    if "--percent" in argv:
        print(info["tightest_remaining_percent"])
    elif "--json" in argv:
        print(json.dumps(info, indent=2))
    else:
        print("=== Codex / ChatGPT quota ===")
        print(f"Account: {info['account']}")
        print(f"Tier:    {info['tier']}")
        if info["session_used_percent"] is not None:
            print(f"5-Hour:  {info['session_used_percent']}% used, {info['session_remaining_percent']}% left (resets in {info['session_resets_in']})")
        if info["weekly_used_percent"] is not None:
            print(f"Weekly:  {info['weekly_used_percent']}% used, {info['weekly_remaining_percent']}% left (resets in {info['weekly_resets_in']})")
        print(f"Tightest Headroom: {info['tightest_remaining_percent']}% left")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
