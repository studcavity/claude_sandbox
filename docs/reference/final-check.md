---
type: Reference
title: "The final check (check: scripts and merge check)"
description: "What runs when you exit Claude, and how failures resume the session so it can fix them before teardown."
tags: [deps, check, teardown]
generated: { by: "claude-code/claude-sonnet-5-5", at: "2026-11-19T01:06:00Z" }
sources:
  - id: resume
    resource: https://github.com/studcavity/claude_sandbox/blob/main/claude_sandbox/resume.py
    title: Failure text and resume prompt
  - id: merge
    resource: https://github.com/studcavity/claude_sandbox/blob/main/claude_sandbox/mergecheck.py
    title: Built-in branch merge check
---

Exiting Claude does not tear the sandbox down at once. Scripts under `check:` in
[the deps file](repo-deps-file.md) (same entry shape as `setup:`) run first, in
the still-live container, in the clone, in order, each under its `timeout`,
streaming output to your terminal.[^resume]

If one fails you are asked `Resume the Claude session to fix it? [Y/n]`. Yes
reopens the same conversation (`claude --continue`: no new session, no adopting
branches) with a prompt holding each failing script's last ~6000 characters of
output. Claude fixes, commits and pushes (the deploy key is still valid), you
exit, and the check runs again, until it passes or you say no. Answering no, or
having no terminal, tears down as before with a warning in the log; what was
pushed still becomes a PR. Ctrl-C during the check skips it and tears down.
`--no-final-check`, `--no-deps` and `--shell` skip it.

# The launcher's own check

Whether or not the repo has `check:` scripts, the launcher judges each of the
session's branches alone against its base and reports a conflict (being behind is
fine).[^merge] On GitHub it covers the session's *pushed* `sbx/<session>/*`
branches after a fetch, each against the default branch (what its PR will
target); local branches never pushed, or moved since, are reported too. With
`--local` it covers the session's local branches against the base it started
from. Empty branches and other sessions' branches are ignored. Findings resume
the session like a failed script, and after the rebase the scripts run again.

# Writing a good check script

Point `check:` at one script that runs the repo's whole lint and test suite and
exits non-zero on any failure. Only the script's *path* comes from the default
branch; its contents come from the session's checkout, so this catches honest
mistakes. It is not a security boundary: the PR's CI is the real gate. This
repo's `scripts/final-check.sh` also runs `tests/integration` when the sandbox was
launched with `--nesting` (and says so when it skips them).

[^resume]: Failure text and resume prompt
[^merge]: Built-in branch merge check
