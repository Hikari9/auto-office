"""Issue #268 (run a9afacbf throughput): append-only registries several tasks
edit are declared `shared:` and never serialize or defect those tasks; check
suites share a host-wide concurrency cap instead of all running at once."""
from __future__ import annotations

import threading

import pytest
import time

from conftest import GOOD_ADD, GOOD_MUL, PLAN_TWO, approved_run
from office import briefs, gates, planfile

SHARED_PLAN = PLAN_TWO.replace("scope: calc.py\n", "scope: calc.py\nshared: REGISTRY.md\n").replace(
    "scope: mul.py\n", "shared: REGISTRY.md\nscope: mul.py\n")


def test_shared_key_marks_scope_entries_in_either_order():
    plan = planfile.parse(SHARED_PLAN)
    assert not plan.errors, plan.errors
    scopes = {t["id"]: t["scope"] for t in plan.tasks}
    assert scopes == {"T1": ["calc.py", "+REGISTRY.md"], "T2": ["mul.py", "+REGISTRY.md"]}, scopes
    assert not any("overlap" in w for w in plan.warnings), plan.warnings


def test_shared_entries_only_skip_overlap_against_each_other():
    assert not planfile.scopes_overlap(["a.py", "+reg.ts"], ["b.py", "+reg.ts"])
    assert planfile.scopes_overlap(["a.py", "+reg.ts"], ["reg.ts"])  # exclusive owner still conflicts
    assert planfile.scopes_overlap(["+src/**"], ["src/x.py"])
    assert planfile.path_in_scope("reg.ts", ["a.py", "+reg.ts"])
    assert planfile.path_in_scope("src/lib/x.ts", ["+src/lib/**"])


def test_plan_review_format_exempts_shared_registries():
    assert "never double-scope-ownership" in briefs.PLAN_REVIEW_FORMAT


def test_tasks_sharing_a_registry_run_in_parallel_and_accept(env):
    approved_run(env, plan=SHARED_PLAN,
                 executor=[{"write_by_task": {"T1": {"calc.py": GOOD_ADD, "REGISTRY.md": "add\n"},
                                              "T2": {"mul.py": GOOD_MUL}}, "submit": True}],
                 code_reviewer=[{"reply": "VERDICT: PASS"}])
    code, out = env.office("dispatch", "T1", "T2", "--parallel")
    assert code == 0 and "scope-held" not in out, out
    code, data = env.ojson("status")
    assert data["data"]["tasks"] == {"T1": "accepted", "T2": "accepted"}, data


def test_check_concurrency_defaults_to_a_quarter_of_the_cpus(monkeypatch):
    monkeypatch.setattr(gates.os, "cpu_count", lambda: 10)
    assert gates.check_concurrency({}) == 2
    assert gates.check_concurrency({"check_concurrency": 0}) == 2
    assert gates.check_concurrency({"check_concurrency": 5}) == 5
    monkeypatch.setattr(gates.os, "cpu_count", lambda: 2)
    assert gates.check_concurrency({}) == 1


def test_check_slot_serializes_beyond_the_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(gates.paths, "state_home", lambda: tmp_path)
    events: list[str] = []

    def hold(name: str, seconds: float) -> None:
        with gates.check_slot(1, poll=0.02):
            events.append(f"{name}+")
            time.sleep(seconds)
            events.append(f"{name}-")

    first = threading.Thread(target=hold, args=("a", 0.3))
    first.start()
    time.sleep(0.1)
    second = threading.Thread(target=hold, args=("b", 0))
    second.start()
    first.join()
    second.join()
    assert events == ["a+", "a-", "b+", "b-"], events


def test_check_slot_admits_up_to_the_limit_at_once(tmp_path, monkeypatch):
    monkeypatch.setattr(gates.paths, "state_home", lambda: tmp_path)
    inside = threading.Barrier(2, timeout=5)

    def hold() -> None:
        with gates.check_slot(2, poll=0.02):
            inside.wait()  # both must be inside together or this times out

    threads = [threading.Thread(target=hold) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not inside.broken


def test_executor_brief_explains_shared_entries_and_asks_for_a_structured_report():
    packet = {"task_id": "T1", "title": "x", "scope": ["a.py", "+reg.ts"], "plan_version": 1, "requirements_version": 1}
    brief = briefs.executor_brief(None, {}, packet)
    assert "SHARED (+)" in brief and "FINAL REPORT" in brief and "mutation" in brief, brief
    plain = briefs.executor_brief(None, {}, {**packet, "scope": ["a.py"]})
    assert "SHARED (+)" not in plain


def test_directory_scope_entries_own_their_tree():
    # #334: `src/auth/` (and bare `src/auth`) cover the files inside them.
    assert planfile.path_in_scope("src/auth/rock-user-resolver.ts", ["src/auth/"])
    assert planfile.path_in_scope("tests/auth/x/y.test.ts", ["tests/auth"])
    # Review F4: brackets in a directory entry are path characters (Next.js dynamic routes).
    assert planfile.path_in_scope("src/app/[slug]/page.tsx", ["src/app/[slug]/"])
    assert planfile.path_in_scope("src/app/(dash)/x/page.tsx", ["src/app/(dash)"])
    # A shared directory owns its tree (plan validation allows it only for ordered tasks).
    assert planfile.path_in_scope("src/reg/a.ts", ["+src/reg/"])
    assert not planfile.path_in_scope("src/authz/x.ts", ["src/auth/"])
    assert not planfile.path_in_scope("src/authz.ts", ["src/auth"])


def test_scope_and_shared_entries_with_notes_are_plan_errors():
    # #416: a note riding on an entry made it a literal that never matched, so the
    # executor's submit was refused for the very file the amendment granted.
    plan = planfile.parse(PLAN_TWO.replace(
        "scope: calc.py\n",
        "scope: calc.py, vitest.config.ts (A3: only to append the include)\n"
        "shared: pnpm-lock.yaml (A4: shared with T2; T1 edits only its importer)\n"))
    bad = [e for e in plan.errors if "not a path or glob" in e]
    assert len(bad) == 3, plan.errors
    assert any("vitest.config.ts (A3" in e for e in bad) and all("accept:" in e for e in bad)
    assert not planfile.parse(PLAN_TWO.replace("scope: calc.py\n", "scope: calc.py, src/a/**, +x.md\n")).errors


def test_entry_validation_allows_route_groups_and_rejects_shared_trees_and_colons():
    ok = planfile.parse(PLAN_TWO.replace("scope: calc.py\n", "scope: calc.py, src/app/(dashboard)/**, src/app/[slug]/\n"))
    assert not ok.errors, ok.errors  # review F1
    bad = planfile.parse(PLAN_TWO.replace("scope: calc.py\n", "scope: calc.py, vitest.config.ts:append-only\n"))
    assert any("vitest.config.ts:append-only" in e for e in bad.errors), bad.errors  # review F10


def test_an_entry_the_accepted_plan_already_had_is_a_warning_on_revision():
    # Review F5: a run accepted before entry validation can still be amended.
    text = PLAN_TWO.replace("scope: calc.py\n", "scope: calc.py\nshared: pnpm-lock.yaml (A4: shared)\n")
    first = planfile.parse(text)
    assert first.errors
    prev = [{"id": "T1", "scope": ["calc.py", "+pnpm-lock.yaml (A4: shared)"]}]
    again = planfile.parse(text)
    planfile.grandfather_entries(again, prev)
    assert not again.errors and any("already had it" in w for w in again.warnings), (again.errors, again.warnings)
    added = planfile.parse(text.replace("scope: calc.py\n", "scope: calc.py, x.ts (new note)\n"))
    planfile.grandfather_entries(added, prev)
    assert any("x.ts (new" in e for e in added.errors), added.errors


def test_planner_template_allows_globs_and_has_no_trailing_note_style():
    # Review F9.
    line = next(ln for ln in briefs.PLAN_FORMAT.splitlines() if ln.startswith("scope:"))
    assert "bare paths only" not in line and "paths/globs" in line


SHARED_TREE = PLAN_TWO.replace("scope: calc.py\n", "scope: calc.py\nshared: src/reg/\n").replace(
    "scope: mul.py\n", "scope: mul.py\nshared: src/reg/\n")


def test_a_shared_directory_between_sequential_tasks_is_accepted():
    # User decision: a shared directory is fine when its tasks never run in parallel.
    plan = planfile.parse(SHARED_TREE.replace("scope: mul.py\nshared: src/reg/\ndepends: none",
                                              "scope: mul.py\nshared: src/reg/\ndepends: T1"))
    deps = {t["id"]: t["depends"] for t in plan.tasks}
    assert deps == {"T1": [], "T2": ["T1"]}, deps
    assert not plan.errors, plan.errors


def test_a_shared_directory_between_parallel_tasks_is_a_plan_error():
    plan = planfile.parse(SHARED_TREE)
    errs = [e for e in plan.errors if "share the directory" in e]
    assert len(errs) == 1 and "T1 and T2" in errs[0] and "'src/reg/'" in errs[0], plan.errors
    # An exclusive path inside the shared tree is the same conflict.
    mixed = planfile.parse(PLAN_TWO.replace("scope: calc.py\n", "scope: calc.py\nshared: src/reg/\n").replace(
        "scope: mul.py\n", "scope: mul.py, src/reg/x.ts\n"))
    assert any("share the directory" in e for e in mixed.errors), mixed.errors
    # Shared files keep today's append-only parallel behaviour.
    assert not planfile.parse(PLAN_TWO.replace("scope: calc.py\n", "scope: calc.py\nshared: REG.md\n").replace(
        "scope: mul.py\n", "scope: mul.py\nshared: REG.md\n")).errors


def test_tasks_sharing_a_directory_never_hold_leases_together(env):
    # If a later amendment drops the ordering, the lease guard still refuses to run both at once.
    approved_run(env, plan=PLAN_TWO, executor=[{"sleep": 0}])
    con = env.con()
    con.execute("UPDATE tasks SET scope_json=? WHERE id IN ('T1','T2')", ('["+src/reg/"]',))
    con.commit()
    assert planfile.scopes_overlap(["+src/reg/"], ["+src/reg/"])
    code, out = env.office("dispatch", "T1", env={"OFFICE_WORKER_LAUNCHER": "external"})
    assert code == 0, out
    code, out = env.office("dispatch", "T2", env={"OFFICE_WORKER_LAUNCHER": "external"})
    assert code != 0 and "scope-held" in out and "T1" in out, out


def test_a_bare_shared_directory_is_a_tree_for_the_plan_check_and_the_lease_guard():
    # Review #447 F1: `shared: src/reg` (no trailing slash) escaped both checks.
    plan = planfile.parse(SHARED_TREE.replace("shared: src/reg/\n", "shared: src/reg\n"))
    assert any("share the directory 'src/reg'" in e for e in plan.errors), plan.errors
    assert planfile.scopes_overlap(["+src/reg"], ["+src/reg"])


def test_a_shared_file_glob_stays_parallel_safe():
    # Review #447 F8: `locales/*.json` names append-only files, not a directory.
    plan = planfile.parse(PLAN_TWO.replace("scope: calc.py\n", "scope: calc.py\nshared: locales/*.json\n").replace(
        "scope: mul.py\n", "scope: mul.py\nshared: locales/*.json\n"))
    assert not plan.errors, plan.errors


def test_bracket_directories_are_literal_for_overlap_and_matching():
    # Review #447 F3 and F4.
    assert not planfile.scopes_overlap(["+src/app/[slug]/"], ["src/app/(dash)/page.tsx"])
    plan = planfile.parse(PLAN_TWO.replace("scope: calc.py\n", "scope: calc.py\nshared: src/app/[slug]/\n").replace(
        "scope: mul.py\n", "scope: mul.py, src/app/(dash)/page.tsx\n"))
    assert not any("share the directory" in e for e in plan.errors), plan.errors
    assert planfile.path_in_scope("src/app/[slug]/page.tsx", ["src/app/[slug]/*.tsx"])
    assert not planfile.path_in_scope("src/app/s/page.tsx", ["src/app/[slug]/page.tsx"])


def test_a_note_glued_to_a_path_is_still_an_entry_error():
    # Review #447 F9: parentheses only as whole segments.
    plan = planfile.parse(PLAN_TWO.replace("scope: calc.py\n", "scope: calc.py\nshared: vitest.config.ts(append-only)\n"))
    assert any("vitest.config.ts(append-only)" in e for e in plan.errors), plan.errors


def test_a_parallel_shared_directory_the_accepted_plan_had_is_grandfathered():
    # Review #447 F2 and F10: grandfathering works from structure, pairs included.
    first = planfile.parse(SHARED_TREE)
    assert first.errors and first.pair_problems
    again = planfile.parse(SHARED_TREE)
    planfile.grandfather_entries(again, first.tasks)
    assert not again.errors and any("already had it" in w for w in again.warnings), (again.errors, again.warnings)
    quoted = PLAN_TWO.replace("scope: calc.py\n", "scope: calc.py, it's (\"odd\") note\n")
    old = planfile.parse(quoted)
    assert old.errors
    redo = planfile.parse(quoted)
    planfile.grandfather_entries(redo, old.tasks)
    assert not redo.errors, redo.errors


@pytest.mark.parametrize("entry,is_dir", [
    ("+src", True), ("+scripts", True), ("+.github", True), ("+docker/Dockerfile", False),
    ("+Makefile", False), ("+locales/*.json", False), ("+src/reg/", True), ("+REG.md", False),
])
def test_shared_entries_are_classed_as_files_or_directories(entry, is_dir):
    # R3-6: one classifier for the depends rule, the lease guard and matching.
    assert planfile.shared_tree(entry) is is_dir
    bare = entry.lstrip("+").rstrip("/")
    assert planfile.path_in_scope(bare + "/a.ts", [entry]) is is_dir
    assert planfile.scopes_overlap([entry], [entry]) is is_dir


def test_two_parallel_tasks_sharing_src_are_a_plan_error():
    plan = planfile.parse(PLAN_TWO.replace("scope: calc.py\n", "scope: calc.py\nshared: src\n").replace(
        "scope: mul.py\n", "scope: mul.py\nshared: src\n"))
    assert any("share the directory 'src'" in e for e in plan.errors), plan.errors


def test_nextjs_optional_catch_all_and_stacked_intercepting_segments_are_paths():
    # R3-7.
    plan = planfile.parse(PLAN_TWO.replace(
        "scope: calc.py\n", "scope: calc.py, src/app/[[...slug]]/page.tsx, src/app/(..)(..)photo/page.tsx\n"))
    assert not plan.errors, plan.errors
    assert planfile._entry_problem("x.csv(A3)")


def test_a_new_shared_directory_on_a_grandfathered_pair_is_still_an_error():
    # R3-8: grandfathering is per (pair, entry), and every shared directory is reported.
    accepted = planfile.parse(SHARED_TREE)
    revised = SHARED_TREE.replace("shared: src/reg/\n", "shared: src/reg/, src/other/\n")
    plan = planfile.parse(revised)
    planfile.grandfather_entries(plan, accepted.tasks)
    assert any("'src/other/'" in e for e in plan.errors), (plan.errors, plan.warnings)


def test_an_entry_error_cites_its_own_line():
    # R3-10.
    text = PLAN_TWO.replace("scope: calc.py\n", "scope: calc.py\nshared: vitest.config.ts (note)\n")
    line = next(i for i, ln in enumerate(text.splitlines(), start=1) if ln.startswith("shared: vitest"))
    plan = planfile.parse(text)
    assert any(f"(line {line})" in e and "vitest.config.ts (note)" in e for e in plan.errors), plan.errors


@pytest.mark.parametrize("entry", ["+src/profile", "+content/authors", "+docs/changelog", "+public/.well-known"])
def test_suffixless_and_dot_directories_below_the_root_are_directories(entry):
    # R4-4: these are real directories; classing them as files took their contents out of scope.
    assert planfile.shared_tree(entry)
    assert planfile.path_in_scope(entry.lstrip("+") + "/a.md", [entry])


@pytest.mark.parametrize("entry,is_dir", [
    # PR #492 review item 7
    ("+README", False), ("+LICENSE", False), ("+src/Makefile", False), ("+docs/README", True),
    # item 5: common files that were classed as directories
    ("+.npmignore", False), ("+.gitkeep", False), ("+.coveragerc", False), ("+.flake8", False),
    ("+.pylintrc", False), ("+.nojekyll", False), ("+docs/CODEOWNERS", False), ("+Readme", False),
    ("+License", False),
])
def test_review_492_file_and_directory_probes(entry, is_dir):
    assert planfile.shared_tree(entry) is is_dir


@pytest.mark.parametrize("entry", ["+.vercel", "+.terraform", "+.astro", "+.netlify", "+.aws", "+.tox", "+.output",
                                   "+.pytest_cache", "+src/.generated"])
def test_unlisted_dot_directories_are_directories(entry):
    # a071c74 re-verify item 3: an extensionless dot-name is a directory unless it is a known dotfile.
    assert planfile.shared_tree(entry)
    assert planfile.path_in_scope(entry.lstrip("+") + "/project.json", [entry])
