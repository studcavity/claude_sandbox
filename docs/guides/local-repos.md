---
type: Guide
title: "Local repositories (--local)"
description: "Work on a git repository that is not on GitHub: one branch goes in as a bundle, session branches come back as bundles."
tags: [usage, local]
generated: { by: "claude-code/claude-sonnet-5-5", at: "2026-11-19T01:06:00Z" }
sources:
  - id: local
    resource: https://github.com/studcavity/claude_sandbox/blob/main/claude_sandbox/backends/local.py
    title: The local backend
---

For code that is not on GitHub, or that the sandbox must not reach over the
network, point the launcher at a git repository on the host:

```
./run.sh --local ~/src/proj [--base BRANCH]   # default base: the checked-out branch
./run.sh --local ~/src/proj --recover         # after a crash: import what was left
```

The design and its alternatives are in
[Spec 08](../decisions/08-local-backend.md).[^local]

# How work goes in and out

- **In.** The launcher bundles that one branch (`git bundle`, from its current
  commit; uncommitted changes and other branches stay behind) and streams it
  into the container, which clones it onto the size-capped work volume and
  starts `sbx/<session>/work`. Nothing is bind-mounted, so the container never
  sees your repo's `.git` (refs, config, hooks). It gets no GitHub key or other
  network credential; outbound internet works as usual.
- **Out.** When the session ends, the launcher bundles `sbx/<session>/*` out of
  the still-running container, destroys it, then fetches **only** those refs
  from the bundle file into your repo (fsck on, one explicit refspec per
  branch, never forced: an existing branch only fast-forwards). A fetch from a
  bundle runs nothing from the session's repo, and nothing outside
  `refs/heads/sbx/<session>/` can be created or moved. Your checked-out branch
  and work tree are never touched.

# Review

The launcher prints a report per branch: commits, provenance (every commit
signed by the session's key, recorded under
`~/.local/state/claude_sandbox/signing-local/`), commit-checker errors, files
that run with privilege (flagged, not held: nothing runs on import), and the
session's `PR-Title` / `Not verified` lines. After that it is plain git:
`git log -p main..sbx/<session>/work`, merge or cherry-pick, `git branch -D`
when done.

There is no CI. Running the branch's tests runs session code, so treat it like
any untrusted branch (a `.gitattributes` it adds applies when you check it out).

# Crashes

The work volume is kept (`claude-sandbox-keep-*`, never swept) until its work is
imported. `--recover` mounts each one read-only in a throwaway container,
exports from there, imports, and deletes the volume. With `--volume-size 0`, or
a pool without quotas, there is no kept volume and a crash loses the session's
work (the session is told which case it is in). Bundles waiting for import live
under `~/.local/state/claude_sandbox/local/`.

GitHub-only flags (`REPOSITORY`, `--account-key`, `--land-all`, `--read-only`)
are refused with `--local`.

[^local]: The local backend
