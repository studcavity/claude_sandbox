---
type: Reference
title: "Commit messages carry point-in-time state"
description: "The Verified, Not verified and Follow-up lines a commit message should hold; read before writing commit messages for work that was or was not tested."
tags: [git, commits]
generated: { by: "claude-code/claude-sonnet-5-5", at: "2026-11-19T01:06:00Z" }
sources:
  - id: check
    resource: https://github.com/studcavity/claude_sandbox/blob/main/claude_sandbox/portable/check.py
    title: The commit linter behind sandbox-check
---

A commit message is the durable place for what is true *now* but does not belong in
code or docs: what you verified and how, what you could **not** verify or do (and
why), and follow-ups someone must action later.[^check]

- Use `Verified:` / `Not verified:` lines, and a `Follow-up:` line per item (what to
  do, where, why it was blocked).
- Find open follow-ups with `git log --grep='Follow-up' -n 20`. Close one in the
  commit that does it: `Resolves follow-up from <short-sha>: ...`.
- Never rewrite pushed history to edit one.
- Files that need a real run (code, scripts, config) need a `Verified` or
  `Not verified` line; `sandbox-check` lints for it. How to run it and what else a
  branch needs before you finish is in [Handing your work back](handing-back.md).
- A follow-up that is more than a line belongs in a
  [follow-up request](follow-up-requests.md), not the commit message.

[^check]: The commit linter behind sandbox-check
