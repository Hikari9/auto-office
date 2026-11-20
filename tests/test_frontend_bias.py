"""AA v5 frontend bias: a frontend-heavy task moves its size's agy gemini-* rung to rung 1 for that task only.

Pure unit tier over executor_slates plus one routing.route decision. The bias reorders routes already on the slate:
it adds no candidate and lifts no gate."""
import pytest

from office import executor_slates as slates, routing

FRONTEND = ["src/app/Button.tsx", "src/app/Card.jsx", "src/app/Nav.vue", "src/app/Panel.svelte", "src/app/a.css",
            "src/app/b.scss", "src/app/c.sass", "src/app/d.less", "src/app/index.html", "src/app/Hero.astro"]


def _paths(frontend: int, total: int) -> list[str]:
    return [f"web/f{i}.tsx" for i in range(frontend)] + [f"src/office/m{i}.py" for i in range(total - frontend)]


def test_listed_extensions_are_exactly_the_frontend_set():
    assert slates.FRONTEND_EXTENSIONS == (".tsx", ".jsx", ".vue", ".svelte", ".css", ".scss", ".sass", ".less",
                                          ".html", ".astro")
    assert slates.frontend_reason({"domain": "mixed"}, FRONTEND)
    assert slates.frontend_reason({}, ["src/app/Button.TSX"]), "extensions match case-insensitively"
    assert slates.frontend_reason({}, ["src/app/main.ts", "src/app/util.js", "docs/readme.md"]) is None


@pytest.mark.parametrize("frontend, total, heavy", [
    (59, 100, False),   # 59% is below the line
    (60, 100, True),    # 60% is on it
    (3, 5, True),       # 60% exactly
    (2, 5, False),      # 40%
    (1, 1, True),
    (0, 4, False),
])
def test_sixty_percent_path_boundary(frontend, total, heavy):
    assert bool(slates.frontend_reason({"domain": "mixed"}, _paths(frontend, total))) is heavy


@pytest.mark.parametrize("domain", ["mixed", "backend", None])
def test_non_ui_domains_are_judged_on_paths_alone(domain):
    assert slates.frontend_reason({"domain": domain}, _paths(6, 10))
    assert slates.frontend_reason({"domain": domain}, _paths(5, 10)) is None


def test_ui_domain_is_frontend_heavy_whatever_the_paths():
    assert slates.frontend_reason({"domain": "ui"}, ["src/office/m.py"]) == "descriptor domain is ui"
    assert slates.frontend_reason({"domain": "ui"}, None)


def test_shared_marker_blank_and_duplicate_paths_do_not_skew_the_ratio():
    scope = ["+web/a.tsx", "web/a.tsx", "", "  ", "src/office/m.py", "+src/office/n.py"]
    assert slates.frontend_reason({}, scope) is None  # 1 frontend of 3 distinct paths
    assert slates.frontend_reason({}, []) is None
    assert slates.frontend_reason({}, None) is None


@pytest.mark.parametrize("size, first, order", [
    ("XS", ("agy", "gemini-4-hydrogen", "low"),
     [("agy", "gemini-4-hydrogen", "low"), ("claude", "claude-haiku-5-5", "high"), ("codex", "gpt-6.1-terra", "low")]),
    ("S", ("agy", "gemini-4-hydrogen", "medium"),
     [("agy", "gemini-4-hydrogen", "medium"), ("claude", "claude-sonnet-5-5", "medium"),
      ("codex", "gpt-6.1-terra", "medium")]),
    ("M", ("agy", "gemini-4-hydrogen", "high"),
     [("agy", "gemini-4-hydrogen", "high"), ("claude", "claude-sonnet-5-5", "high"), ("codex", "gpt-6.1-terra", "high")]),
    ("L", ("agy", "gemini-4-neon", "medium"),
     [("agy", "gemini-4-neon", "medium"), ("claude", "claude-opus-5-5", "medium"), ("codex", "gpt-6.2-sol", "medium")]),
    ("XL", ("agy", "gemini-4-neon", "high"),
     [("agy", "gemini-4-neon", "high"), ("claude", "claude-opus-5-5", "high"), ("codex", "gpt-6.2-sol", "high")]),
])
def test_bias_moves_the_gemini_rung_to_rung_one_and_adds_no_route(size, first, order):
    seed = slates.ideal_seed(size)
    biased = slates.frontend_bias(seed, {"domain": "ui"}, [])
    assert [(r["harness"], r["model_id"], r["effort"]) for r in biased] == order
    assert (biased[0]["harness"], biased[0]["model_id"], biased[0]["effort"]) == first
    assert [r["rung"] for r in biased] == [1, 2, 3]
    assert biased[0]["frontend_bias"] == "descriptor domain is ui"
    assert sorted(map(str, ((r["harness"], r["model_id"], r["effort"]) for r in biased))) == sorted(
        map(str, ((r["harness"], r["model_id"], r["effort"]) for r in seed))), "no route added or removed"
    assert all("frontend_bias" not in r for r in biased[1:])


def test_bias_leaves_a_non_frontend_task_and_the_shared_slate_alone():
    seed = slates.ideal_seed("S")
    snapshot = [dict(r) for r in seed]
    assert slates.frontend_bias(seed, {"domain": "backend"}, _paths(1, 4)) is None
    assert slates.frontend_bias(seed, {"domain": "ui"}, []) is not None
    assert seed == snapshot, "the bias returns a new list: the shipped slate is never mutated"
    assert slates.ideal_seed("S") == snapshot, "the next task starts from the unbiased slate"


def test_bias_without_a_gemini_rung_does_nothing():
    seed = [{"model_id": "claude-sonnet-5-5", "effort": "high", "harness": "claude", "rung": 1},
            {"model_id": "gpt-6.1-terra", "effort": "high", "harness": "codex", "rung": 2}]
    assert slates.frontend_bias(seed, {"domain": "ui"}, []) is None
    assert slates.frontend_bias([], {"domain": "ui"}, []) is None


def test_the_disclosure_says_why_and_that_no_gate_moved():
    biased = slates.frontend_bias(slates.ideal_seed("L"), {}, _paths(6, 10))
    chosen = {"model_id": "gemini-4-neon", "harness": "agy", "effort": "medium", "quota": {"status": "ok"}}
    reason = routing.selection_disclosure("executor", chosen, biased, "balanced")["reason"]
    assert "frontend-heavy task (6 of 10 declared paths are frontend files)" in reason
    assert "agy/gemini-4-neon@medium moved to rung 1 for this task only" in reason
    assert "no route added, no gate lifted" in reason
    plain = routing.selection_disclosure("executor", chosen, slates.ideal_seed("L"), "balanced")["reason"]
    assert "frontend-heavy" not in plain

