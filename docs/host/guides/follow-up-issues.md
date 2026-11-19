---
type: Guide
title: "Follow-up issues (todo/ files)"
description: "How a session asks for a GitHub issue by committing todo/<name>.md, and why the issue is filed only after merge."
tags: [github, issues, workflow]
generated: { by: "claude-code/claude-sonnet-5-5", at: "2026-11-19T01:06:00Z" }
sources:
  - id: issues
    resource: https://github.com/studcavity/claude_sandbox/blob/main/claude_sandbox/github/issues.py
    title: Files issues for todo/ files
  - id: todo
    resource: https://github.com/studcavity/claude_sandbox/blob/main/claude_sandbox/portable/todo.py
    title: The todo/ file format
---

*GitHub sessions only.* In a `--local` session a `todo/` file is simply read by the user when reviewing the branch; nothing files it.

A session cannot call the GitHub API, so it asks for an issue by adding
`todo/<name>.md` to a commit. `<name>` is lowercase letters, digits and `-`; the
first heading is the issue title, the file is the body, and the path is its
identity. `sandbox_check.py` (run in CI) checks the name, size and heading.[^todo]

The issue is filed **only after the file reaches the default branch**. That way
a human has reviewed the text before it is published, and a request whose PR is
never merged is never filed (delete the file before merge to withdraw it). The
reasoning is in [Spec 03](../../dev/decisions/03-file-issues-after-merge.md) and
[Spec 02](../../dev/decisions/02-todo-file-is-the-request.md).

# Who files it

`github/issues.py`, run by `.github/workflows/file-issues.yml` on every push to
`main`. Issues come from `github-actions[bot]`, and the job's only write
permission is `issues: write`.[^issues] To run it by hand in a checkout:

```
python -m claude_sandbox.github.issues OWNER/REPO --since REV [--dry-run]
```

The one ledger is a marker on the last line of each issue, so running it twice
over a range files nothing new. A later edit to the file is ignored, deleting a
file closes nothing, and a path added again after deletion is a new request. A
repo without the workflow (and `github/issues.py`) files nothing unless someone
runs it. The launcher itself files nothing when a session ends.

[^todo]: The todo/ file format
[^issues]: Files issues for todo/ files
