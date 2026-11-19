---
type: Procedure
title: "Leaving a follow-up request with a todo/ file (local backend)"
description: "How to leave a follow-up request for the user by committing todo/<name>.md; read when you find work you cannot or should not do now."
tags: [todo, review]
generated: { by: "claude-code/claude-sonnet-5-5", at: "2026-11-19T01:06:00Z" }
sources:
  - id: todo
    resource: https://github.com/studcavity/claude_sandbox/blob/main/claude_sandbox/portable/todo.py
    title: The todo/ file format
---

Add `todo/<name>.md` to a commit (`<name>`: lowercase letters, digits and `-`; the
first heading is the title). Nothing files it here: the user reads it when reviewing
the branch.[^todo]

Describe the problem and options for someone with no context from this session, and
do not dictate a solution:

- the problem and why it matters;
- what you tried or ruled out;
- exact files, commands and error text;
- why it was blocked here, and what "done" looks like;
- options with trade-offs (say which you lean towards, as a suggestion).

Delete the file in the commit that resolves it.

[^todo]: The todo/ file format
