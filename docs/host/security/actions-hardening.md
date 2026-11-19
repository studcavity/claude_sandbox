---
type: Security Model
title: "Actions hardening: keep session code's CI untrusted"
description: "Why same-repo PR CI runs session code, which settings the launcher checks or offers, and the open question about workflow pushes."
tags: [security, github, actions]
generated: { by: "claude-code/claude-sonnet-5-5", at: "2026-11-19T01:06:00Z" }
sources:
  - id: hard
    resource: https://github.com/studcavity/claude_sandbox/blob/main/claude_sandbox/github/hardening.py
    title: Checks and tightens Actions settings
  - id: todo
    resource: https://github.com/studcavity/claude_sandbox/blob/main/todo/verify-deploy-key-workflow-push.md
    title: Open verification request
---

*GitHub sessions only:* a `--local` session pushes nothing and runs no CI.

The rule: **nothing runs in a trusted execution context before a human has
reviewed it.** A same-repo PR runs the session's own workflow code (pytest, a
`conftest.py`, anything it imports) before anyone reads it. That is acceptable
only while the run is *untrusted*: a read-only token, no reachable secrets, no way
to approve or post anything that gates a merge, and logs that stay on GitHub.

# What can and cannot be set

GitHub has no setting that requires approval for same-repo runs. Its approval
settings cover *fork* PRs only, and sandbox PRs come from a branch in the repo. So
at launch the launcher checks what *can* be set and states each condition as ok /
NOT MET / unknown:[^hard]

- the default workflow token is read-only, and Actions may not approve pull
  requests. These are offered on a terminal, one yes/no each (default no), showing
  the exact `PUT .../actions/permissions/workflow` it would make; re-running finds
  nothing left to offer (`--no-hardening-prompt` skips the offer, not the check). A
  409 (your organisation forbids it) or 403 (you are not a repo admin) is
  reported, never fatal;
- no repo- or organisation-level Actions secrets exist. This is detected only
  (names, never values): the launcher cannot move them. Put real secrets in an
  Environment limited to the default branch.

What is not met is repeated as a warning in the exit summary, to you and not to
the session. The rationale is in [Spec 07](../../dev/decisions/07-untrusted-ci-context.md).

# Open question: can a deploy key push workflow files?

**Not verified.** Removing the `workflow` scope from your `gh` token (see
[Getting started](../guides/getting-started.md)) does not answer it, since a
deploy key is a separate SSH credential with no scopes, and
`default_workflow_permissions` is only a default: a workflow that declares its own
`permissions:` gets them. If such a push is accepted, a session can push an
`on: push` workflow that runs at once, before any PR or hold, with any repo secret
it names. That would be a fix-immediately finding.

Options if so: a push ruleset with a `file_path_restriction` on
`.github/workflows/**` (only some plans for private repos); secrets kept in
protected-branch Environments; or a staging repo (item 6 of the
[simplification plan](../../dev/plans/simplification.md)). The check needs a scratch repo
with no secrets and a deploy key: push a no-op workflow file and record the
result. It has not been done; see `todo/verify-deploy-key-workflow-push.md`.

[^hard]: Checks and tightens Actions settings
