"""The executor's self-review findings ledger: what the brief tells the executor to write, what
`office preflight` demands of it, and that submit consumes it without counting it as scope."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from conftest import GOOD_ADD, approved_run, task_row

ROOT = Path(__file__).resolve().parents[2]
EXTERNAL = {"OFFICE_WORKER_LAUNCHER": "external"}
TASK = {"scope": ["calc.py", "tests/**"], "accept": ["calc.add(2, 3) == 5", "no CLI"]}
LENSES = ("security", "edge-cases", "platform", "test-strength")


def _git(wt: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(wt), *args], check=True, capture_output=True, text=True).stdout.strip()


def ledger_text(head: str, *, rnd: int = 1, findings=(), skip=(), drop=()) -> str:
    lines = [f"COMMIT {head}", f"ROUND {rnd}"]
    for lens in LENSES:
        if lens in drop:
            continue
        lines.append(f"LENS {lens} skipped {skip[lens] if isinstance(skip, dict) else 'n/a'}" if lens in skip
                     else f"LENS {lens} reviewed")
    return "\n".join(lines + list(findings)) + "\n"


def write_ledger(wt: Path, text: str | None = None, **kw) -> Path:
    from office import briefs
    path = wt / briefs.LEDGER_FILE
    path.write_text(text if text is not None else ledger_text(_git(wt, "rev-parse", "HEAD"), **kw))
    return path


@pytest.fixture
def repo(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    _git(wt, "init", "-q")
    _git(wt, "config", "user.email", "t@example.test")
    _git(wt, "config", "user.name", "t")
    (wt / "README.md").write_text("x\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-qm", "base")
    (wt / "calc.py").write_text(GOOD_ADD)  # an in-scope change, so a ledger is owed
    (wt / "tests").mkdir()
    (wt / "tests" / "test_calc.py").write_text("def test_x():\n    pass\n")
    return wt


def verdict(wt: Path, task=TASK, changed=()):
    from office import preflight
    return preflight.ledger_verdict(wt, task, list(changed), _git(wt, "rev-parse", "HEAD"))


def fixes(wt: Path, text: str, **kw) -> list[str]:
    write_ledger(wt, text)
    stop, fix = verdict(wt, **kw)
    assert not stop, stop
    return fix


# ------------------------------------------------------------------ when a ledger is owed

def test_no_ledger_with_a_nonempty_diff_is_a_fix(repo):
    stop, fix = verdict(repo)
    assert stop == [] and len(fix) == 1 and "no OFFICE_SELF_REVIEW.md" in fix[0], fix


@pytest.mark.parametrize("how", ["committed", "tracked-edit", "untracked"])
def test_every_kind_of_in_scope_change_owes_a_ledger(repo, how):
    (repo / "calc.py").unlink()
    (repo / "tests" / "test_calc.py").unlink()
    (repo / "tests").rmdir()
    assert verdict(repo) == ([], [])
    if how == "committed":
        (repo / "calc.py").write_text(GOOD_ADD)
        _git(repo, "add", "calc.py")
        _git(repo, "commit", "-qm", "calc")
        stop, fix = verdict(repo, changed=["calc.py"])
    elif how == "tracked-edit":
        (repo / "calc.py").write_text(GOOD_ADD)
        _git(repo, "add", "calc.py")
        _git(repo, "commit", "-qm", "calc")
        (repo / "calc.py").write_text(GOOD_ADD + "\n")
        stop, fix = verdict(repo)
    else:
        (repo / "calc.py").write_text(GOOD_ADD)
        stop, fix = verdict(repo)
    assert stop == [] and any("no OFFICE_SELF_REVIEW.md" in f for f in fix), (how, fix)


def test_empty_diff_needs_no_ledger(repo):
    (repo / "calc.py").unlink()
    (repo / "tests" / "test_calc.py").unlink()
    assert verdict(repo) == ([], [])


def test_scope_none_needs_no_ledger_even_with_changes(repo):
    assert verdict(repo, task={"scope": [], "accept": []}) == ([], [])


def test_only_out_of_scope_or_harness_changes_need_no_ledger(repo):
    (repo / "calc.py").unlink()
    (repo / "tests" / "test_calc.py").unlink()
    (repo / "notes.txt").write_text("x\n")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text("{}")
    assert verdict(repo, task={"scope": ["calc.py", ".claude/**"], "accept": []}) == ([], [])


def test_the_ledger_alone_is_not_a_change(repo):
    (repo / "calc.py").unlink()
    (repo / "tests" / "test_calc.py").unlink()
    write_ledger(repo, "garbage\n")
    assert verdict(repo, task={"scope": ["**"], "accept": []}) == ([], [])


# ------------------------------------------------------------------ ready, stale, lenses

def test_all_dispositions_resolved_is_ready(repo):
    findings = [
        "FINDING high calc.py:3 | add overflows | fixed tests/test_calc.py",
        "FINDING medium calc.py:4 | add drops negatives | fixed tests/test_calc.py",
        "FINDING low calc.py:5 | naming | fixed",
        "FINDING medium README.md:1 | doc drift | out-of-scope",
        "FINDING low calc.py:6 | false positive | rejected the call never happens",
    ]
    write_ledger(repo, ledger_text(_git(repo, "rev-parse", "HEAD"), findings=findings, skip={"platform": "pure python"}))
    assert verdict(repo) == ([], [])


def test_a_ledger_with_no_findings_is_ready(repo):
    write_ledger(repo)
    assert verdict(repo) == ([], [])


def test_blank_lines_and_indentation_are_fine(repo):
    head = _git(repo, "rev-parse", "HEAD")
    write_ledger(repo, "\n  COMMIT " + head + "\n\nROUND 1\n" + "".join(f"  LENS {x} reviewed\n" for x in LENSES))
    assert verdict(repo) == ([], [])


def test_ledger_naming_a_stale_commit_is_a_fix(repo):
    stale = _git(repo, "rev-parse", "HEAD")
    write_ledger(repo)
    (repo / "calc.py").write_text(GOOD_ADD)
    _git(repo, "add", "calc.py")
    _git(repo, "commit", "-qm", "later")
    stop, fix = verdict(repo)
    head = _git(repo, "rev-parse", "HEAD")
    assert stop == [] and len(fix) == 1, fix
    assert f"names commit {stale[:12]} but HEAD is {head[:12]}" in fix[0] and "stale" in fix[0], fix


@pytest.mark.parametrize("fmt", ["short", "upper"])
def test_commit_may_be_an_abbreviation_in_either_case(repo, fmt):
    head = _git(repo, "rev-parse", "HEAD")
    write_ledger(repo, ledger_text(head[:7] if fmt == "short" else head.upper()))
    assert verdict(repo) == ([], [])


def test_a_wrong_sha_that_looks_valid_is_stale(repo):
    stop, fix = verdict(write_ledger(repo, ledger_text("0" * 40)).parent)
    assert any("but HEAD is" in f for f in fix), fix


@pytest.mark.parametrize("lens", LENSES)
def test_a_lens_missing_without_a_skip_reason_is_a_fix(repo, lens):
    write_ledger(repo, drop=(lens,))
    stop, fix = verdict(repo)
    assert stop == [] and len(fix) == 1 and f"lens {lens} has no line" in fix[0], fix


def test_a_skipped_lens_needs_its_reason(repo):
    head = _git(repo, "rev-parse", "HEAD")
    text = ledger_text(head, drop=("platform",)) + "LENS platform skipped\n"
    fix = fixes(repo, text)
    assert any("line 6" in f and "LENS needs" in f for f in fix), fix
    assert any("lens platform has no line" in f for f in fix), fix


# ------------------------------------------------------------------ findings

@pytest.mark.parametrize("severity", ["high", "medium", "low"])
def test_an_open_finding_is_a_fix_naming_it(repo, severity):
    head = _git(repo, "rev-parse", "HEAD")
    fix = fixes(repo, ledger_text(head, findings=[f"FINDING {severity} calc.py:9 | add skips zero | open"]))
    assert len(fix) == 1 and f"{severity} calc.py:9 add skips zero is open" in fix[0], fix


def test_a_medium_fixed_without_a_test_path_is_a_fix(repo):
    head = _git(repo, "rev-parse", "HEAD")
    for sev in ("medium", "high"):
        fix = fixes(repo, ledger_text(head, findings=[f"FINDING {sev} calc.py:9 | add skips zero | fixed"]))
        assert len(fix) == 1 and "without a test path" in fix[0] and "calc.py:9" in fix[0], fix


@pytest.mark.parametrize("path", ["tests/missing.py", "../outside.py", "/etc/hosts", "tests"])
def test_a_fixed_medium_must_name_a_real_test_file_inside_the_worktree(repo, path, tmp_path):
    (tmp_path / "outside.py").write_text("x\n")
    head = _git(repo, "rev-parse", "HEAD")
    fix = fixes(repo, ledger_text(head, findings=[f"FINDING medium calc.py:9 | add skips zero | fixed {path}"]))
    assert len(fix) == 1 and "is not a file in this worktree" in fix[0], fix


def test_a_low_fix_may_omit_its_test(repo):
    head = _git(repo, "rev-parse", "HEAD")
    assert fixes(repo, ledger_text(head, findings=["FINDING low calc.py:9 | nit | fixed"])) == []


# ------------------------------------------------------------------ stops

def test_contract_conflict_stops_with_the_accept_line_quoted(repo):
    head = _git(repo, "rev-parse", "HEAD")
    write_ledger(repo, ledger_text(head, findings=["FINDING high calc.py:2 | add must reject ints | contract-conflict accept=2"]))
    stop, fix = verdict(repo)
    assert fix == [] and len(stop) == 1, (stop, fix)
    assert 'would break ACCEPT 2: "no CLI"' in stop[0] and "calc.py:2" in stop[0], stop


def test_contract_conflict_naming_no_accept_line_is_a_fix(repo):
    head = _git(repo, "rev-parse", "HEAD")
    fix = fixes(repo, ledger_text(head, findings=["FINDING high calc.py:2 | x | contract-conflict accept=3"]))
    assert len(fix) == 1 and "accept=3" in fix[0] and "2 ACCEPT lines" in fix[0], fix


@pytest.mark.parametrize("sev, kind", [("medium", "stop"), ("high", "stop"), ("low", "fix")])
def test_round_three_with_an_open_finding(repo, sev, kind):
    head = _git(repo, "rev-parse", "HEAD")
    write_ledger(repo, ledger_text(head, rnd=3, findings=[f"FINDING {sev} calc.py:9 | add skips zero | open"]))
    stop, fix = verdict(repo)
    if kind == "stop":
        assert fix == [] and len(stop) == 1 and "round 3 ended with a finding still open" in stop[0], (stop, fix)
        assert f"{sev} calc.py:9 add skips zero" in stop[0]
    else:
        assert stop == [] and len(fix) == 1, (stop, fix)


@pytest.mark.parametrize("rnd", [1, 2])
def test_an_open_blocker_before_round_three_is_a_fix_not_a_stop(repo, rnd):
    head = _git(repo, "rev-parse", "HEAD")
    write_ledger(repo, ledger_text(head, rnd=rnd, findings=["FINDING medium calc.py:9 | x | open"]))
    stop, fix = verdict(repo)
    assert stop == [] and len(fix) == 1, (stop, fix)


def test_round_three_with_everything_resolved_is_ready(repo):
    head = _git(repo, "rev-parse", "HEAD")
    write_ledger(repo, ledger_text(head, rnd=3, findings=["FINDING high calc.py:9 | x | fixed tests/test_calc.py"]))
    assert verdict(repo) == ([], [])


# ------------------------------------------------------------------ malformed lines

MALFORMED = [
    "# a comment",
    "NOTE all fine",
    "FINDING",
    "FINDING medium calc.py:1",
    "FINDING medium calc.py:1 | no disposition",
    "FINDING critical calc.py:1 | s | open",
    "FINDING Medium calc.py:1 | s | open",
    "FINDING medium calc.py:1 and more | s | open",
    "FINDING medium  | s | open",
    "FINDING medium calc.py:1 |  | open",
    "FINDING medium calc.py:1 | s | maybe",
    "FINDING medium calc.py:1 | s | open now",
    "FINDING medium calc.py:1 | s | fixed a.py b.py",
    "FINDING medium calc.py:1 | s | rejected",
    "FINDING medium calc.py:1 | s | contract-conflict",
    "FINDING medium calc.py:1 | s | contract-conflict accept=0",
    "FINDING medium calc.py:1 | s | contract-conflict accept=x",
    "COMMIT",
    "COMMIT nothex1",
    "COMMIT abc",
    "ROUND 0",
    "ROUND 4",
    "ROUND two",
    "ROUND ²",
    "LENS",
    "LENS security",
    "LENS security done",
    "LENS security reviewed extra",
    "LENS crypto reviewed",
    "lens security reviewed",
]


@pytest.mark.parametrize("bad", MALFORMED)
def test_a_malformed_line_is_a_fix_with_its_line_number_and_never_ready(repo, bad):
    head = _git(repo, "rev-parse", "HEAD")
    text = ledger_text(head) + bad + "\n"
    n = text.splitlines().index(bad) + 1
    fix = fixes(repo, text)
    assert any(f.startswith(f"ledger line {n}:") for f in fix), (bad, fix)


@pytest.mark.parametrize("dup", ["COMMIT {head}", "ROUND 2", "LENS security reviewed", "LENS security skipped why"])
def test_a_repeated_header_or_lens_line_is_malformed(repo, dup):
    head = _git(repo, "rev-parse", "HEAD")
    text = ledger_text(head) + dup.format(head=head) + "\n"
    n = len(text.splitlines())
    assert any(f.startswith(f"ledger line {n}:") for f in fixes(repo, text))


def test_malformed_lines_do_not_hide_a_stop(repo):
    head = _git(repo, "rev-parse", "HEAD")
    text = ledger_text(head, findings=["junk", "FINDING high calc.py:2 | s | contract-conflict accept=1"])
    write_ledger(repo, text)
    stop, fix = verdict(repo)
    assert len(stop) == 1 and any("line 7" in f for f in fix), (stop, fix)


def test_every_malformed_line_is_listed_not_only_the_first(repo):
    head = _git(repo, "rev-parse", "HEAD")
    fix = fixes(repo, ledger_text(head, findings=["junk one", "junk two"]))
    assert [f for f in fix if "unknown line" in f] and len([f for f in fix if "unknown line" in f]) == 2, fix


@pytest.mark.parametrize("text", ["", "\n\n", "   \n"])
def test_an_empty_ledger_is_never_ready(repo, text):
    fix = fixes(repo, text)
    assert any("no COMMIT line" in f for f in fix) and any("no ROUND line" in f for f in fix), fix
    assert sum("has no line" in f for f in fix) == 4, fix


def test_a_ledger_with_findings_but_no_header_is_never_ready(repo):
    fix = fixes(repo, "FINDING low calc.py:1 | s | fixed\n")
    assert fix, "a header-less ledger must not be ready"


# ------------------------------------------------------------------ unsafe or oversized files

def test_a_symlinked_ledger_is_refused_not_read(repo, tmp_path):
    from office import briefs
    secret = tmp_path / "secret.txt"
    secret.write_text(ledger_text(_git(repo, "rev-parse", "HEAD")))
    os.symlink(secret, repo / briefs.LEDGER_FILE)
    stop, fix = verdict(repo)
    assert stop == [] and len(fix) == 1 and "regular untracked file" in fix[0], fix


def test_a_hard_linked_ledger_is_refused(repo, tmp_path):
    from office import briefs
    other = tmp_path / "other.txt"
    other.write_text(ledger_text(_git(repo, "rev-parse", "HEAD")))
    os.link(other, repo / briefs.LEDGER_FILE)
    stop, fix = verdict(repo)
    assert len(fix) == 1 and "regular untracked file" in fix[0], fix


def test_a_committed_ledger_is_refused(repo):
    from office import briefs
    write_ledger(repo)
    _git(repo, "add", briefs.LEDGER_FILE)
    _git(repo, "commit", "-qm", "oops")
    stop, fix = verdict(repo)
    assert len(fix) == 1 and "git rm --cached" in fix[0], fix


def test_an_oversized_ledger_is_refused_not_truncated(repo):
    from office import briefs
    head = _git(repo, "rev-parse", "HEAD")
    pad = "FINDING low calc.py:1 | " + "x" * 200 + " | fixed\n"
    write_ledger(repo, ledger_text(head) + pad * (briefs.LEDGER_MAX_CHARS // len(pad) + 5))
    stop, fix = verdict(repo)
    assert len(fix) == 1 and "over" in fix[0] and "characters" in fix[0], fix


# ------------------------------------------------------------------ brief, skill, SKILL.md agree

RULES = ("OFFICE_SELF_REVIEW.md", "severity as found", "do not trigger a re-review", "fix-diff re-review",
         "3-round cap", "contract-conflict", "consumes", "ACCEPT line")


def _brief(tier: str) -> str:
    from office import briefs
    gear, risk = {"inline": ("direct", '{"blast_radius": "local"}'), "single": ("quick", '{"blast_radius": "local"}'),
                  "deep": ("full", '{"blast_radius": "production"}')}[tier]
    packet = {"task_id": "T1", "title": "x", "scope": ["a.py"], "plan_version": 1, "requirements_version": 1,
              "base_commit": "abc123"}
    return briefs.executor_brief(None, {"gear": gear, "risk_json": risk}, packet)


@pytest.mark.parametrize("tier", ["inline", "single", "deep"])
def test_brief_self_review_block_states_the_ledger_and_the_round_rules(tier):
    from office import briefs
    brief = _brief(tier)
    block = brief[brief.index("SELF-REVIEW before submitting"):brief.index("WHEN DONE run: office preflight")]
    for rule in RULES:
        assert rule in block, (tier, rule)
    for needle in ("COMMIT <full sha of HEAD>", "ROUND <1-3>", "LENS <security|edge-cases|platform|test-strength> reviewed",
                   "LENS <lens> skipped <reason>", "FINDING <high|medium|low> <file:line> | <summary> | <disposition>",
                   "write it fresh every round", "after your last commit"):
        assert needle in block, (tier, needle)
    for disposition in briefs.LEDGER_DISPOSITIONS:
        assert f"`{disposition}" in block, (tier, disposition)
    assert "subagent" not in block.lower() or tier != "inline"


def test_fix_round_brief_carries_the_same_ledger_block():
    from office import briefs
    packet = {"task_id": "T1", "title": "x", "scope": ["a.py"], "plan_version": 1, "requirements_version": 1,
              "base_commit": "abc123", "fix_of": "R1"}

    class _Con:
        def execute(self, *a, **k):
            class _R:
                def fetchall(self_inner):
                    return []
            return _R()

    brief = briefs.executor_brief(_Con(), {"id": "r", "gear": "full", "risk_json": None}, packet)
    assert "FIX ROUND for revision R1" in brief
    for rule in RULES:
        assert rule in brief, rule


def test_office_submit_step_2_and_the_hub_state_the_same_rules_as_the_brief():
    skill = (ROOT / "skills/office-submit/SKILL.md").read_text()
    step = skill[skill.index("## 2. Adversarial self-review"):skill.index("## 3. Checks")]
    hub = (ROOT / "SKILL.md").read_text()
    line = hub[hub.index("- Executors simplify"):hub.index("- Before submitting a plan inline")]
    for name, text in (("office-submit step 2", step), ("SKILL.md executor line", line)):
        flat = " ".join(text.split()).lower()
        for rule in RULES:
            assert rule.lower() in flat, (name, rule)
        assert "preflight" in flat, name
    for disposition in ("fixed <test path>", "out-of-scope", "rejected <reason>", "contract-conflict accept=<n>", "open"):
        assert disposition in step, disposition


# ------------------------------------------------------------------ through the real preflight and submit

def _dispatch(env, plan=None):
    kw = {"plan": plan} if plan else {}
    approved_run(env, executor=[{}], code_reviewer=[{"reply": "VERDICT: PASS"}], **kw)
    env.office("dispatch", "T1", env=EXTERNAL, check=0)
    con = env.con()
    t = task_row(env, "T1")
    d = dict(con.execute("SELECT * FROM dispatches WHERE id=?", (t["current_dispatch_id"],)).fetchone())
    wenv = {"OFFICE_RUN_ID": d["run_id"], "OFFICE_DISPATCH_ID": d["id"], "OFFICE_TASK_ID": "T1",
            "OFFICE_ROLE": "executor", "OFFICE_JOBS": "manual"}
    wt = Path(d["worktree"])
    (wt / "calc.py").write_text(GOOD_ADD)
    return wenv, wt, d


@pytest.mark.integration
@pytest.mark.approved
def test_preflight_walks_missing_stale_open_then_ready_and_submit_consumes_the_ledger(env):
    from office import briefs
    wenv, wt, d = _dispatch(env)
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 1 and f"fix: ledger: no {briefs.LEDGER_FILE}" in out, out
    head = _git(wt, "rev-parse", "HEAD")
    write_ledger(wt, ledger_text("0" * 40))
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 1 and "ledger: names commit" in out, out
    write_ledger(wt, ledger_text(head, findings=["FINDING medium calc.py:1 | add drops negatives | open"]))
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 1 and "medium calc.py:1 add drops negatives is open" in out, out
    write_ledger(wt, ledger_text(head, findings=["FINDING medium calc.py:1 | add drops negatives | fixed calc.py"]))
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 0 and out.startswith("PREFLIGHT ready"), out
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code == 0, out
    assert "warning" not in out and briefs.LEDGER_FILE not in out, out
    assert not (wt / briefs.LEDGER_FILE).exists(), "submit consumes the ledger"
    rev = dict(env.con().execute("SELECT * FROM revisions WHERE task_id='T1'").fetchone())
    assert briefs.LEDGER_FILE not in _git(wt, "ls-tree", "-r", "--name-only", rev["commit_sha"])


@pytest.mark.integration
@pytest.mark.approved
def test_preflight_contract_conflict_is_a_stop_quoting_the_accept_line(env):
    wenv, wt, d = _dispatch(env)
    write_ledger(wt, ledger_text(_git(wt, "rev-parse", "HEAD"),
                                 findings=["FINDING high calc.py:1 | add must be float | contract-conflict accept=1"]))
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 4 and out.startswith("PREFLIGHT stop") and 'ACCEPT 1: "calc.add(2, 3) == 5"' in out, out


@pytest.mark.integration
def test_a_ledger_inside_a_wide_scope_still_stays_out_of_the_revision(env):
    from conftest import PLAN_ONE
    from office import briefs
    wenv, wt, d = _dispatch(env, plan=PLAN_ONE.replace("scope: calc.py", "scope: *"))
    write_ledger(wt)
    code, out = env.office("preflight", cwd=wt, env=wenv)
    assert code == 0, out
    code, out = env.office("submit", cwd=wt, env=wenv)
    assert code == 0 and "warning" not in out, out
    rev = dict(env.con().execute("SELECT * FROM revisions WHERE task_id='T1'").fetchone())
    assert _git(wt, "ls-tree", "-r", "--name-only", rev["commit_sha"]).split().count("calc.py") == 1
    assert briefs.LEDGER_FILE not in _git(wt, "ls-tree", "-r", "--name-only", rev["commit_sha"])
    assert not (wt / briefs.LEDGER_FILE).exists()
