---
type: Guide
title: "Concurrent sessions and landing PRs"
description: "How several sandboxes share one repo, and how a session's branches become PRs that you merge."
tags: [usage, github, pull-requests]
generated: { by: "claude-code/claude-sonnet-5-5", at: "2026-11-19T01:06:00Z" }
sources:
  - id: land
    resource: https://github.com/studcavity/claude_sandbox/blob/main/claude_sandbox/github/land.py
    title: Opens PRs for a session's branches
  - id: sessions
    resource: https://github.com/studcavity/claude_sandbox/blob/main/claude_sandbox/sessions.py
    title: Session ids and branch prefix
---

Any number of sandboxes can run on one repo at once. The rest of this page is
what keeps them apart and what happens to their work.

# Identity and branches

- Each session gets a Docker-style id (`quirky-hopper`, or `--name`), used as the
  container name, in its deploy key's title (`ephemeral <host>:<id>`) and in its
  branch names.[^sessions]
- The clone starts on `sbx/<id>/work`. The session renames it to a numbered
  slug of its task (`01-fix-x`) before its first push, and PRs are opened in
  alphanumeric branch order. It is told to use only `sbx/<id>/*`, never the
  default branch or another session's branches.
- The branch prefix is advice, not a permission: a deploy key can push anywhere.
  The enforcement is on GitHub; see [Protecting the default branch](../security/branch-protection.md).

# Pull requests

When a session ends, the launcher (on the host, with your `gh` login) opens a PR
for each of its pushed branches. **No GitHub credential enters the sandbox**: it
can push with its deploy key and nothing else.[^land]

- `--no-pr` skips opening PRs. `./run.sh REPO --land-all` opens PRs for every
  unmerged `sbx/*` branch (recovery after a crashed launcher; it also grabs live
  sessions' work).
- Session text in PRs is sanitised (no live `@mentions`) and labelled untrusted.
- Every PR targets the default branch. Workflows are filtered to PRs into it, so
  a PR into another branch would get no CI, and the launcher no longer opens
  those. A branch built on another of a session's branches shows its parent's
  commits too: merge the parent first, then rebase.
- **Held, not opened:** a branch that changes `.github/`, `.githooks/`,
  `CODEOWNERS`, the deps/host files or a `setup:` script (and any branch built
  on one) gets no PR; review it by hand. In an interactive terminal the launcher
  asks `Open a PR for it anyway? [y/N]` (default no). A branch whose tip can't
  be pinned, or that moves while landing, is never overridable. These files run
  with extra privilege (CI with Actions secrets; root and the deploy key in
  every future sandbox), so they are how an injected session would persist.
- **Landing happens after teardown.** The session's key is revoked and its
  container destroyed *before* the launcher checks branches and opens PRs. Each
  PR is pinned to the commit that was checked; if the branch moved anyway
  (another live session's key can push to any branch), the PR is closed and
  reported HELD. A live sibling pushing to a PR *after* it was opened is not
  stopped: treat every push to an open sandbox PR as unreviewed, and do not run
  concurrent sessions on repos whose CI exposes secrets to same-repo PRs.
- **Merging is yours.** The launcher never merges and there is no auto-merge (a
  PR that merges itself would let one injected session change `main`). Use
  GitHub's rebase or squash button to keep history linear.

The exit summary lists every branch with its PR number, target, commit count and
URL, or why none was opened:

```
┌ pull requests for session quirky-hopper ───────────────
│ ✔ opened    #12   sbx/quirky-hopper/1-api → main  [3 commits]
│             https://github.com/o/r/pull/12
│ ⚠ HELD      sbx/quirky-hopper/ci
└ 1 opened. The launcher never merges.
```

# Housekeeping

- **Stale keys.** A launcher killed by SIGKILL or a reboot leaves its write
  deploy key behind. Each launch deletes this host's keys whose container no
  longer exists; siblings' and other hosts' keys are never touched.
- **Image and venv builds** are serialised with file locks, so simultaneous
  first launches build once.
- **Follow-up issues** a session asks for are covered in
  [Follow-up issues](follow-up-issues.md).

[^sessions]: Session ids, branch prefix and stale-key reaping
[^land]: Opens PRs for a session's branches
