"""Integration: verify the actual composed result, not each green worktree.

When every task is accepted the runtime composes the accepted revisions onto
the run base in dependency order and runs the run-level checks on the composed
tree.

v3.1 contract: it also runs an integration review at a real boundary (Q7): a
task built on another's unmerged output, or two tasks that changed the same
file or share a declared interface.

Convergence contract (#337): those boundaries are lanes and shared scopes,
reviewed by office.convergence before integration starts; integration waits
until every scope converged (APPROVED, or waived by landing authority) and
adds no review of its own.
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from office import briefs, contract, db, gates, jobs, paths, planfile, review_parse, state, verification_coverage, worktree_setup
from office.util import dumps, now_iso, sha256_obj


DETAIL_LIMIT = 400


def blocked_detail(outcome):
    """The integration blocked line: the check summary, shortened in the middle so the trailing next step survives."""
    summary = outcome.get("summary") or ""
    if len(summary) > DETAIL_LIMIT:
        # The next step is the last "; " clause; elide the middle (usually the quoted command) instead.
        cut = summary.rfind("; ")
        tail = summary[cut:] if 0 < cut and len(summary) - cut <= DETAIL_LIMIT // 2 ¶»§q«^