"""#1168: Hydrogen shadow-review scorecard matching and metrics (research lane, no network)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import yaml

LANE = Path(__file__).resolve().parents[1] / "research" / "hydrogen-shadow"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"hydrogen_shadow_{name}", LANE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


scorecard = _load("scorecard")
shadow = _load("shadow_review")

TRUTH = {"file": "src/a.py", "line": 40, "keywords": ["race", "transaction"]}


def _result(root: Path, case: str, reviewer: str, findings: list, status: str = "ok") -> None:
    d = root / "results" / case
    d.mkdir(parents=True, exist_ok=True)
    verdict = "RECHECK" if any(f["severity"] in ("blocker", "major") for f in findings) else "APPROVED"
    (d / f"{reviewer}.json").write_text(json.dumps({"status": status, "verdict": verdict, "findings": findings}))


def test_truth_match_by_line_window_or_keyword():
    assert scorecard.matches_truth({"file": "src/a.py", "line": 45, "summary": "x"}, TRUTH)
    assert not scorecard.matches_truth({"file": "src/a.py", "line": 46, "summary": "x"}, TRUTH)
    assert not scorecard.matches_truth({"file": "src/b.py", "line": 40, "summary": "x"}, TRUTH)
    assert scorecard.matches_truth({"file": "src/a.py", "line": 90, "summary": "Lost update: RACE on book"}, TRUTH)


def test_keyword_in_another_file_is_not_a_catch():
    assert not scorecard.matches_truth({"file": "tests/t.py", "line": 40, "summary": "a race in the fixture"}, TRUTH)
    truth = {"file": "src/jobs.py", "line": 94, "keywords": ["test"], "must_mention": ["OFFICE_KICK"]}
    assert not scorecard.matches_truth({"file": "src/other.py", "line": 3, "summary": "no test for rename"}, truth)
    assert scorecard.matches_truth({"file": "src/jobs.py", "line": 300, "summary": "OFFICE_KICK override unchecked"}, truth)


def test_failed_planted_run_is_a_miss_and_blocks_promotion(tmp_path):
    (tmp_path / "planted" / "p1").mkdir(parents=True)
    (tmp_path / "planted" / "p1" / "truth.yaml").write_text(yaml.safe_dump(TRUTH))
    hit = {"severity": "major", "file": "src/a.py", "line": 40, "summary": "race"}
    _result(tmp_path, "p1", "luna", [hit])
    _result(tmp_path, "p1", "hydrogen", [], status="quota-blocked")
    for i in range(10):
        _result(tmp_path, f"pr-{i}", "luna", [])
        _result(tmp_path, f"pr-{i}", "hydrogen", [])
    s = scorecard.score(scorecard.load(tmp_path))
    assert (s["hydrogen"]["caught"], s["hydrogen"]["planted"], s["hydrogen"]["planted_failed"]) == (0, 1, 1)
    assert s["hydrogen"]["recall"] == 0.0
    ok, why = scorecard.promotion(s)
    assert not ok and "planted run failed for hydrogen" in why
    assert "(0/1, 1 failed)" in scorecard.render(s)


def test_blocker_agreement_needs_same_region_or_both_clean():
    a = {"findings": [{"severity": "major", "file": "f.py", "line": 10}]}
    near = {"findings": [{"severity": "blocker", "file": "f.py", "line": 20}]}
    far = {"findings": [{"severity": "blocker", "file": "f.py", "line": 21}]}
    minor_only = {"findings": [{"severity": "minor", "file": "f.py", "line": 10}]}
    assert scorecard.blocker_agree(a, near)
    assert not scorecard.blocker_agree(a, far)
    assert not scorecard.blocker_agree(a, minor_only)
    assert scorecard.blocker_agree(minor_only, {"findings": []})


def test_scores_recall_fp_agreement_escapes_and_holds_promotion(tmp_path):
    (tmp_path / "planted" / "p1").mkdir(parents=True)
    (tmp_path / "planted" / "p1" / "truth.yaml").write_text(yaml.safe_dump(TRUTH))
    hit = {"severity": "major", "file": "src/a.py", "line": 41, "summary": "cap read outside the tx"}
    noise = {"severity": "minor", "file": "src/c.py", "line": 3, "summary": "rename"}
    _result(tmp_path, "p1", "luna", [hit])
    _result(tmp_path, "p1", "hydrogen", [hit, noise])
    _result(tmp_path, "pr-9", "luna", [])
    _result(tmp_path, "pr-9", "hydrogen", [], status="quota-blocked")
    (tmp_path / "dispositions.yaml").write_text(yaml.safe_dump({"p1": {"hydrogen": {1: "reject"}}}))
    (tmp_path / "escapes.yaml").write_text(yaml.safe_dump([{"case": "pr-9", "missed_by": ["luna"]}]))

    s = scorecard.score(scorecard.load(tmp_path))
    assert (s["luna"]["caught"], s["luna"]["planted"], s["luna"]["fp_rate"]) == (1, 1, 0.0)
    assert (s["hydrogen"]["caught"], s["hydrogen"]["rejected"], s["hydrogen"]["findings"]) == (1, 1, 2)
    assert s["hydrogen"]["agreement"] == 1.0 and s["hydrogen"]["compared"] == 1
    assert s["hydrogen"]["failed"] == ["pr-9: quota-blocked"]
    assert (s["luna"]["escapes"], s["hydrogen"]["escapes"]) == (1, 0)
    ok, why = scorecard.promotion(s)
    assert not ok and "live PRs" in why
    assert "Decision now: hold" in scorecard.render(s)


def test_missing_planted_result_is_a_failed_miss(tmp_path):
    for case in ("p1", "p2"):
        (tmp_path / "planted" / case).mkdir(parents=True)
        (tmp_path / "planted" / case / "truth.yaml").write_text(yaml.safe_dump(TRUTH))
    hit = {"severity": "major", "file": "src/a.py", "line": 40, "summary": "race"}
    _result(tmp_path, "p1", "luna", [hit])
    _result(tmp_path, "p1", "hydrogen", [hit])
    _result(tmp_path, "p2", "luna", [hit])  # hydrogen never ran p2
    for i in range(10):
        _result(tmp_path, f"pr-{i}", "luna", [])
        _result(tmp_path, f"pr-{i}", "hydrogen", [])
    s = scorecard.score(scorecard.load(tmp_path))
    assert (s["hydrogen"]["caught"], s["hydrogen"]["planted"], s["hydrogen"]["planted_failed"]) == (1, 2, 1)
    assert s["hydrogen"]["failed"] == ["p2: missing"]
    assert (s["luna"]["caught"], s["luna"]["planted"]) == (2, 2)
    ok, why = scorecard.promotion(s)
    assert not ok and "planted run failed for hydrogen" in why


def test_promotion_requires_lower_fp_and_equal_recall():
    base = {"recall": 1.0, "fp_rate": 0.2, "escapes": 0, "pending": 0, "live_prs": 10}
    assert scorecard.promotion({"luna": base, "hydrogen": {**base, "fp_rate": 0.1}})[0]
    assert not scorecard.promotion({"luna": base, "hydrogen": {**base, "fp_rate": 0.2}})[0]
    assert not scorecard.promotion({"luna": base, "hydrogen": {**base, "fp_rate": 0.1, "recall": 0.8}})[0]
    assert not scorecard.promotion({"luna": base, "hydrogen": {**base, "fp_rate": 0.1, "pending": 1}})[0]


def test_reply_parser_takes_last_verdict_object():
    reply = 'thinking {"note": 1}\n{"verdict": "APPROVED", "findings": []}\nthen {"verdict": "RECHECK", ' \
            '"findings": [{"severity": "major", "file": "a", "line": 1, "summary": "s {x}"}]}'
    assert shadow.parse(reply)["verdict"] == "RECHECK"
    assert shadow.parse("no json here") is None


def test_planted_cases_are_complete():
    cases = sorted(p.name for p in (LANE / "planted").iterdir() if p.is_dir())
    assert len(cases) == 5
    classes = set()
    for c in cases:
        truth = yaml.safe_load((LANE / "planted" / c / "truth.yaml").read_text())
        assert (LANE / "planted" / c / "change.diff").read_text().startswith("diff --git")
        assert truth["file"] and truth["line"] and truth["keywords"]
        classes.add(truth["class"])
    assert classes == {"logic", "auth", "off-by-one", "missing-test", "race"}
