### Starting a subsequent phase

Use `office start "Phase 2" --base origin/phase-one` to pin the next run to an existing named branch (including a PR branch). An explicit branch base is intentional and can contain unmerged commits; use `--no-prs` to disable task PRs when continuing locally. Without `--base`, Office continues to guard against accidentally starting a task-PR run on unrelated feature commits.
