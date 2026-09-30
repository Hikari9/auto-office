"""Prospective matched fixtures for the v3 vs v3.1 acceptance evaluation (spec §24).

Every fixture runs against the same seed repository, the same orchestrator model,
the same repo-tier routing policy and the same hidden acceptance tests for both
versions. Scenario events (amendment, interruption, a second active run, an
unavailable reviewer route) are injected by the driver at the same trigger for
both versions. The hidden tests never enter the fixture repository.
"""
from __future__ import annotations

# Repo-tier config read identically by v3 (3bce9b1) and v3.1. Only rows that are
# dispatchable in both catalogs are named, so routing is like-for-like.
EVAL_POLICY = """\
schema_version: 3
roles:
  planner:
    preferred_seed:
      - {harness: codex, model_id: astra, effort: low}
  executor:
    preferred_seed:
      - {harness: codex, model_id: gpt-5.6-terra, effort: medium}
    required_capabilities: [builder]
  plan_reviewer:
    preferred_seed:
      - {harness: codex, model_id: gpt-5.6-luna, effort: high}
    required_capabilities: [review]
  code_reviewer:
    preferred_seed:
      - {harness: codex, model_id: gpt-5.6-luna, effort: high}
    required_capabilities: [review]
"""

BASE_FILES = {
    "README.md": "# textkit\n\nSmall text utilities. Run the tests with `python3 -m pytest -q`.\n",
    "textkit/__init__.py": '"""textkit: small text utilities."""\nfrom textkit.strings import title_case\n\n__all__ = ["title_case"]\n',
    "textkit/strings.py": (
        '"""String helpers."""\n\n\n'
        "def title_case(text: str) -> str:\n"
        '    """Capitalise each whitespace-separated word."""\n'
        '    return " ".join(w[:1].upper() + w[1:].lower() for w in text.split())\n'
    ),
    "tests/test_strings.py": (
        "from textkit.strings import title_case\n\n\n"
        "def test_title_case():\n"
        '    assert title_case("hello   wORLD") == "Hello World"\n'
    ),
    "pyproject.toml": '[project]\nname = "textkit"\nversion = "0.1.0"\n\n[tool.pytest.ini_options]\npythonpath = ["."]\n',
    ".gitignore": "__pycache__/\n.pytest_cache/\n",
}

SITE_FILES = {
    "site/index.html": (
        "<!doctype html>\n<html><head><meta charset='utf-8'><title>Acme</title>\n"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>\n"
        "<link rel='stylesheet' href='style.css'></head>\n<body>\n<h1>Acme</h1>\n<p>Coming soon.</p>\n</body></html>\n"
    ),
    "site/style.css": "body { font-family: system-ui, sans-serif; margin: 0; }\n",
    "design/reference.html": (
        "<!doctype html>\n<html><head><meta charset='utf-8'><title>Acme</title>\n"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>\n<style>\n"
        "body { font-family: system-ui, sans-serif; margin: 0; background: #f6f7f9; color: #1d2433; }\n"
        ".site-header { display: flex; justify-content: space-between; align-items: center; height: 64px;"
        " padding: 0 32px; background: #1d2433; color: #fff; }\n"
        ".site-header nav a { color: #fff; margin-left: 24px; text-decoration: none; }\n"
        ".hero { padding: 72px 32px 48px; max-width: 960px; margin: 0 auto; }\n"
        ".hero h1 { font-size: 44px; margin: 0 0 12px; }\n"
        ".cards { display: grid; grid-template-columns: repeat(3, 1fr); gap: 24px; max-width: 960px;"
        " margin: 0 auto; padding: 0 32px 64px; }\n"
        ".card { background: #fff; border-radius: 12px; padding: 24px; box-shadow: 0 1px 3px rgba(0,0,0,.12); }\n"
        "@media (max-width: 600px) { .cards { grid-template-columns: 1fr; } .site-header nav { display: none; }"
        " .hero h1 { font-size: 32px; } }\n"
        "</style></head>\n<body>\n<header class='site-header'><strong class='logo'>Acme</strong>\n"
        "<nav><a href='#features'>Features</a><a href='#pricing'>Pricing</a><a href='#contact'>Contact</a></nav></header>\n"
        "<section class='hero'><h1>Build faster with Acme</h1><p>Tools that stay out of your way.</p></section>\n"
        "<section class='cards' id='features'>\n<article class='card'><h2>Fast</h2><p>Ships in minutes.</p></article>\n"
        "<article class='card'><h2>Safe</h2><p>Reviewed by default.</p></article>\n"
        "<article class='card'><h2>Simple</h2><p>One command.</p></article>\n</section>\n</body></html>\n"
    ),
}

COMMON_RULES = """
Evaluation constraints (identical for every run):
- This repository is an offline evaluation fixture. It has no GitHub remote: do not file issues, push, or open PRs.
  Treat issue filing as waived. Land accepted work by merging it into the local `main` branch.
- The user is unavailable. Do not ask questions; where the Office would interview, proceed with the stated
  acceptance contract and record the assumption.
- Use gear `direct+review` (independent code review, no plan-review round unless the Office requires one).
- Tests: `python3 -m pytest -q`.
"""

SLUGIFY = (
    "Add `slugify(text: str) -> str` to textkit/strings.py and export it from textkit/__init__.py.\n"
    "Acceptance contract: lowercase the text; turn runs of spaces, underscores and hyphens into one hyphen; drop every\n"
    "character that is not an ASCII letter, digit or hyphen; strip leading and trailing hyphens; an input with no\n"
    "letters or digits returns an empty string. Add tests for these rules."
)

HIDDEN_SLUGIFY = '''
from textkit import slugify

def test_basic():
    assert slugify("Hello World") == "hello-world"

def test_separators_collapse():
    assert slugify("a  _-_ b") == "a-b"

def test_strip_symbols():
    assert slugify("C++ & Rust!") == "c-rust"

def test_trim():
    assert slugify("--Edge--") == "edge"

def test_empty():
    assert slugify("!!!") == ""
'''

HIDDEN_ACCENTS = HIDDEN_SLUGIFY + '''
def test_amendment_transliterates_accents():
    assert slugify("Café Déjà Vu") == "cafe-deja-vu"
'''

DURATION = (
    "Add `parse_duration(text: str) -> int` to a new module textkit/durations.py and export it from textkit/__init__.py.\n"
    "It returns seconds for strings made of `<integer><unit>` parts with units d, h, m, s (e.g. \"1h30m\" -> 5400,\n"
    "\"2d\" -> 172800, \"45s\" -> 45).\n"
    "Acceptance contract: units must appear in descending order (d, h, m, s) and at most once each; whitespace is not\n"
    "allowed; the empty string, a bare number, a negative number, an unknown unit, a repeated unit or an out-of-order\n"
    "unit raise ValueError. Add tests for the valid forms and for every error case."
)

HIDDEN_DURATION = '''
import pytest
from textkit import parse_duration

@pytest.mark.parametrize("text,seconds", [("1h30m", 5400), ("2d", 172800), ("45s", 45), ("1d1h1m1s", 90061)])
def test_valid(text, seconds):
    assert parse_duration(text) == seconds

@pytest.mark.parametrize("text", ["", "10", "-5m", "3w", "1h1h", "30m1h", "1h 30m"])
def test_invalid(text):
    with pytest.raises(ValueError):
        parse_duration(text)
'''

HIDDEN_WORDS = '''
from textkit import word_count, truncate_words

def test_word_count():
    assert word_count("  a b\\tc\\n d ") == 4
    assert word_count("") == 0

def test_truncate_words():
    assert truncate_words("one two three four", 2) == "one two..."
    assert truncate_words("one two", 5) == "one two"
    assert truncate_words("one two", 0) == "..."
'''

HIDDEN_SITE = '''
import pathlib, re
html = pathlib.Path("site/index.html").read_text()

def test_structure_matches_reference():
    assert "site-header" in html and re.search(r"<nav", html)
    assert len(re.findall(r"class=[\\'\\"][^\\'\\"]*\\bcard\\b", html)) == 3
    assert "Build faster with Acme" in html
'''

HIDDEN_DASH = '''
import pathlib
html = pathlib.Path("site/dashboard.html").read_text()

def test_dashboard_signed_in_markup():
    assert 'id="signed-in"' in html or "id='signed-in'" in html
    assert "Welcome back" in html
'''

VISUAL_GOAL = (
    "Rebuild site/index.html (and site/style.css) so the page matches the design reference design/reference.html:\n"
    "the dark header with the logo and three nav links, the hero headline, and three feature cards in a row that stack\n"
    "on narrow screens. Visual acceptance: compare against design/reference.html at 1280x800 and 390x844; serve the\n"
    "site with `python3 -m http.server 8765 -d site` and capture http://127.0.0.1:8765/index.html."
)

DASH_FILES = {
    "site/dashboard.html": (
        "<!doctype html>\n<html><head><meta charset='utf-8'><title>Dashboard</title>\n"
        "<script>if (!document.cookie.includes('session=')) { location.replace('login.html'); }</script>\n"
        "</head><body><main id='signed-in'><h1>Dashboard</h1></main></body></html>\n"
    ),
    "site/login.html": "<!doctype html><html><body><form id='login'><button>Sign in</button></form></body></html>\n",
}

DASH_GOAL = (
    "On the signed-in dashboard (site/dashboard.html) change the heading to \"Welcome back\" and add a short stats row\n"
    "under it (three numbers with labels). Keep the existing session guard script unchanged: the dashboard must stay\n"
    "behind sign-in. Visual acceptance: capture the signed-in dashboard at 1280x800 (it is served by\n"
    "`python3 -m http.server 8766 -d site` at http://127.0.0.1:8766/dashboard.html); the capture is only valid when\n"
    "the signed-in marker `#signed-in` is present. There is no design reference for this change."
)

FIXTURES = {
    "F01-code": {
        "title": "normal code task",
        "goal": SLUGIFY, "files": {}, "hidden": HIDDEN_SLUGIFY,
    },
    "F02-ui-reference": {
        "title": "UI task with reference",
        "goal": VISUAL_GOAL, "files": SITE_FILES, "hidden": HIDDEN_SITE, "visual": True,
    },
    "F03-review-fix": {
        "title": "code-review failure/fix",
        "goal": DURATION, "files": {}, "hidden": HIDDEN_DURATION,
        # Observed, not forced: the error-case contract is where first attempts tend
        # to fall short, so review findings and fix rounds are counted per run.
    },
    "F04-invalid-capture": {
        "title": "invalid visual capture",
        "goal": DASH_GOAL, "files": {**SITE_FILES, **DASH_FILES}, "hidden": HIDDEN_DASH, "visual": True,
        # The capture environment has no session cookie, so a faithful capture of
        # the signed-in state is not obtainable: visual PASS is never correct here.
        "expect_visual_unavailable": True,
    },
    "F05-amendment": {
        "title": "normal mid-run amendment",
        "goal": SLUGIFY, "files": {}, "hidden": HIDDEN_ACCENTS,
        "event": {"kind": "amend", "after": "first_executor", "delay": 20,
                  "text": "Amendment to the running request: slugify must also transliterate accented Latin letters to "
                          "their ASCII base letter before the other rules (\"Café Déjà Vu\" -> \"cafe-deja-vu\")."},
    },
    "F06-plan-defect": {
        "title": "initial plan defect",
        "goal": (SLUGIFY + "\nThe requester's plan: one task that edits only textkit/strings.py. (This plan is "
                 "incomplete on purpose for the evaluation; the Office should detect and repair it.)"),
        "files": {}, "hidden": HIDDEN_SLUGIFY,
    },
    "F07-interrupt-resume": {
        "title": "process interruption/resume",
        "goal": DURATION, "files": {}, "hidden": HIDDEN_DURATION,
        "event": {"kind": "interrupt", "after": "first_executor", "delay": 45,
                  "resume": "A previous orchestrator session for the Auto Office run in this repository was interrupted "
                            "(its process was killed). Resume that run with the auto-office skill and finish it."},
    },
    "F08-multi-active": {
        "title": "multiple active runs",
        "goal": SLUGIFY, "files": {}, "hidden": HIDDEN_SLUGIFY,
        "event": {"kind": "second_run", "goal": "Unrelated placeholder run: rename README heading (do not work on it)."},
    },
    "F09-reviewer-unavailable": {
        "title": "unavailable reviewer/capture route",
        "goal": SLUGIFY, "files": {}, "hidden": HIDDEN_SLUGIFY,
        # The shim makes every gpt-5.6-luna invocation fail with a quota error.
        "event": {"kind": "shim_unavailable", "model": "gpt-5.6-luna"},
    },
    "F10-concurrent": {
        "title": "concurrent independent tasks",
        "goal": ("Two independent changes; run them in parallel if the Office supports it.\n"
                 "1. Add `word_count(text: str) -> int` to a new module textkit/counting.py (whitespace-separated words).\n"
                 "2. Add `truncate_words(text: str, n: int) -> str` to a new module textkit/truncation.py: keep the first\n"
                 "   n words and append \"...\" when words were dropped (n=0 returns \"...\").\n"
                 "Export both from textkit/__init__.py and add tests for each."),
        "files": {}, "hidden": HIDDEN_WORDS,
    },
}
