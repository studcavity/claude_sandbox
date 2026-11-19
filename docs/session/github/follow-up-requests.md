---
type: Procedure
title: "Asking for a GitHub issue with a todo/ file (GitHub backend)"
description: "How to leave a follow-up issue request by committing todo/<name>.md and what to put in it; read when you find work you cannot or should not do now."
tags: [issues, todo]
generated: { by: "claude-code/claude-sonnet-5-5", at: "2026-11-19T01:06:00Z" }
sources:
  - id: issues
    resource: https://github.com/studcavity/claude_sandbox/blob/main/claude_sandbox/github/issues.py
    title: Files issues for todo/ files
  - id: todo
    resource: https://github.com/studcavity/claude_sandbox/blob/main/claude_sandbox/portable/todo.py
    title: The todo/ file format
---

# How it works

Add `todo/<name>.md` to a commit (`<name>`: lowercase letters, digits and `-`). Its
first heading is the issue title and the whole file is the issue text. It is filed as
an issue only once the file reaches the default branch (unmerged work files nothing),
so deleting it before merge withdraws the request, and deleting it later closes
nothing.[^todo]

# What to write

It is **NOT a work order**: describe a problem and some potential solutions, and do not
dictate which one gets implemented. Write it for a fresh agent with no context from
this session:

- the problem and why it matters;
- what you tried or ruled out;
- exact files, commands and error text;
- why it was blocked here;
- what "done" looks like and how to verify it;
- options with trade-offs (say which you lean towards, as a suggestion);
- anything risky.

Delete the file in the commit that resolves it. `sandbox-check` lints the name, size
and heading; see [Handing your work back](handing-back.md).

[^todo]: The todo/ file format
