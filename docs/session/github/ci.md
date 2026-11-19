---
type: Reference
title: "CI and workflows (GitHub backend)"
description: "How CI runs for a session's PRs, how to watch it, and the rules for editing workflows; read before touching .github/ or reporting CI status."
tags: [ci, github-actions]
generated: { by: "claude-code/claude-sonnet-5-5", at: "2026-11-19T01:06:00Z" }
sources:
  - id: land
    resource: https://github.com/studcavity/claude_sandbox/blob/main/claude_sandbox/github/land.py
    title: Opens PRs for a session's branches
---

CI normally runs on the PR the launcher opens **after** you exit, so validate locally.

- If the repo has CI, make sure new tests run there. Edit `.github/workflows/*` in
  place, do not park it elsewhere (for example `ci/`): the user approves the change in
  session, and the launcher holds the PR for their confirmation when it files it. Say
  in your final message that the branch changes a workflow.
- If a workflow runs on pushes to your branch, poll the public API (there is no
  token here): `https://api.github.com/repos/OWNER/REPO/actions/runs?head_sha=SHA`.
  On a 404, 401/403 or rate limit, say plainly that you cannot watch CI; never report
  CI as passing unseen. Mention anything you suspect CI will catch.
- CI job names may be required status checks. Before you add, rename or remove one,
  tell the user in your final message: renaming a required job strands every open PR.
  Do not assume a check is required or enforced.
