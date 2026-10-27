"""Derived routing: harness@version x model x effort.

Ported from the v3 `scripts/office_routing.py`. Trust, floors and local reward
are derived from runs.db evidence and the pinned role floor; a candidate's own
claims about them are read nowhere. The quota reserve comes from the pinned
policy (5% by default), never a literal.

Executor and worker routes (#300) keep stages 1-6 as qualification, then
`office.adaptive` compares the qualifying set and returns a slate of up to
three. Planner and reviewer routes keep the legacy stages 7-9, including the
balanced money band, until their own routing is redesigned.
"""
from __future__ import annotations

import math
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from office import adaptive, executor_slates, route_learning, route_policy, scoring
from office.util import sha256_obj

# Roles whose selection confers mutable or gate authority; they need derived trust.
MUTABLE_TRUST_ROLES = {"executor", "code_reviewer", "browser_verifier", "closeout_verifier", "visual_reviewer",
                       "integration_reviewer"}
DEFAULT_RESERVE_PERCENT = 5.0
# Legacy planner/reviewer routing only. Executor and worker routing (#300) has no
# money band; its cost safety valve is routing.adaptive.budget_ceiling_multiple.
DEFAULT_MONEY_BAND_PERCENT = 20.0
ADAPTIVE_ROLES = set(route_learning.ADAPTIVE_ROLES)

_REQUIRED_OVERRIDE_FIELDS = (
    "override_id", "run_id", "family_id", "task_id", "role", "candidat¶»§q«^u•¹Ğˆ¤¥Ì9½¹”è(€€€€€€€€€€€€€€€Õ¹­¹½İ¸¹…ÁÁ•¹¡Œ¤ì½¹Ñ¥¹Õ”(€€€€€€€€€€€É•µ…¥¹¥¹œ€ô™±½…Ğ¡Ål‰Ñ¥¡Ñ•ÍÑ}É•µ…¥¹¥¹}Á•É•¹Ğ‰t¤ì‰ÕÉ¸€ô™±½…Ğ¡Ä¹•Ğ ‰ÁÉ½©•Ñ•‘}‰ÕÉ¹}Á•É•¹Ğˆ¤½È€À¤(€€€€€€€€€€€€¡Í…™”¥˜É•µ…¥¹¥¹œ€´‰ÕÉ¸€øôÉ•Í•ÉÙ”•±Í”Õ¹Í…™”¤¹…ÁÁ•¹¡Œ¤(€€€€€€€¥˜¹½Ğ€¡Í…™”½ÈÕ¹­¹½İ¸½ÈÕ¹Í…™”¤è(€€€€€€€€€€€}É•©•Ñ}Á½½°¡Á½½°°É•©•Ñ•°€‰¹¼­¹½İ¸µİ½É­¥¹œÉ½ÕÑ”ÅÕ…±¥™¥•°Í¼¹¼™…±±‰…¬•á¥ÍÑÌˆ¤(€€€€€€€€€€€É•ÑÕÉ¸ì‰Í•±•Ñ•ˆè9½¹”°€‰ÍÑ…ÑÕÌˆè€‰¹½}ÅÕ…±¥™å¥¹}…¹‘¥‘…Ñ”ˆ°€‰É•©•Ñ•ˆèÉ•©•Ñ•‘ô(€€€€€€€¥˜Í…™”è(€€€€€€€€€€€™½ÈŒ¥¸Õ¹Í…™”è(€€€€€€€€€€€€€€€É•©•Ñ•¹…ÁÁ•¹¡ì‰…¹‘¥‘…Ñ”ˆè…¹‘¥‘…Ñ•}¥¡Œ¤°€‰ÍÑ…”ˆè€Ø°€‰É•…Í½¸ˆè€‰ÁÉ½©•Ñ•ÅÕ½Ñ„É½ÍÍ•ÌÉ•Í•ÉÙ”İ¡¥±”Í…™”…±Ñ•É¹…Ñ¥Ù”•á¥ÍÑÌ‰ô¤(€€€€€€€€€€€ÍÑ…”€ôÍ…™”€¬Õ¹­¹½İ¸(€€€€€€€•±¥˜Õ¹­¹½İ¸è(€€€€€€€€€€€™½ÈŒ¥¸Õ¹Í…™”è(€€€€€€€€€€€€€€€É•©•Ñ•¹…ÁÁ•¹¡ì‰…¹‘¥‘…Ñ”ˆè…¹‘¥‘…Ñ•}¥¡Œ¤°€‰ÍÑ…”ˆè€Ø°€‰É•…Í½¸ˆè€‰­¹½İ¸ÅÕ½Ñ„É½ÍÍ•ÌÉ•Í•ÉÙ”ì½¹±äÕ¹­¹½İ¸…¹‘¥‘…Ñ•ÌÉ•µ…¥¸‰ô¤(€€€€€€€€€€€ÍÑ…”€ôÕ¹­¹½İ¸(€€€€€€€•±Í”è(€€€€€€€€€€€}É•©•Ñ}Á½½°¡Á½½°°É•©•Ñ•°€‰ÁÉ½Ñ•Ñ•ÅÕ½Ñ„‰±½­Ì•Ù•ÉäÅÕ…±¥™å¥¹œÉ½ÕÑ”ˆ¤(€€€€€€€€€€€É•ÑÕÉ¸ì(€€€€€€€€€€€€€€€€‰Í•±•Ñ•ˆè9½¹”°€‰ÍÑ…ÑÕÌˆè€‰ÁÉ½Ñ•Ñ•‘}ÅÕ½Ñ…}İ½Õ±‘}‰•}½¹ÍÕµ•ˆ°€‰É•©•Ñ•ˆèÉ•©•Ñ•°(€€€€€€€€€€€€€€€€‰…Ñ¥½¸ˆè€‰¡½½Í”„Íµ…±±•È½¡•…Á•ÈÙ…±¥ÍÑÉ…Ñ•ä°ÁÉ½Á½Í”…¹½Ñ¡•ÈÉ½ÕÑ”°½È½‰Ñ…¥¸•áÁ±¥¥ĞÕÍ•È…ÕÑ¡½É¥Ñäˆ°(€€€€€€€€€€€ô((€€€€€€€¥˜…‘…ÁÑ¥Ù•}É½±”è(€€€€€€€€€€€É•ÑÕÉ¸}…‘…ÁÑ¥Ù”¡É•ÅÕ•ÍĞ°É½±”°Á±…å‰½½¬°Á½±¥ä°ÍÑ…”°É•©•Ñ•°•±¥¥‰¥±¥Ñä°±•…É¹•°(€€€€€€€€€€€€€€€€€€€€€€€€€€€€ÁÉ•™•ÉÉ•‘}Í••°…±±½İ}…‘Ù¥ÎÚîÆ­y