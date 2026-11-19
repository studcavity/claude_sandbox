---
type: Security Model
title: "Threat model: what is and is not protected"
description: "The trust boundaries claude_sandbox draws around an agent session, and what each protection does and does not stop."
tags: [security, threat-model]
generated: { by: "claude-code/claude-sonnet-5-5", at: "2026-11-19T01:06:00Z" }
sources:
  - id: plan
    resource: /decisions/07-untrusted-ci-context.md
    title: "Spec 07: untrusted CI context"
---

The launcher assumes a session can be prompt-injected and may act maliciously.
The goal is not to make that harmless but to confine the damage to one repo's
unmerged branches and make it visible to a human before anything lands.
The guiding rule, from [Spec 07](../decisions/07-untrusted-ci-context.md):[^plan]
**nothing from a sandbox runs in a trusted execution context before a human has
reviewed it.**

# What the session holds

The launcher puts exactly three secrets in a container: the Claude credential,
the repo-scoped deploy key, and the session's commit-signing key. Everything
else on the host (files, credentials, services) is unreachable unless you grant
a [mount or USB device](../guides/host-access.md). The deploy key and container
are destroyed at exit. See [Secret scanning](secrets-scanning.md).

# Protections and their limits

| Protection | Stops | Does not stop |
| --- | --- | --- |
| Deploy key scoped to one repo | Reaching other repos | Pushing to any branch of that repo, including other sessions' |
| No GitHub token in the sandbox | Opening or merging PRs, filing issues | (nothing: the launcher does these from the host) |
| PRs opened after teardown, pinned to the checked commit | A session editing a branch it was judged on | A *live sibling* session pushing to an open PR |
| [Default-branch ruleset](branch-protection.md) | Direct pushes, force-push, deletion on the default branch | Admins; a repo that never applied it |
| [Commit signing](provenance.md) | Foreign commits on a session's branch going unnoticed | A malicious session's own commits (it holds its key) |
| Hold for privileged files | `.github/`, hooks, CODEOWNERS, deps/host files merging unseen | A human approving without reading |
| [Actions hardening checks](actions-hardening.md) | Session CI code approving PRs or using a writable token | Whether a deploy key can push workflow files (open question) |

# Where gaps are reported

Protection gaps are reported to **you** and never to the session: at launch (a
`warning:` line) and again in the exit summary, so a long session does not scroll
them away.

# Further reading

[Optional rulesets](optional-rulesets.md) tighten branch and tag creation for
non-admins. Design history is in [decisions](../decisions/index.md).

[^plan]: Spec 07: untrusted CI context
