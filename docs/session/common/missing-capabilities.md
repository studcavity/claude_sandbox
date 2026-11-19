---
type: Procedure
title: "When the sandbox lacks something the task needs"
description: "What to do instead of faking or giving up when a task needs a device, permission, host service, credential or tool this sandbox cannot provide."
tags: [limits, launcher]
generated: { by: "claude-code/claude-sonnet-5-5", at: "2026-11-19T01:06:00Z" }
sources:
  - id: repo
    resource: https://github.com/studcavity/claude_sandbox/blob/main/
    title: The claude_sandbox launcher repository
---

# Trigger

The task needs something this sandbox does not provide: access to a device, a
permission or capability the container lacks, a host service, nested containers,
credentials, a tool that cannot be installed here. Do not silently work around it,
fake it, or give up.

# Steps

1. Confirm it really is a sandbox limitation, not something you can fix inside the
   container (for example by installing a package or using a venv).
2. Review the `studcavity/claude_sandbox` repository (the launcher that creates this
   sandbox) to see whether the launcher could grant it: clone it read-only into the
   scratchpad or another throwaway directory, never into the working copy, and never
   push to it. If it cannot be cloned from here (this sandbox's credential is scoped
   to its own repo or absent), say so and base the plan on what you know.
3. Draft a plan for adding the capability to the launcher: what is missing and why
   the task needs it, which files would change and how, security and isolation
   trade-offs (what this would expose from the host), and how to verify it works. If
   the repo shows no safe way to add it, say that instead.
4. Give the plan to the user as a single self-contained block they can copy and
   paste into a session in the `claude_sandbox` repo, written so that session needs
   none of this conversation's context. Do not try to implement it yourself, and tell
   the user what you could not do in the meantime.
