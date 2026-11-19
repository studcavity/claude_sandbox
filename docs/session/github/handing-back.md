---
type: Procedure
title: "Handing your work back (GitHub backend)"
description: "What to do before you finish: PR synopsis in the last commit, sandbox-check, held branches, and what to list in your final message."
tags: [pull-requests, finish]
generated: { by: "claude-code/claude-sonnet-5-5", at: "2026-11-19T01:06:00Z" }
sources:
  - id: land
    resource: https://github.com/studcavity/claude_sandbox/blob/main/claude_sandbox/github/land.py
    title: Opens PRs for a session's branches
---

# Trigger

You are about to finish a branch, hand control back, or end the session.

# Steps

1. **Propose a PR synopsis in your last commit** of each branch: one
   `PR-Title: <what the branch does, under 80 chars>` line and a few
   `PR-Summary: <point>` lines (what and why; one line each) in the message body. The
   launcher uses the title as the PR title (appending `[<session>]`) and copies both,
   sanitised and marked untrusted, into the PR description; a later commit's synopsis
   replaces an earlier one.[^land]
2. **Run `sandbox-check`** (no arguments). It lints your commits since the default
   branch the way CI does (`todo/` files, no `fixup!` or merge commits, no
   credential-shaped text, a `Verified` or `Not verified` line where files need a
   real run). Fix with new commits (or, on your own unmerged branch, rebase and
   `git push --force-with-lease`). It is advisory; CI decides.
3. **Check what the launcher will hold.** It opens a PR for each pushed branch that
   has commits, but holds back (no PR unless the user confirms at an interactive
   prompt) any branch that changes `.github/`, `.githooks/`, `CODEOWNERS`, the deps
   or host files or a `setup:` script, because those run with extra privilege. The
   check is against the default branch, so a branch built on a held one is held too.
   Say in your final message if a branch needs that kind of review.
4. **Say if the branch changes a workflow**, so the user knows to expect a held PR.
5. **List your branch names and what each contains** in your final message.

You cannot open or merge PRs yourself; the launcher does it after you exit, from the
host. Every PR targets the default branch, so prefer independent branches from it. If
you must build on another of your branches, it merges first; then rebase the other
(`git fetch origin && git rebase origin/HEAD`, then `git push --force-with-lease`).

[^land]: Opens PRs for a session's branches
