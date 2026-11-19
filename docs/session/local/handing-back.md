---
type: Procedure
title: "Handing your work back (local backend)"
description: "What to do before you finish: synopsis in the last commit, sandbox-check, and what to list in your final message."
tags: [review, finish]
generated: { by: "claude-code/claude-sonnet-5-5", at: "2026-11-19T01:06:00Z" }
sources:
  - id: local
    resource: https://github.com/studcavity/claude_sandbox/blob/main/claude_sandbox/backends/local.py
    title: The local backend
---

# Trigger

You are about to finish a branch, hand control back, or end the session.

# Steps

1. **Propose a synopsis in your last commit** of each branch: one
   `PR-Title: <what the branch does, under 80 chars>` line and a few
   `PR-Summary: <point>` lines (what and why; one line each). The launcher shows the
   title, sanitised, in the report the user reviews.[^local]
2. **Run `sandbox-check`** (no arguments). It lints your commits since the base
   (`todo/` files, no `fixup!` or merge commits, no credential-shaped text, a
   `Verified` or `Not verified` line where files need a real run). Fix with new
   commits, or rebase your own branch. The launcher runs it again on what it imports;
   it is advisory, the user decides.
3. **Say if a branch needs privileged review.** Branches that change `.github/`,
   `.githooks/`, `CODEOWNERS`, the deps or host files or a `setup:` script are flagged
   to the user, since those run with extra privilege.
4. **List your branch names and what each contains** in your final message.

There are no PRs and no CI: the user reviews and merges your branches themselves.
Prefer independent branches from the base.

[^local]: The local backend
