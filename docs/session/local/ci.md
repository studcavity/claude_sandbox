---
type: Reference
title: "Testing without CI (local backend)"
description: "Nothing runs CI on your branches here, so your own runs are the only check."
tags: [ci, testing]
generated: { by: "claude-code/claude-sonnet-5-5", at: "2026-11-19T01:06:00Z" }
sources:
  - id: local
    resource: https://github.com/studcavity/claude_sandbox/blob/main/claude_sandbox/backends/local.py
    title: The local backend
---

Nothing runs CI on your branches: your own runs are the only check. Run the whole
suite if it is reasonably fast, and say what you did not run. See
[Testing and quality gates](testing.md).[^local]

[^local]: The local backend
