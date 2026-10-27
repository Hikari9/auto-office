# Changelog

One line or short list per version, newest first. The version itself lives only in `src/office/_version.py` (see `AGENTS.md`).

## 3.7.1

- #809 conformance follow-up (#841): an M run executes a task inline only for one eligible low-risk task with a structured, strictly positive, beneficial total cost-to-success estimate; otherwise it delegates. The estimate parser rejects zero and negative costs. New-run help prefers XS-XL, and a historical gear alias prints a compatibility notice while keeping its pinned gates.
- Tests: the scripted-fake routing hides every agy row (Gemini as well as Claude), so a fake worker no longer runs as a real process that drains convergence jobs past an in-process monkeypatch (the self-review subagent block test failed about half the runs).

## 3.7.0

Rolls up everything landed on `main` since 3.6.6 (the version was not bumped per PR before this release).

- Routing: new-model trials on bounded production tasks, pin-loop fix, strict reviewer seed (#815); quoted override effort ceiling (#818); `routing_seed` no longer hashes `plan_version` (#820); seeded reviewer fallbacks cut by the trust floor are named (#825); Claude effort readback for the conformance probe (#832); no free-route substitution for an available paid preference.
- Catalog: GPT-6.1 Terra with effort-aware 6.1-over-5.6 Sol/Terra supersession (#811); `claude-opus-5-5@low` dispatchable on recorded probe evidence (#835); Pi/Kilo MiMo 2.6 Pro and Flash routes retired (#786).
- Runtime fixes: stalled orchestration recovers with current legal actions (#747); route authority preserved and accepted-task reroutes requalified (#745); squash landing provenance verified before closing runs (#748); runs anchored to durable Git repository roots (#746); fetched bases and dependency composition preserved (#743); review authority packets refreshed and foreign-scope repairs blocked (#742).
- Docs: Codex first-launch trust and hook-review screens (#807); reviewers on luna@high with opus@low the only fallback.
- Versioning: single source `src/office/_version.py`; `pyproject.toml` reads it dynamically; the `VERSION` file and manifest versions are removed; mandatory per-PR semver bump in `AGENTS.md`.
