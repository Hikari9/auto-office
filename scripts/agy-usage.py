#!/usr/bin/env python3
"""Read remaining AGY (Antigravity/Gemini) quota programmatically.

Reads OAuth credentials from ~/.gemini/antigravity-cli/antigravity-oauth-token,
refreshes the access token via Google OAuth2, and queries the CloudCode API endpoint:
https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota

Usage:
    ./agy-usage.py           # human-readable (Gemini models by default)
    ./agy-usage.py --all     # include other non-Claude models
    ./agy-usage.py --claude  # ALSO include Claude buckets (opt-in, see Note)
    ./agy-usage.py --json    # machine-readable, for routing
    ./agy-usage.py --percent # bare integer percentage (lowest remaining among Gemini models)

Exit codes:
    0  successfully retrieved quota
    2  headroom unknown / error reading credentials

Note:
    Produces Gemini numbers by default, and never Claude numbers unless --claude
    is passed. agy is used here for Gemini-based models only; a Claude model is
    routed through the claude harness, which has its own quota. The Claude
    buckets on this endpoint are a separate allowance on a different reset
    schedule (observed 2026-09-17: gemini 20 buckets at one shared percentage
    and reset, claude 2 buckets at 0% with a reset six hours later), so folding
    them into the default headroom number would let an allowance nobody is
    routing against veto a Gemini dispatch.

    --all is for inspection, not routing. It admits buckets such as
    gpt-oss-120b-medium that sit at 0% and would drive tightest_remaining_percent
    to 0, refusing every dispatch. Route off the default output.

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
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request

TOKEN_PATH = os.path.expanduser("~/.gemini/antigravity-cli/antigravity-oauth-token")

# ---------------------------------------------------------------------------
# OAuth client credentials are DISCOVERED from the installed Antigravity CLI,
# never hard-coded here.
#
# They used to be literals in this file. GitHub push protection blocked a
# branch over them (Google OAuth Client ID + Secret, 2026-09-15) and it was
# right to: whatever RFC 8252 says about installed-app secrets not being
# confidential, a credential committed to a repo cannot be rotated without a
# commit, and it turns every future push to this repo into a secret-scanning
# negotiation.
#
# `agy` is a single self-contained binary that necessarily carries the client
# it authenticates as, so the values are already present on any machine that
# can run this probe at all. Reading them from there keeps exactly one copy on
# disk, owned by the tool that owns the credential, and picks up a rotation the
# moment the CLI is upgraded.
# ---------------------------------------------------------------------------
TOKEN_URL = "https://oauth2.googleapis.com/token"
QUOTA_URL = "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota"
UA = "antigravity-cli"

CLIENT_ID_RE = re.compile(rb"[0-9]{10,}-[a-z0-9]{28,}\.apps\.googleusercontent\.com")
# Exactly 28 characters after the prefix -- Google's fixed client-secret shape.
# NOT `{20,}` and NOT a negative lookahead: in the agy binary the secret abuts
# unrelated string data with no delimiter, so a greedy match runs past the end
# of the credential, and a lookahead matches nothing at all. Both were observed;
# the greedy one produced a plausible value the token endpoint simply rejected.
CLIENT_SECRET_RE = re.compile(rb"GOCSPX-[A-Za-z0-9_-]{28}")
CREDENTIAL_CACHE = os.path.expanduser("~/.cache/auto-office/agy-oauth-client.json")


def _scan_agy_binary(path):
    """Every (client_id, secret) candidate in the agy binary.

    A LIST, not a pair: the binary carries more than one client id and there is
    no reliable structural way to tell which is live. Proximity to the secret
    picks the wrong one, and match order is an implementation detail of
    whatever bundler produced the binary. Rather than encode a brittle "it is
    the second one", hand over every candidate and let the token endpoint
    adjudicate; the winner is cached, so the ambiguity costs one extra request
    once per agy upgrade.

    Streamed in chunks with an overlap, not read whole: the binary is ~180 MB
    and this probe sits on the routing path. The overlap is so a match
    straddling a chunk boundary is not missed -- without it the probe would
    fail intermittently, on some installs and not others.
    """
    ids, secrets = [], []
    tail_bytes = b""
    try:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                buf = tail_bytes + chunk
                for m in CLIENT_ID_RE.finditer(buf):
                    v = m.group(0).decode()
                    if v not in ids:
                        ids.append(v)
                for m in CLIENT_SECRET_RE.finditer(buf):
                    v = m.group(0).decode()
                    if v not in secrets:
                        secrets.append(v)
                tail_bytes = buf[-256:]
    except OSError:
        return []
    return [(i, s) for i in ids for s in secrets]


def _binary_key(binary):
    """Cache key on (path, size, mtime) so an agy upgrade invalidates it.
    Not a content hash: hashing 180 MB to avoid scanning 180 MB saves nothing."""
    st = os.stat(binary)
    return {"path": binary, "size": st.st_size, "mtime": int(st.st_mtime)}


def _read_cached_pair(key):
    try:
        with open(CREDENTIAL_CACHE, "r") as fh:
            blob = json.load(fh)
    except (OSError, ValueError):
        return None
    if all(blob.get(k) == v for k, v in key.items()):
        cid, sec = blob.get("client_id"), blob.get("client_secret")
        if cid and sec:
            return (cid, sec)
    return None


def _write_cached_pair(key, cid, sec):
    try:
        os.makedirs(os.path.dirname(CREDENTIAL_CACHE), exist_ok=True)
        fd = os.open(CREDENTIAL_CACHE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({**key, "client_id": cid, "client_secret": sec}, fh)
    except OSError:
        pass


def resolve_oauth_client():
    """(candidates, cache_key, error). Never returns a baked-in default.

    EXPLICIT ENVIRONMENT FIRST, then discovery -- a deliberate inversion of
    "discover, falling back to env". An override that applies only when
    discovery fails is not an override: with `agy` installed it would silently
    do nothing, which is the more confusing of the two failures.
    """
    env_id = os.environ.get("AGY_OAUTH_CLIENT_ID")
    env_secret = os.environ.get("AGY_OAUTH_CLIENT_SECRET")
    if env_id and env_secret:
        return [(env_id, env_secret)], None, None
    if env_id or env_secret:
        return [], None, (
            "Set BOTH AGY_OAUTH_CLIENT_ID and AGY_OAUTH_CLIENT_SECRET, or neither "
            "(one alone cannot authenticate, and silently pairing it with a "
            "discovered value would mix two clients)."
        )

    binary = shutil.which("agy")
    if not binary:
        return [], None, (
            "`agy` not found on PATH, so OAuth client credentials cannot be "
            "discovered. Install the Antigravity CLI, or set "
            "AGY_OAUTH_CLIENT_ID and AGY_OAUTH_CLIENT_SECRET."
        )

    try:
        key = _binary_key(binary)
    except OSError as exc:
        return [], None, f"Cannot stat {binary}: {exc}"

    cached = _read_cached_pair(key)
    if cached:
        return [cached], key, None

    candidates = _scan_agy_binary(binary)
    if not candidates:
        return [], key, (
            f"Could not find OAuth client credentials in {binary}. The CLI's "
            "internals may have changed; set AGY_OAUTH_CLIENT_ID and "
            "AGY_OAUTH_CLIENT_SECRET to override."
        )
    return candidates, key, None


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

    candidates, cache_key, cred_err = resolve_oauth_client()
    if cred_err:
        return None, cred_err

    last_error = None
    for client_id, client_secret in candidates:
        payload = urllib.parse.urlencode({
            "client_id": client_id,
            "client_secret": client_secret,
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
                if cache_key:
                    _write_cached_pair(cache_key, client_id, client_secret)
                return access_token, None
        except urllib.error.HTTPError as e:
            body = e.read()[:200].decode("utf-8", "replace")
            last_error = f"OAuth token refresh failed ({e.code}): {body}"
            # A rejected CLIENT means this candidate is the wrong one -- try the
            # next. `invalid_grant` means the user's refresh token is dead, which
            # no other candidate can fix, so stop.
            if "invalid_grant" in body:
                return None, last_error
            if e.code in (400, 401):
                continue
            return None, last_error
        except urllib.error.URLError as e:
            return None, f"OAuth token refresh unreachable: {e.reason}"

    return None, last_error or "No OAuth client candidates available"


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


def process_quota(data, all_models=False, include_claude=False):
    buckets = data.get("buckets", [])
    models = {}
    filtered_buckets = []
    min_pct = 100.0
    found_target = False

    for b in buckets:
        model_id = b.get("modelId") or ""
        model_lower = model_id.lower()

        # Claude is a separate allowance on a separate reset, and agy is used
        # here for Gemini only -- so it is excluded unless asked for explicitly.
        is_claude = "claude" in model_lower
        if is_claude and not include_claude:
            continue

        is_gemini = model_lower.startswith("gemini")
        if not all_models and not (is_gemini or is_claude):
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
        if is_gemini or is_claude or all_models:
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
    include_claude = "--claude" in argv
    processed = process_quota(
        raw_data, all_models=all_models, include_claude=include_claude
    )
    processed["scope"] = "gemini+claude" if include_claude else (
        "gemini+other" if all_models else "gemini"
    )
    tightest_pct = processed["tightest_remaining_percent"]

    if "--percent" in argv:
        print(tightest_pct)
        return 0
    elif "--json" in argv:
        print(json.dumps(processed, indent=2))
        return 0
    else:
        print("=== AGY (Antigravity/Gemini) Quota ===")
        print(f"Scope: {processed['scope']}"
              + ("" if include_claude else "  (Claude excluded; pass --claude to include)"))
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
