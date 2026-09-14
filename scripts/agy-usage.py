#!/usr/bin/env python3
"""Read remaining AGY (Antigravity/Gemini) quota programmatically.

Reads OAuth credentials from ~/.gemini/antigravity-cli/antigravity-oauth-token,
refreshes the access token via Google OAuth2, and queries the CloudCode API endpoint:
https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota

Usage:
    ./agy-usage.py           # human-readable (Gemini models by default)
    ./agy-usage.py --all     # include other non-Claude models
    ./agy-usage.py --json    # machine-readable, for routing
    ./agy-usage.py --percent # bare integer percentage (lowest remaining among Gemini models)

Exit codes:
    0  successfully retrieved quota
    2  headroom unknown / error reading credentials

Note:
    Produces Gemini numbers by default, and never Claude numbers.

    The model list below is NOT an exhaustive catalog of valid agy models. It only
    contains models for which the CloudCode quota endpoint returned a bucket in
    this response (i.e. models with recorded usage / an assigned quota bucket on
    this account). A newly-released or rarely-used slug (observed: gemini-3.8-flash-low,
    2026-09-08 — works fine via `agy --model`, absent from this probe) can be
    completely valid and still not appear here. Do not conclude a model does not
    exist because it is missing from this output — check `agy models` (or
    `agy-office/scripts/agy-model.sh`) for the actual catalog instead.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

TOKEN_PATH = os.path.expanduser("~/.gemini/antigravity-cli/antigravity-oauth-token")

# Google OAuth2 credentials for the native Antigravity CLI desktop client (RFC 8252)
DEFAULT_CLIENT_ID = "REMOVED-OAUTH-CLIENT-ID-DISCOVERED-AT-RUNTIME"
DEFAULT_CLIENT_SECRET = "REMOVED-OAUTH-CLIENT-SECRET-DISCOVERED-AT-RUNTIME"

OAUTH_CLIENT_ID = os.environ.get("AGY_OAUTH_CLIENT_ID") or DEFAULT_CLIENT_ID
OAUTH_CLIENT_SECRET = os.environ.get("AGY_OAUTH_CLIENT_SECRET") or DEFAULT_CLIENT_SECRET
TOKEN_URL = "https://oauth2.googleapis.com/token"
QUOTA_URL = "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota"
UA = "antigravity-cli"


def get_refreshed_access_token():
    if not os.path.exists(TOKEN_PATH):
        return None, f"Token file not found at {TOKEN_PATH} (run `agy` and log in)"

    try:
        with open(TOKEN_PATH, "r") as f:
            data = json.load(f)
    except Exception as e:
        return None, f"Failed to read token file: {e}"

    tok_obj = data.get("token", {})
    refresh_token = tok_obj.get("refresh_token")
    if not refresh_token:
        return None, f"No refresh_token found in {TOKEN_PATH} (run `agy` and log in)"

    payload = urllib.parse.urlencode({
        "client_id": OAUTH_CLIENT_ID,
        "client_secret": OAUTH_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token"
    }).encode("utf-8")

    req = urllib.request.Request(
        TOKEN_URL,
        data=payload,
        headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"}
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            token_json = json.load(res)
            access_token = token_json.get("access_token")
            if not access_token:
                return None, "No access_token returned by OAuth refresh"
            return access_token, None
    except urllib.error.HTTPError as e:
        body = e.read()[:200].decode("utf-8", "replace")
        return None, f"OAuth token refresh failed ({e.code}): {body}"
    except urllib.error.URLError as e:
        return None, f"OAuth token refresh unreachable: {e.reason}"
    except Exception as e:
        return None, f"Error refreshing OAuth token: {e}"


def fetch_agy_quota():
    access_token, err = get_refreshed_access_token()
    if err:
        return None, err

    headers = {
        "Authorization": f"Bearer {access_token}",
        "User-Agent": UA,
        "Content-Type": "application/json"
    }

    req = urllib.request.Request(QUOTA_URL, data=b"{}", headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return json.load(res), None
    except urllib.error.HTTPError as e:
        body = e.read()[:200].decode("utf-8", "replace")
        return None, f"API returned status {e.code}: {body}"
    except urllib.error.URLError as e:
        return None, f"Quota API unreachable: {e.reason}"
    except Exception as e:
        return None, f"Failed to query quota API: {e}"


def process_quota(data, all_models=False):
    buckets = data.get("buckets", [])
    models = {}
    filtered_buckets = []
    min_pct = 100.0
    found_target = False

    for b in buckets:
        model_id = b.get("modelId") or ""
        model_lower = model_id.lower()

        # NEVER produce claude numbers
        if "claude" in model_lower:
            continue

        is_gemini = model_lower.startswith("gemini")
        if not all_models and not is_gemini:
            continue

        frac = b.get("remainingFraction", 1.0)
        pct = round(frac * 100, 1)
        reset_time = b.get("resetTime")

        models[model_id] = {
            "remaining_percent": pct,
            "reset_time": reset_time
        }
        filtered_buckets.append(b)

        # Track minimum remaining percentage for active model buckets
        if is_gemini or all_models:
            if not found_target or pct < min_pct:
                min_pct = pct
                found_target = True
        elif not found_target and pct < min_pct:
            min_pct = pct

    return {
        "tightest_remaining_percent": int(min_pct) if (found_target or models) else 100,
        "models": models,
        "raw_buckets": filtered_buckets,
        "note": (
            "models lists only quota-tracked buckets returned by this response, not an "
            "exhaustive catalog of valid agy models. A model's absence here does not mean "
            "it does not exist — check `agy models` for the actual catalog."
        )
    }


def main(argv):
    raw_data, err = fetch_agy_quota()
    if err:
        if "--json" in argv:
            print(json.dumps({"error": err, "remaining_percent": None}, indent=2))
        else:
            print(f"AGY quota probe UNKNOWN: {err}", file=sys.stderr)
        return 2

    all_models = "--all" in argv
    processed = process_quota(raw_data, all_models=all_models)
    tightest_pct = processed["tightest_remaining_percent"]

    if "--percent" in argv:
        print(tightest_pct)
        return 0
    elif "--json" in argv:
        print(json.dumps(processed, indent=2))
        return 0
    else:
        print("=== AGY (Antigravity/Gemini) Quota ===")
        print(f"Overall Tightest Headroom: {tightest_pct}% left")
        print("\nModel Breakdown (quota-tracked models only — NOT an exhaustive catalog;")
        print("a model missing here may still be valid, check `agy models` before ruling it out):")
        for model_id, info in processed["models"].items():
            pct = info["remaining_percent"]
            reset = info.get("reset_time", "N/A")
            print(f"  - {model_id}: {pct}% remaining (resets: {reset})")
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
