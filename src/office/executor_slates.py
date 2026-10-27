"""Issue #809: version-pinned size floors and *advisory* ideal executor routes.

This is not a registry. A preferred route is never made dispatchable merely
because it is named here; current catalog, trust, capability, quota, discovery,
and per-invocation conformance gates remain authoritative.
"""
from __future__ import annotations

INDEX = "Artificial Analysis Intelligence Index v4.3.2"
FLOORS = {"XS": 31, "S": 38, "M": 40, "L": 44, "XL": 47}
VERSION = "xs-xl-executor-slates-2026-10-27"

# Model ID, exact effort, harness (None = qualify any real harness).
# M/L Sonnet self-fallback is the identical model and effort, Claude Code then
# agy; other-model alternatives come afterwards. Missing rows stay conditional.
IDEAL = {
    "XS": (("claude-haiku-5-5", "medium", "claude"),
           ("gemini-3.8-flash", "low", None),
           ("gpt-6.1-terra", "low", "codex")),
    "S": (("gemini-3.8-flash", "medium", None),
          ("claude-haiku-5-5", "high", "claude"),
          ("gpt-6.1-terra", "medium", "codex")),
    "M": (("claude-sonnet-5-5", "medium", "claude"),
          ("claude-sonnet-5-5", "medium", "agy"),
          ("gpt-6.1-terra", "medium", "codex"),
          ("glm-5.3-flash", "max", "opencode")),
    "L": (("claude-sonnet-5-5", "high", "claude"),
          ("claude-sonnet-5-5", "high", "agy"),
          ("gpt-6.1-terra", "high", "codex"),
          ("gemini-4-argon", "medium", None)),
    "XL": (("claude-opus-5-5", "medium", "claude"),
           ("gpt-6.1-sol", "me¶»§q«^