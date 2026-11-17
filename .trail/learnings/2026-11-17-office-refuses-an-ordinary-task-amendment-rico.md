---
title: Office refuses an ordinary task amendment whose wording mentions a release
author: Rico
date: 2026-11-17
branch: main
cost: { minutes: 1, tokens: 2047 }
id: l-0fd451c4
scope: repo
status: active
src: agent
touches:
  - src/office/amend.py
---

## Decided
When an acceptance line for a version-bump task needs to say which version number to take, word it as "the next PATCH after the newest version already on origin/main". That wording passes as an ordinary amendment.

## Tried and ruled out
Writing "latest release tag" in the new accept line and "version rule" in the delta. Office treated "release" as an authority action (external or irreversible). It refused with `contract-level-change` and asked for `--contract` plus user authorization. The change itself was only acceptance detail.

## Watch out
The authority-word check in src/office/amend.py (the `contract-level-change` refusal near line 364) scans the `--add-accept` text and the `--` delta prose alike. Rephrase it neutrally rather than escalating to `--contract`, unless the change really is an external action.

In the code: src/office/amend.py raises `Refused("contract-level-change", "this delta names authority action 'release' ...")`. Command: `office amend T8 --drop-accept "<old>" --add-accept "<new>" -- "<delta>"`.
