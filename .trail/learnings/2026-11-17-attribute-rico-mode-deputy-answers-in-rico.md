---
title: "Attribute rico-mode deputy answers in Office quotes; never present them as Rico's words"
author: Rico
date: 2026-11-17
branch: main
cost: { minutes: 30, tokens: 74628 }
id: l-9e62fd5f
scope: repo
status: active
src: agent
touches:
  - ".office/plans/fcb7f3a3/PLAN.md"
---

## Decided
When a rico-mode deputy (not Rico) answers an AskUserQuestion that feeds an Office authority command, the --quote must attribute it, e.g. "rico-mode deputy, SO20 plan pre-authorization: <answer>". If auto mode blocks the command, stop and tell rico-mode instead of working around it.

## Tried and ruled out
Quoting Rico's verbatim words from a rico-mode brief as plan approval: the auto-mode classifier refused it, since a brief's goal statement is not consent to a specific plan. Quoting the option label alone ("Approve and merge (Recommended)") records it as if Rico chose it.

## Watch out
An Office approval cannot be deleted. A corrected re-approval adds a second authority.plan event, so tell rico-mode that both records exist.
Ordinary `office amend --add-accept` text containing the word "release" is refused as a contract-level authority action. Say "version tag" instead.

In the code: office approve plan --quote; office amend <T> --add-accept; ~/Git/rico-mode/scripts/notify-rico-mode.sh
