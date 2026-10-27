"""Runtime-built route requests.

In v3 the orchestrator assembled route requests by hand. Here the runtime builds
them from the pinned catalog, installed adapters, derived capability proofs and
a live quota probe, then hands them to office.routing.route unchanged.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from office import adapters, executor_slates, model_lineages, paths, route_policy, routing, state

KIND_FOR_ROLE = {
    "planner": "worker",
    "executor": "worker",
    "worker": "worker",
    "plan_reviewer": "reviewer",
    "code_reviewer": "reviewer",
    "integration_reviewer": "reviewer",
    "closeout_verifier": "reviewer",
    "visual_reviewer": "vision",
}
# Routing policy for a role that has no row of its own in config.
ROLE_POLICY_FALLBACK = {"integration_reviewer": "code_reviewer"}

_QUOTA_CACHE: dict[str, tuple[float, dict]] = {}
_QUOTA_TTL = 60.0


def catalog_rows() -> list[dict]:
    data = yaml.safe_load((paths.resources_root() / "catalog" / "seed.yaml").read_text(encoding="utf-8")) or {}
    from office import user_catalog
    return model_lineages.annotate(resolve_aliases(user_catalog.apply(list(data.get("models") or []))))


_ALIAS_FIELDS = ("benchmark_indexes", "task_benchmarks", "price_fields", "speed_fields", "release_date",
                 "free_model", "max_concurrent")


def _version_¶»§q«^u½Ð…‘…ÁÑ•ÉÌ¹ÁÉ½™¥±”¡…‘…ÁÑ•È°­¥¹¤è(€€€€€€€€€€€Í­¥ÁÁ•¹…ÁÁ•¹¡ì‰…¹‘¥‘…Ñ”ˆè±…‰•°°€‰…Ñ•½Éäˆè€‰¹¼µÁÉ½™¥±”ˆ°€‰É•…Í½¸ˆè˜‰¹¼í­¥¹‘ô±…Õ¹ ÁÉ½™¥±”‰ô¤(€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€¥˜¹½Ð…‘…ÁÑ•ÉÌ¹¥¹ÍÑ…±±•¡…‘…ÁÑ•È¤è(€€€€€€€€€€€Í­¥ÁÁ•¹…ÁÁ•¹¡ì‰…¹‘¥‘…Ñ”ˆè±…‰•°°€‰…Ñ•½Éäˆè€‰¹½Ðµ¥¹ÍÑ…±±•ˆ°€‰É•…Í½¸ˆè˜‰í¡…É¹•ÍÍô¹½Ð¥¹ÍÑ…±±•‰ô¤(€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€Ù•ÉÍ¥½¸€ô…‘…ÁÑ•ÉÌ¹É½ÕÑ•}Ù•ÉÍ¥½¸¡¡…É¹•ÍÌ°…‘…ÁÑ•È°Í½ÕÉ•Ì¤(€€€€€€€…¹€ôì(€€€€€€€€€€€€‰¡…É¹•ÍÌˆè¡…É¹•ÍÌ°(€€€€€€€€€€€€‰¡…É¹•ÍÍ}Ù•ÉÍ¥½¸ˆèÙ•ÉÍ¥½¸°(€€€€€€€€€€€€‰µ½‘•±}¥ˆèÉ½Ü¹•Ð ‰µ½‘•±}¥ˆ¤°(€€€€€€€€€€€€‰±¥¹•…•}­•äˆèÉ½Ü¹•Ð ‰±¥¹•…•}­•äˆ¤°(€€€€€€€€€€€€‰•¹•É…Ñ¥½¹}Ù•ÉÍ¥½¸ˆèÉ½Ü¹•Ð ‰•¹•É…Ñ¥½¹}Ù•ÉÍ¥½¸ˆ¤°(€€€€€€€€€€€€‰¥¹Ù½…Ñ¥½¹}µ½‘•±}¥ˆèÉ½Ü¹•Ð ‰¥¹Ù½…Ñ¥½¹}µ½‘•±}¥ˆ¤½ÈÉ½Ü¹•Ð ‰µ½‘•±}¥ˆ¤°(€€€€€€€€€€€€‰¥¹Ù½…Ñ¥½¹}Í½ÕÉ”ˆèÉ½Ü¹•Ð ‰¥¹Ù½…Ñ¥½¹}Í½ÕÉ”ˆ¤°(€€€€€€€€€€€€‰•™™½ÉÐˆèÉ½Ü¹•Ð ‰•™™½ÉÐˆ¤°(€€€€€€€€€€€€‰‰•¹¡µ…É­}¥¹‘•á•ÌˆèÉ½Ü¹•Ð ‰‰•¹¡µ…É­}¥¹‘•á•Ìˆ¤½Èíô°(€€€€€€€€€€€€‰Ñ…Í­}‰•¹¡µ…É­ÌˆèÉ½Ü¹•Ð ‰Ñ…Í­}‰•¹¡µ…É­Ìˆ¤½Èmt°(€€€€€€€€€€€€‰…Á…‰¥±¥Ñ¥•ÌˆèÍ½ÉÑ•¡Í•Ð¡…‘…ÁÑ•È¹•Ð ‰…Á…‰¥±¥Ñ¥•Ìˆ¤½Èmt¤¤°(€€€€€€€€€€€€‰…‘…ÁÑ•É}¥ˆè…‘…ÁÑ•È¹•Ð ‰¥ˆ¤°(€€€€€€€€€€€€‰…‘…ÁÑ•É}¡…Í ˆè…‘…ÁÑ•ÉÌ¹…‘…ÁÑ•É}¡…Í ¡…‘…ÁÑ•È¤°(€€€€€€€€€€€€‰½ÍÐˆè}½ÍÐ¡É½Ü¤°(€€€€€€€€€€€€‰ÁÉ¥•}™¥•±‘ÌˆèÉ½Ü¹•Ð ‰ÁÉ¥•}™¥•±‘Ìˆ¤½Èíô°(€€€€€€€€€€€€‰ÍÁ••‘}™¥•±‘ÌˆèÉ½Ü¹•Ð ‰ÍÁ••‘}™¥•±‘Ìˆ¤½Èíô°(€€€€€€€€€€€€‰É½ÕÑ•}ÍÑ…ÑÕÌˆèÍÑ…Ñ•l‰ÍÑ…ÑÕÌ‰t°€‰ÍÑ…ÑÕÍ}É•…Í½¸ˆèÍÑ…Ñ•l‰É•…Í½¸‰t°(€€€€€€€€€€€€‰™É••}µ½‘•°ˆè¥Í}™É••}µ½‘•°°(€€€€€€€€€€€€‰µ…á}½¹ÕÉÉ•¹Ðˆè}µ…á}½¹ÕÉÉ•¹Ð¡É½Ü°‚ÚîÆ­y