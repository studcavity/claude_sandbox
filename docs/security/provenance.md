---
type: Security Model
title: "Provenance: which commits came from the session"
description: "Per-session SSH signing keys let the launcher prove every commit on a PR came from the session or from you."
tags: [security, signing, github]
generated: { by: "claude-code/claude-sonnet-5-5", at: "2026-11-19T01:06:00Z" }
sources:
  - id: prov
    resource: https://github.com/studcavity/claude_sandbox/blob/main/claude_sandbox/provenance.py
    title: Signing keys and the sandbox/provenance status
---

A deploy key can push to any branch, so a sibling session, or anyone with push
access, can add commits to a session's branch even after its PR is open. The
launcher can prove which commits a session made.[^prov]

# How it works

- At launch the container makes an ed25519 signing key and git signs every commit
  with it. Only the public half leaves; the launcher records it on the host
  (`~/.local/state/claude_sandbox/signing/<owner>/<repo>/<session>.pub`, or under
  `$CLAUDE_SANDBOX_STATE_DIR`), never in the repo, where the session could change
  it. The private half dies with the container. A new session never reuses a name
  that has a recorded key.
- After teardown, for each PR it opened or found, the launcher checks that every
  commit between the default branch and the PR's head has a good signature from
  that session's key **or one of your own SSH signing keys on GitHub** (so your
  rebases and fixups pass if you sign them), and that none is a merge commit. It
  posts the result as the commit status `sandbox/provenance` on the head commit
  (`--no-provenance` skips this).
- Verification is git's own (`%G?` / `%GF`) with your git config shut out and
  `gpg.ssh.program` pinned: a lying program would make git call any commit good,
  and a test proves it.

# Re-checking after later pushes

A status belongs to one commit, so anything pushed later leaves the new head
without one. With `rulesets/require-sandbox-provenance.json` applied, the merge
stays blocked until you re-check:

```
./venv/bin/python -m claude_sandbox.provenance OWNER/REPO --pr N
```

Fixes: drop or re-sign foreign commits, or
`git rebase --exec 'git commit --amend --no-edit -S' origin/main` with your own SSH
signing key. Or break glass for this one head commit with `--override "reason"`,
which posts success and comments on the PR without checking signatures.

# What it proves and what it does not

It proves every commit came from this session or from you. It does not prove the
session's own commits are benign (it holds its key), or that CI did not already
run foreign commits (`pull_request: synchronize` runs on every push).

# Caveats

- GitHub's "Update branch" button and GPG-signed commits fail the check (only SSH
  signing keys are read from your account), which is why "require branches to be
  up to date" is off.
- A PR from before this feature, or from another host, has no recorded key and
  fails closed.
- The status is posted with your `gh` login, so a ruleset cannot pin it to an app:
  anything else allowed to post statuses (for example a workflow's `GITHUB_TOKEN`
  when default workflow permissions allow writes) could post a fake success. The
  launcher offers to make that default read-only; see
  [Actions hardening](actions-hardening.md).

[^prov]: Signing keys and the sandbox/provenance status
