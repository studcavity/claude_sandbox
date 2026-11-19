---
type: Security Model
title: "Secrets: what is scanned, and GitHub push protection"
description: "Which credentials the launcher scans for, which it deliberately does not, and why GitHub push protection is a useful extra."
tags: [security, secrets]
generated: { by: "claude-code/claude-sonnet-5-5", at: "2026-11-19T01:06:00Z" }
sources:
  - id: untrusted
    resource: https://github.com/studcavity/claude_sandbox/blob/main/claude_sandbox/portable/untrusted.py
    title: Sanitising and credential scan
  - id: spec
    resource: https://github.com/studcavity/claude_sandbox/blob/main/docs/dev/decisions/04-narrow-secret-scan.md
    title: "Spec 04"
---

A session can only leak what it can see, and the launcher puts exactly three
secrets in a container: the Claude credential (`sk-ant-...`), the deploy key and
the session signing key (OpenSSH private keys).

`portable/untrusted.py` scans for those, plus GitHub token formats, in commit
messages and added lines (CI), PR text and `todo/` issue text. It reports the
kind, never the value.[^untrusted] It does not scan for credentials the sandbox
never holds (AWS, Slack, generic `password = ...`), and there is no allow marker:
a test fixture is assembled at runtime (`"ghp_" + "x" * 40`). The narrow scope is
deliberate; see [Spec 04](../../dev/decisions/04-narrow-secret-scan.md).

# GitHub push protection

The launcher does **not rely on** push protection, but it is the better first line
where you have it, because it stops a known credential before it is in history:

```
gh api -X PATCH repos/OWNER/REPO -f 'security_and_analysis[secret_scanning][status]=enabled' -f 'security_and_analysis[secret_scanning_push_protection][status]=enabled'
```

(or Settings, then Code security). **Not verified:** whether it is available for
private repos on your plan (as written, free for public repos and a paid GitHub
Secret Protection feature for private ones), or whether it applies to pushes made
with a deploy key. Test with a throwaway credential before relying on it.

[^untrusted]: Sanitising and credential scan
