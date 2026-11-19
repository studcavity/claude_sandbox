---
type: Security Model
title: "Optional rulesets: confine deploy keys to sbx/"
description: "Two admin-bypassable rulesets, applied by hand, that stop deploy keys creating arbitrary branches or pushing tags."
tags: [security, github, rulesets]
generated: { by: "claude-code/claude-sonnet-5-5", at: "2026-11-19T01:06:00Z" }
sources:
  - id: r1
    resource: https://github.com/studcavity/claude_sandbox/blob/main/rulesets/optional/sandbox-branches-only.json
    title: sandbox-branches-only
  - id: r2
    resource: https://github.com/studcavity/claude_sandbox/blob/main/rulesets/optional/no-tag-pushes.json
    title: no-tag-pushes
---

A deploy key can create any branch and push tags. A pushed tag runs tag-triggered
(release) workflows on code the session wrote, with whatever publishing secrets
they use; a pushed branch runs any unfiltered `on: push` workflow. Two rulesets in
`rulesets/optional/` stop that for everyone but repo admins (deploy keys are never
admins):

```
gh api -X POST repos/OWNER/REPO/rulesets --input rulesets/optional/sandbox-branches-only.json
gh api -X POST repos/OWNER/REPO/rulesets --input rulesets/optional/no-tag-pushes.json
```

- `sandbox-branches-only`: outside `refs/heads/sbx/**`, non-admins cannot create,
  update or delete branches. That includes non-admin collaborators merging PRs and
  bots pushing to, say, `gh-pages`; skip it if you rely on those.[^r1]
- `no-tag-pushes`: non-admins cannot create, move or delete tags, including release
  bots using `GITHUB_TOKEN`.[^r2]

They have an admin bypass, which repo-supplied rulesets may not have, so the
launcher never offers them. Apply them by hand after reading them.

[^r1]: sandbox-branches-only
[^r2]: no-tag-pushes
