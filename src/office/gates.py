"""Gate engine: revision-bound verdicts, selective invalidation, bounded
convergence, and the single acceptance evaluator.

Two review contracts (office.contract), chosen by the run's pin:

v3.1 â€” verdicts PASS | CHANGES_REQUIRED | PLAN_DEFECT | BRIEF_DEFECT |
UNAVAILABLE. Every task revision gets checks, an independent code review and,
when applicable, a visual review. Acceptance requires every required gate to
PASS on the task's current revision, the worker's applied version to be
current, and no open plan defect on the task's scope.

convergence-v1 (#337) â€” a task revision gets only its deterministic checks;
the executor's four-lens self-review is enforced by preflight. Independent
code and visual review happen once per ownership/composition lane on the
composed result (office.convergence). A checks gate records APPROVED (all
pass) or RECHECK (a failure) with review status COMPLETED; a check that cannot
run is UNAVAILABLE status with no verdict. Nothing unavailable, malformed,
stale, or missing is ever treated as approved.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
import uuid
from pathlib import Path

from office import briefs, candidates, contract, db, jobs, paths, planfile, review_parse, routing, state, version, worktree_setup
from office.result import Result
from office.state import Refused, Usage
from office.util import dumps, now_iso,¶»§q«^