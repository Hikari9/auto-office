"""Issue #268 (run a9afacbf throughput): append-only registries several tasks
edit are declared `shared:` and never serialize or defect those tasks; check
suites share a host-wide concurrency cap instead of all running at once."""
from __future__ import annotations

import threading
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
